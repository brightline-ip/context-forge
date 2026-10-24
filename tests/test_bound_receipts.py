import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from context_forge.server import App, ExclusiveHTTPServer, handler_factory
from context_forge.pipeline import Pipeline
from context_forge.safety import InputError

TEST_TOKEN = 'context-forge-bound-test-token-0000000000000000'


class BoundReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='forge-bound-')
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.txt'
        self.source.write_text('    Original fact\nRepeated fact\nRepeated fact\n', encoding='utf-8')
        self.sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.app = App(TEST_TOKEN, self.root)
        self.server = ExclusiveHTTPServer(('127.0.0.1', 0), handler_factory(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.app.close(); self.temp.cleanup()

    def request(self, method, route, body=None, auth=True):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            headers = {'Content-Type': 'application/json'}
            if auth:
                headers['Authorization'] = 'Bearer ' + TEST_TOKEN
            conn.request(method, route, None if body is None else json.dumps(body), headers)
            r = conn.getresponse()
            return r.status, json.loads(r.read())
        finally:
            conn.close()

    def body(self, key='a'*64):
        return {'path': str(self.source), 'expected_sha256': self.sha, 'operation_key': key}

    def test_http_accept_lookup_replay_and_completed_original(self):
        route = '/api/bound-receipt?operation_key='+'a'*64
        self.assertFalse(self.request('GET', route)[1]['found'])
        status, first = self.request('POST', '/api/jobs/bound-path', self.body())
        self.assertEqual(status, 202)
        self.app.pipeline.close()
        receipt = self.request('GET', route)[1]
        self.assertEqual(receipt['job_id'], first['job_id'])
        self.assertEqual(set(receipt), {'found','operation_key','job_id','request'})
        self.assertEqual(receipt['request'], {'path': str(self.source.resolve()), 'expected_sha256': self.sha, 'recursive': False, 'enrich': False, 'normalization_mode': 'lossless'})
        before = self.app.pipeline.store.rows('SELECT * FROM jobs')
        self.assertEqual(self.request('POST', '/api/jobs/bound-path', self.body())[1], first)
        self.assertEqual(self.app.pipeline.store.rows('SELECT * FROM jobs'), before)
        job = self.app.pipeline.job(first['job_id'])
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['files'][0]['source_sha256'], self.sha)
        self.assertFalse(job['options']['enrich'])
        reopened = Pipeline(self.root, self.app.settings)
        try:
            self.assertEqual(reopened.bound_receipt('a'*64), receipt)
        finally:
            reopened.close()

    def test_auth_strict_fields_and_no_folder_or_hash_substitution(self):
        self.assertEqual(self.request('POST', '/api/jobs/bound-path', self.body(), False)[0], 401)
        self.assertEqual(self.request('GET', '/api/bound-receipt?operation_key='+'a'*64, auth=False)[0], 401)
        for change in [{'recursive': True}, {'enrich': True}, {'expected_sha256': 'b'*64}, {'path': str(self.root)}, {'operation_key': 'short'}]:
            self.assertEqual(self.request('POST', '/api/jobs/bound-path', self.body() | change)[0], 400)
        for query in ['operation_key=', 'operation_key=short', 'operation_key='+'a'*64+'&extra=x', 'operation_key='+'a'*64+'&operation_key='+'a'*64]:
            self.assertEqual(self.request('GET', '/api/bound-receipt?'+query)[0], 400)
        security = self.root / 'security'
        security.mkdir()
        token_file = security / 'service-token.txt'
        token_file.write_text(TEST_TOKEN, encoding='utf-8')
        self.assertEqual(self.request('POST', '/api/jobs/bound-path', self.body() | {'path': str(token_file)})[0], 400)
        self.assertEqual(self.app.pipeline.store.rows('SELECT * FROM jobs'), [])

    def test_concurrent_same_key_one_job_and_changed_request_rejected(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda _: self.app.pipeline.create_bound_job(self.source, self.sha, 'a'*64), range(4)))
        self.assertEqual(len(set(ids)), 1)
        with self.assertRaises(InputError):
            self.app.pipeline.create_bound_job(self.source, 'b'*64, 'a'*64)
        self.app.pipeline.close()
        self.assertEqual(len(self.app.pipeline.store.rows('SELECT * FROM jobs')), 1)

    def test_binding_failure_rolls_back_job_and_file_rows(self):
        self.app.pipeline.store.execute("CREATE TRIGGER reject_binding BEFORE INSERT ON bound_requests BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(Exception):
            self.app.pipeline.create_bound_job(self.source, self.sha, 'a'*64)
        self.assertEqual(self.app.pipeline.store.rows('SELECT * FROM jobs'), [])
        self.assertEqual(self.app.pipeline.store.rows('SELECT * FROM files'), [])
        self.assertFalse(self.app.pipeline.bound_receipt('a'*64)['found'])

    def test_worker_start_failure_retains_receipt_and_never_restarts_on_replay(self):
        with patch('context_forge.pipeline.threading.Thread.start', side_effect=RuntimeError('synthetic worker start failure')):
            with self.assertRaises(RuntimeError):
                self.app.pipeline.create_bound_job(self.source, self.sha, 'a'*64)
        receipt = self.app.pipeline.bound_receipt('a'*64)
        self.assertTrue(receipt['found'])
        self.assertEqual(self.app.pipeline.job(receipt['job_id'])['status'], 'failed')
        self.assertEqual(self.app.pipeline.create_bound_job(self.source, self.sha, 'a'*64), receipt['job_id'])

    def test_changed_copy_rejected_before_extractor(self):
        self.source.write_text('changed', encoding='utf-8')
        output = self.root / 'process-test'; output.mkdir()
        with patch('context_forge.pipeline.run_extraction_worker') as extractor:
            with self.assertRaises(InputError):
                self.app.pipeline._process_one(self.source, output, False, 'lossless', expected_sha256=self.sha)
            extractor.assert_not_called()


if __name__ == '__main__':
    unittest.main()
