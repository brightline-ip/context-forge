import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
import zipfile

from context_forge.enrich import enrich, validate_capsule
from context_forge.extractors import extract, _decode
from context_forge.inline import normalize_inline
from context_forge.normalize import chunk, normalize
from context_forge.pipeline import Pipeline
from context_forge.safety import BusyError, InputError, validate_file
from context_forge.server import App, ExclusiveHTTPServer, handler_factory, load_service_token
from context_forge.worker_runtime import WorkerRuntimeError, _environment


TEST_TOKEN = "context-forge-python-test-token-000000000000000"
WRONG_TOKEN = "context-forge-python-wrong-token-0000000000000"


class HTTPBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.security_dir = self.root / 'security'
        self.security_dir.mkdir()
        self.default_token_file = self.security_dir / 'service-token.txt'
        self.default_token_file.write_text(TEST_TOKEN, encoding='utf-8')
        self.configured_token_file = self.root / 'operator-config' / 'external-auth-token.txt'
        self.configured_token_file.parent.mkdir()
        self.configured_token_file.write_text(TEST_TOKEN, encoding='utf-8')
        self.app = App(TEST_TOKEN, self.root, self.configured_token_file)
        self.server = ExclusiveHTTPServer(('127.0.0.1', 0), handler_factory(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.app.close()
        self.temp.cleanup()

    def test_second_service_cannot_recover_active_database(self):
        with self.assertRaises(InputError):
            App(TEST_TOKEN, self.root)

    def request(self, method, route, body=None, headers=None, auth=True):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            supplied = {'Content-Type': 'application/json'} | (headers or {})
            if auth:
                supplied['Authorization'] = 'Bearer ' + (TEST_TOKEN if auth is True else auth)
            conn.request(method, route, body=None if body is None else json.dumps(body), headers=supplied)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_cross_site_and_rebinding_requests_rejected(self):
        for headers in ({'Origin': 'https://hostile.invalid'}, {'Host': 'hostile.invalid'}, {'Sec-Fetch-Site': 'cross-site'}, {'Origin': 'null'}):
            status, _ = self.request('POST', '/api/normalize/text', {'text': 'private'}, headers)
            self.assertEqual(status, 403)
        status, _ = self.request('POST', '/api/normalize/text', {'text': 'hello'}, {'Origin': f'http://127.0.0.1:{self.port}'})
        self.assertEqual(status, 200)

    def test_token_covers_all_api_routes(self):
        for route in ['/api/status', '/api/jobs', '/api/settings', '/api/jobs/' + '0'*12, '/api/documents/' + '0'*32]:
            self.assertEqual(self.request('GET', route, auth=False)[0], 401)
            self.assertEqual(self.request('GET', route, auth=WRONG_TOKEN)[0], 401)
        self.assertEqual(self.request('POST', '/api/jobs/path', {'path': str(self.root)}, auth=False)[0], 401)
        self.assertEqual(self.request('POST', '/api/ocr/continue', {'document_id': '0'*32}, auth=WRONG_TOKEN)[0], 401)
        status, result = self.request('GET', '/api/status')
        self.assertEqual((status, result['security_revision'], result['auth_required']), (200, 3, True))

    def test_service_token_loads_from_exactly_one_protected_source(self):
        token_file = self.root / 'token.txt'
        token_file.write_text(TEST_TOKEN, encoding='utf-8')
        self.assertEqual(load_service_token(self.root, {'CONTEXT_FORGE_TOKEN': TEST_TOKEN}), TEST_TOKEN)
        self.assertEqual(load_service_token(self.root, {'CONTEXT_FORGE_TOKEN_FILE': str(token_file)}), TEST_TOKEN)
        self.assertEqual(load_service_token(self.root, {}), TEST_TOKEN)
        with self.assertRaises(InputError):
            load_service_token(self.root / 'missing-root', {})
        with self.assertRaises(InputError):
            load_service_token(self.root, {'CONTEXT_FORGE_TOKEN': WRONG_TOKEN, 'CONTEXT_FORGE_TOKEN_FILE': str(token_file)})
        token_file.write_text('short', encoding='utf-8')
        with self.assertRaises(InputError):
            load_service_token(self.root, {'CONTEXT_FORGE_TOKEN_FILE': str(token_file)})
        with self.assertRaises(InputError):
            App(None, self.root)

    def test_json_shape_content_type_and_length_validation(self):
        for body in ({'document_id':'../data'}, {'document_id':'0'*32,'path':'private'}, {}):
            self.assertEqual(self.request('POST','/api/ocr/continue',body)[0],400)
        self.assertEqual(self.request('POST', '/api/normalize/text', [1, 2])[0], 400)
        self.assertEqual(self.request('POST', '/api/normalize/text', {'text': 'x', 'unexpected': 'retained'})[0], 400)
        source = self.root / 'source.txt'
        source.write_text('fixture')
        self.assertEqual(self.request('POST', '/api/jobs/path', {'path': str(source), 'unexpected': True})[0], 400)
        self.assertEqual(self.request('POST', '/api/normalize/text', {'text': 'x'}, {'Content-Type': 'text/plain'})[0], 400)
        with socket.create_connection(('127.0.0.1', self.port), timeout=3) as connection:
            connection.sendall(f'POST /api/normalize/text HTTP/1.0\r\nHost: 127.0.0.1:{self.port}\r\nAuthorization: Bearer {TEST_TOKEN}\r\nContent-Type: application/json\r\nContent-Length: -1\r\n\r\n'.encode())
            self.assertIn(b'400', connection.recv(1024))

    def test_settings_cannot_redirect_content_or_expand_roots(self):
        original = dict(self.app.settings)
        for body in [{'ollama_base_url': 'http://hostile.invalid'}, {'allowed_input_roots': ['C:/']}, {'chunk_chars': 0}, {'llm_timeout_seconds': 999999}, {'output_dir': str(self.root.parent)}]:
            self.assertEqual(self.request('POST', '/api/settings', body)[0], 400)
            self.assertEqual(self.app.settings, original)
        self.assertEqual(self.request('POST', '/api/settings', {'chunk_chars': 4000})[0], 200)
        self.assertEqual(json.loads((self.root/'data/settings.json').read_text())['chunk_chars'], 4000)

    def test_input_roots_and_generated_storage_rejected(self):
        for path in [str(self.root.parent), str(self.root/'data/settings.json'), '\\\\hostile.invalid\\share\\input.txt']:
            self.assertEqual(self.request('POST', '/api/jobs/path', {'path': path})[0], 400)
        credential_dir = self.root/'.hermes'
        credential_dir.mkdir()
        (credential_dir/'auth.json').write_text('{}')
        self.assertEqual(self.request('POST', '/api/jobs/path', {'path': str(credential_dir/'auth.json')})[0], 400)

    def test_token_paths_and_recursive_discovery_are_fail_closed(self):
        for path in (self.default_token_file, self.security_dir, self.configured_token_file):
            self.assertEqual(self.request('POST', '/api/jobs/path', {'path': str(path)})[0], 400)
        self.assertEqual(self.app.pipeline.jobs(), [])
        ordinary = self.root / 'ordinary.txt'
        ordinary.write_text('ordinary fixture', encoding='utf-8')
        status, result = self.request('POST', '/api/jobs/path', {
            'path': str(self.root), 'recursive': True, 'enrich': False,
            'normalization_mode': 'lossless',
        })
        self.assertEqual(status, 202)
        for _ in range(500):
            job = self.app.pipeline.job(result['job_id'])
            if job['status'] not in {'queued', 'running'}:
                break
            time.sleep(.01)
        else:
            self.fail('Recursive fixture job did not finish')
        sources = {Path(item['source']).resolve() for item in job['files']}
        self.assertIn(ordinary.resolve(), sources)
        self.assertNotIn(self.default_token_file.resolve(), sources)
        self.assertNotIn(self.configured_token_file.resolve(), sources)

    def test_upload_rejects_credential_named_paths_without_staging_them(self):
        before = set(self.app.upload_root.iterdir())
        for filename, content in (
                ('security/service-token.txt', 'fixture'),
                ('operator-config/external-auth-token.txt', 'fixture'),
                ('renamed-credential.txt', TEST_TOKEN)):
            body = (f'--boundary\r\nContent-Disposition: form-data; name="files"; filename="{filename}"\r\n'
                    f'Content-Type: text/plain\r\n\r\n{content}\r\n--boundary--\r\n').encode()
            conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
            try:
                conn.request('POST', '/api/jobs/upload', body, {
                    'Content-Type': 'multipart/form-data; boundary=boundary',
                    'Authorization': 'Bearer ' + TEST_TOKEN,
                })
                response = conn.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
            finally:
                conn.close()
        self.assertEqual(set(self.app.upload_root.iterdir()), before)

    def test_upload_original_is_archived_before_cleanup(self):
        body = b'--boundary\r\nContent-Disposition: form-data; name="files"; filename="source.txt"\r\nContent-Type: text/plain\r\n\r\n    original bytes\r\n--boundary--\r\n'
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            conn.request('POST', '/api/jobs/upload', body, {
                'Content-Type': 'multipart/form-data; boundary=boundary',
                'Authorization': 'Bearer ' + TEST_TOKEN,
            })
            response = conn.getresponse()
            self.assertEqual(response.status, 202)
            job_id = json.loads(response.read())['job_id']
        finally:
            conn.close()
        self.app.pipeline.close()
        job = self.app.pipeline.job(job_id)
        self.assertEqual(job['status'], 'completed')
        result = self.app.pipeline.read_document(job['files'][0]['id'])
        original = Path(result['manifest']['original_path']).read_bytes()
        self.assertEqual(original, b'    original bytes')
        self.assertEqual(hashlib.sha256(original).hexdigest(), result['manifest']['source_sha256'])
        self.assertFalse(Path(job['files'][0]['source']).exists())

    def test_errors_and_access_logs_do_not_echo_input(self):
        marker = 'mock-private-input-DO-NOT-LOG'
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            status, result = self.request('POST', '/api/normalize/text?token=' + marker, {'text': [marker]})
        self.assertEqual(status, 400)
        self.assertNotIn(marker, json.dumps(result) + stream.getvalue())


class ExtractionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_archive_expansion_and_file_size_guards(self):
        archive = self.root/'bomb.epub'
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr('large.html', b'x' * (2 * 1024 * 1024))
        with self.assertRaises(InputError):
            validate_file(archive)
        with patch('context_forge.safety.MAX_FILE_BYTES', 1):
            with self.assertRaises(InputError):
                validate_file(archive)

    def test_xml_entities_are_blocked(self):
        file = self.root/'entity.xml'
        file.write_text('<!DOCTYPE root [<!ENTITY danger "untrusted">]><root>&danger;</root>')
        with self.assertRaises(Exception):
            extract(file)

    def test_json_exact_values_and_legacy_encoding_preserved(self):
        source = '{"n": 1.00000000000000000001, "n": 2}'
        file = self.root/'source.json'
        file.write_text(source)
        self.assertEqual(extract(file).sections[0].text, source)
        self.assertEqual(_decode(b'caf\xe9')[0], 'caf\u00e9')

    def test_docx_table_order_headings_and_spreadsheet_formulas(self):
        from docx import Document
        from openpyxl import Workbook
        document = Document()
        document.add_paragraph('Before')
        document.add_table(rows=1, cols=1).cell(0, 0).text = 'Middle'
        document.add_heading('Last heading')
        file = self.root/'source.docx'
        document.save(file)
        joined = '\n'.join(s.text for s in extract(file).sections)
        self.assertLess(joined.index('Before'), joined.index('Middle'))
        self.assertLess(joined.index('Middle'), joined.index('Last heading'))
        book = Workbook()
        book.active['A1'] = '=1+2'
        file = self.root/'source.xlsx'
        book.save(file)
        book.close()
        self.assertIn('=1+2', extract(file).sections[0].text)

    def test_blank_pdf_warns_and_chunk_spacing_is_preserved(self):
        from pypdf import PdfWriter
        file = self.root/'scan.pdf'
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(file)
        self.assertIn('OCR', extract(file).warnings[0])
        text = self.root/'source.txt'
        text.write_text('    indentation\n')
        self.assertEqual(chunk(normalize(extract(text), mode='lossless'))[0].text, '    indentation\n')

    def test_busy_guard_and_transactional_creation(self):
        source = self.root/'source.txt'
        source.write_text('mock data')
        pipeline = Pipeline(self.root, {'output_dir': str(self.root/'output')})
        pipeline.slots.acquire()
        pipeline.slots.acquire()
        with self.assertRaises(BusyError):
            pipeline.create_job([source], 'test', False)
        self.assertEqual(pipeline.jobs(), [])
        pipeline.slots.release()
        pipeline.slots.release()

    def test_restart_marks_incomplete_work_interrupted(self):
        pipeline = Pipeline(self.root, {'output_dir': str(self.root/'output')})
        pipeline.store.execute("INSERT INTO jobs(id,status,created_at,updated_at,source,options_json) VALUES('abc','running','now','now','test','{}')")
        restored = Pipeline(self.root, {'output_dir': str(self.root/'output')})
        self.assertEqual(restored.job('abc')['status'], 'interrupted')

    def test_extraction_timeout_is_visible(self):
        source = self.root/'source.txt'
        source.write_text('mock data')
        pipeline = Pipeline(self.root, {'output_dir': str(self.root/'output')})
        with patch('context_forge.pipeline.run_extraction_worker', side_effect=WorkerRuntimeError('timeout')):
            job_id = pipeline.create_job([source], 'test', False)
            pipeline.close()
        self.assertIn('120-second', pipeline.job(job_id)['files'][0]['error'])

    def test_snapshot_backup_and_chunk_corruption(self):
        source = self.root/'source.txt'
        source.write_text('backup fixture')
        pipeline = Pipeline(self.root, {'output_dir': str(self.root/'output')})
        job_id = pipeline.create_job([source], 'backup test', False)
        pipeline.close()
        item = pipeline.job(job_id)['files'][0]
        document = pipeline.read_document(item['id'])
        backup = self.root/'backup'
        backup.mkdir()
        with pipeline.store.connect() as current, contextlib.closing(sqlite3.connect(backup/'state.sqlite3')) as target:
            current.backup(target)
            self.assertEqual(target.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(target.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()[0], 'completed')
        shutil.copytree(self.root/'output', backup/'output')
        archived = Path(document['manifest']['original_path'])
        copied = backup/'output'/archived.relative_to(self.root/'output')
        self.assertEqual(hashlib.sha256(copied.read_bytes()).hexdigest(), document['manifest']['source_sha256'])
        chunks = Path(item['output_dir'])/'chunks.jsonl'
        record = json.loads(chunks.read_text())
        record['text'] = 'changed without matching hash'
        chunks.write_text(json.dumps(record) + '\n')
        with self.assertRaises(InputError):
            pipeline.read_document(item['id'])

    def test_parser_worker_does_not_inherit_credentials(self):
        with patch.dict(os.environ, {'MOCK_API_KEY': 'mock-value', 'CONTEXT_FORGE_TOKEN': 'mock-value'}):
            observed = _environment(self.root)
        self.assertNotIn('MOCK_API_KEY', observed)
        self.assertNotIn('CONTEXT_FORGE_TOKEN', observed)
        self.assertEqual(set(observed) - {'SYSTEMROOT', 'WINDIR', 'PATH', 'LANG', 'TEMP', 'TMP', 'PYTHONUTF8'}, set())
        self.assertEqual(observed['TEMP'], str(self.root))


class ModelBoundaryTests(unittest.TestCase):
    @staticmethod
    def load_plugin():
        stub = types.ModuleType('agent.context_compressor')
        class Base:
            def __init__(self, **kwargs):
                pass
        stub.ContextCompressor = Base
        with patch.dict(sys.modules, {'agent': types.ModuleType('agent'), 'agent.context_compressor': stub}):
            spec = importlib.util.spec_from_file_location('forge_test_plugin', Path(__file__).parents[1]/'integrations/hermes-plugin/context-forge/__init__.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        return module

    def test_malformed_model_response_is_controlled_and_private(self):
        for payload in [[], {'message': {'content': '[]'}}, {'message': {'content': None}}, {'message': {'content': 'mock-secret-not-json'}}]:
            response = contextlib.nullcontext(io.BytesIO(json.dumps(payload).encode()))
            with patch('context_forge.enrich.local_opener') as opener:
                opener.return_value.open.return_value = response
                result = enrich('source', 'test', 'http://127.0.0.1:11434/v1', retries=0)
            self.assertIsNone(result.data)
            self.assertTrue(result.error)
            self.assertNotIn('mock-secret', result.error)

    def test_external_model_url_rejected_without_network(self):
        with patch('context_forge.enrich.local_opener') as opener:
            result = enrich('private', 'test', 'https://hostile.invalid')
            opener.assert_not_called()
        self.assertEqual(result.attempts, 0)

    def test_capsule_schema_is_bounded(self):
        with self.assertRaises(InputError):
            validate_capsule({'execute_tool': 'send-secret'})

    def test_preflight_rejects_semantic_rewrites_and_malformed_shapes(self):
        module = self.load_plugin()
        engine = module.ContextForgeEngine()
        engine.min_chars = 0
        original = 'Preserve fact 42.'
        with patch.object(engine, '_record'), patch.object(engine, '_normalize', return_value=['malformed']):
            self.assertIsNone(engine.select_context([{'role': 'user', 'content': original}]))
        result = normalize_inline(original).to_dict()
        self.assertFalse(engine._verified_lossless_response(original, 'Fact 99', result))
        self.assertTrue(engine._verified_lossless_response(original, result['text'], result))

    def test_preflight_loads_protected_token_and_requires_revision_header(self):
        module = self.load_plugin()
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / 'service-token.txt'
            token_file.write_text(TEST_TOKEN, encoding='utf-8')
            with patch.dict(os.environ, {'CONTEXT_FORGE_TOKEN': '', 'CONTEXT_FORGE_TOKEN_FILE': str(token_file)}):
                engine = module.ContextForgeEngine()
        self.assertEqual(engine.token, TEST_TOKEN)
        payload = normalize_inline('  Fact 42.  ').to_dict()
        response = io.BytesIO(json.dumps(payload).encode())
        response.headers = {'X-Context-Forge-Security-Revision': '3'}
        with patch.object(module.request, 'build_opener') as build:
            build.return_value.open.return_value = contextlib.nullcontext(response)
            self.assertEqual(engine._normalize('  Fact 42.  ')['text'], 'Fact 42.')
            sent = build.return_value.open.call_args.args[0]
            self.assertEqual(sent.get_header('Authorization'), 'Bearer ' + TEST_TOKEN)
        stale = io.BytesIO(json.dumps(payload).encode())
        stale.headers = {'X-Context-Forge-Security-Revision': '2'}
        engine._cache.clear()
        with patch.object(module.request, 'build_opener') as build:
            build.return_value.open.return_value = contextlib.nullcontext(stale)
            self.assertIsNone(engine._normalize('  Fact 42.  '))


if __name__ == '__main__':
    unittest.main()
