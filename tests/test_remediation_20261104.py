import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from context_forge import ocr_crosscheck
from context_forge.audit import ServiceLog, append_event
from context_forge.pipeline import Pipeline, quality_status, quality_tier
from context_forge.worker_runtime import WorkerRuntimeError, _wait_while_awake


class FrozenThenRunning:
    """A process that never exits, on a clock that jumps as if the machine slept."""
    def __init__(self, clock, jump_at=2, jump=600.0):
        self.clock, self.calls, self.jump_at, self.jump = clock, 0, jump_at, jump

    def wait(self, timeout=None):
        self.calls += 1
        self.clock['t'] += self.jump if self.calls == self.jump_at else timeout
        import subprocess
        raise subprocess.TimeoutExpired('worker', timeout)


class SleepSafeTimeoutTests(unittest.TestCase):
    def test_time_asleep_does_not_use_up_the_limit(self):
        clock = {'t': 0.0}
        process = FrozenThenRunning(clock)
        import subprocess
        with patch('context_forge.worker_runtime.time.monotonic', lambda: clock['t']):
            with self.assertRaises(subprocess.TimeoutExpired):
                _wait_while_awake(process, 20)
        self.assertGreater(clock['t'], 600, 'the 600 s jump counted as at most a few seconds')

    def test_a_real_overrun_still_times_out(self):
        import subprocess
        code = 'import time;time.sleep(30)'
        process = subprocess.Popen([sys.executable, '-c', code])
        try:
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                _wait_while_awake(process, 1)
            self.assertLess(time.monotonic() - started, 10)
        finally:
            process.kill()
            process.wait()

    def test_timeout_is_retried_once_and_the_retry_can_succeed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.txt'
            source.write_text('mock data')
            pipeline = Pipeline(root, {'output_dir': str(root / 'output')})
            calls = []

            def flaky(python, arguments, cwd, temp_dir, timeout):
                calls.append(1)
                if len(calls) == 1:
                    raise WorkerRuntimeError('timeout')
                Path(arguments[1]).write_text(json.dumps({'sections': [{'locator': 'document', 'text': 'mock data', 'heading': ''}], 'metadata': {}, 'warnings': []}), encoding='utf-8')

                class Result:
                    def metadata(self):
                        return {}
                return Result()
            with patch('context_forge.pipeline.run_extraction_worker', side_effect=flaky):
                job_id = pipeline.create_job([source], 'test', False)
                pipeline.close()
            job = pipeline.job(job_id)
            self.assertEqual(len(calls), 2)
            self.assertEqual(job['files'][0]['status'], 'completed')
            self.assertTrue(any('retry' in w for w in job['files'][0]['warnings']))
            events = (root / 'data' / 'intake-audit.jsonl').read_text(encoding='utf-8').splitlines()
            self.assertEqual(json.loads(events[0])['event'], 'extraction_timeout_retried')

    def test_timeout_twice_fails_visibly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.txt'
            source.write_text('mock data')
            pipeline = Pipeline(root, {'output_dir': str(root / 'output')})
            with patch('context_forge.pipeline.run_extraction_worker', side_effect=WorkerRuntimeError('timeout')) as worker:
                job_id = pipeline.create_job([source], 'test', False)
                pipeline.close()
            self.assertEqual(worker.call_count, 2)
            self.assertIn('120-second', pipeline.job(job_id)['files'][0]['error'])


class QueueTests(unittest.TestCase):
    def test_third_job_waits_as_queued_then_runs(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.txt'
            source.write_text('mock data')
            pipeline = Pipeline(root, {'output_dir': str(root / 'output')})
            pipeline.slots.acquire()
            pipeline.slots.acquire()
            job_id = pipeline.create_job([source], 'test', False)
            time.sleep(0.2)
            self.assertEqual(pipeline.job(job_id)['status'], 'queued')
            pipeline.slots.release()
            pipeline.slots.release()
            pipeline.close()
            job = pipeline.job(job_id)
            self.assertEqual(job['status'], 'completed')
            self.assertIn('processing_seconds', json.loads(pipeline.store.rows('SELECT manifest_json FROM files WHERE job_id=?', (job_id,))[0]['manifest_json']))
            self.assertEqual(threading.active_count() >= 1, True)


class IntakeRulesTests(unittest.TestCase):
    def test_credential_shaped_paths_are_refused_anywhere_under_a_root(self):
        from context_forge.safety import InputError, input_path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for name in ('Other Project/security/notes.txt', 'Other Project/server.pem', 'x/service-token.txt', 'x/backup-key.dpapi'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('mock')
                with self.assertRaises(InputError):
                    input_path(str(path), [str(root)])
            fine = root / 'Other Project' / 'brief.docx'
            fine.write_text('mock')
            self.assertEqual(input_path(str(fine), [str(root)]), fine)


class AuditTests(unittest.TestCase):
    def test_append_rotates_and_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'audit.jsonl'
            append_event(path, {'event': 'a'}, max_bytes=10)
            append_event(path, {'event': 'b'}, max_bytes=10)
            self.assertTrue(path.with_suffix('.jsonl.1').exists())
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['event'], 'b')
            append_event(Path(tmp) / 'missing-dir' / 'x' / 'audit.jsonl', {'event': 'c'})


class ServiceLogTests(unittest.TestCase):
    def test_output_is_kept_and_a_closed_stream_does_not_break_it(self):
        import io
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'service.log'
            stream = io.StringIO()
            log = ServiceLog(path, stream)
            log.write('{"event": "x"}\n')
            log.write('\n')
            stream.close()
            log.write('after close\n')
            log.flush()
            lines = path.read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(lines), 2)
            self.assertTrue(lines[0].endswith('{"event": "x"}'))


class QualityTierTests(unittest.TestCase):
    def tier(self, **ocr):
        return quality_tier({'format': '.pdf', 'metadata': {'ocr': ocr}})

    def test_tiers_separate_clean_ocr_from_real_problems(self):
        self.assertEqual(quality_tier({'format': '.docx', 'metadata': {}}), 'ready')
        self.assertEqual(self.tier(pending_pages=[3], recognized_pages=[1]), 'incomplete')
        self.assertEqual(self.tier(unresolved_pages=[2], recognized_pages=[1]), 'ocr_problems')
        self.assertEqual(self.tier(recognized_pages=[1]), 'ocr_unverified')
        self.assertEqual(quality_tier({'format': '.pdf', 'metadata': {'pdf_layout_pages': [1]}}), 'layout_review')

    def test_numeric_disagreement_outranks_clean_ocr_and_is_a_reason(self):
        manifest = {'format': '.pdf', 'metadata': {'ocr': {'recognized_pages': [1]}, 'ocr_numeric_check': {'disputed_pages': [{'page': 1, 'conflicts': [['7/2025', '10/17/2025']]}]}}}
        self.assertEqual(quality_tier(manifest), 'ocr_numbers_disputed')
        status, reasons = quality_status(manifest)
        self.assertEqual(status, 'needs_review')
        self.assertIn('ocr_numeric_disagreement', reasons)

    def test_a_number_one_engine_simply_missed_does_not_flag_the_document(self):
        manifest = {'format': '.pdf', 'metadata': {'ocr': {'recognized_pages': [1]}, 'ocr_numeric_check': {'disputed_pages': [{'page': 1, 'windows_only': ['4471'], 'second_engine_only': [], 'conflicts': []}]}}}
        self.assertEqual(quality_tier(manifest), 'ocr_unverified')
        self.assertNotIn('ocr_numeric_disagreement', quality_status(manifest)[1])

    def test_existing_status_values_are_unchanged(self):
        self.assertEqual(quality_status({'format': '.pdf', 'metadata': {'ocr': {'recognized_pages': [1]}}}), ('needs_review', ['unverified_ocr_text']))


class CrossCheckTests(unittest.TestCase):
    def test_the_real_misread_is_flagged_both_ways(self):
        windows_only, second_only = ocr_crosscheck.compare_page('FILING DATE\n1 Oil 7/2025\n19/362,183', 'FILING DATE\n10/17/2025\n19/362,183')
        self.assertEqual(windows_only, ['7/2025'])
        self.assertEqual(second_only, ['10/17/2025'])

    def test_conflicts_pair_similar_numbers_and_ignore_unrelated_ones(self):
        self.assertEqual(ocr_crosscheck.find_conflicts(['7/2025'], ['10/17/2025', '999']), [['7/2025', '10/17/2025']])
        self.assertEqual(ocr_crosscheck.find_conflicts(['1,453'], ['11,453', '1.85']), [['1,453', '11,453']])
        self.assertEqual(ocr_crosscheck.find_conflicts(['4471'], []), [])
        self.assertEqual(ocr_crosscheck.find_conflicts(['12345'], ['98760']), [])

    def test_spacing_and_punctuation_differences_are_not_disputes(self):
        self.assertEqual(ocr_crosscheck.compare_page('App 19/362, 183 filed 2026.', 'App 19/362,183 filed 2026'), ([], []))

    def test_short_numbers_are_ignored(self):
        self.assertEqual(ocr_crosscheck.compare_page('claims 1, 2', 'claims 7, 8'), ([], []))

    def test_summary_reports_pages_not_reached(self):
        result = ocr_crosscheck.summarize({1: 'no. 12345', 2: 'no. 777'}, {1: 'no. 12345'})
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['not_checked_pages'], [2])
        self.assertEqual(result['disputed_pages'], [])
        self.assertEqual(ocr_crosscheck.summarize({1: 'x'}, {})['status'], 'failed')

    def test_cached_pages_are_not_read_again(self):
        from context_forge.extractors import Extraction, Section
        extraction = Extraction([Section('page:1', 'no. 12345', 'Page 1'), Section('page:2', 'no. 777', 'Page 2')],
                                {'ocr': {'recognized_pages': [1, 2]}}, [])
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / 'nested' / 'digest.json'
            ocr_crosscheck._save_cache(cache, {1: 'no. 12345', 2: 'no. 777'})
            with patch.object(ocr_crosscheck, 'tesseract_path', return_value=Path('t.exe')), \
                    patch.object(ocr_crosscheck, '_version', return_value=None), \
                    patch.object(ocr_crosscheck, 'run_extraction_worker') as worker, \
                    patch.object(ocr_crosscheck.os, 'name', 'nt'):
                result = ocr_crosscheck.check_recognized_pages('s.pdf', extraction, tmp, cache_path=cache)
            worker.assert_not_called()
            self.assertEqual(result['status'], 'complete')

    def test_progress_saves_each_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            def render(source, pages, scratch):
                for page in pages:
                    (Path(scratch) / f'page-{page}.png').write_bytes(b'x')
            saved = []
            ocr_crosscheck.read_pages('s.pdf', list(range(1, 10)), 't', tmp, render=render, recognize=lambda t, i: 'text', progress=lambda texts: saved.append(len(texts)))
            self.assertEqual(saved, [8, 9])

    def test_a_page_that_fails_is_left_out_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            def render(source, pages, scratch):
                for page in pages:
                    (Path(scratch) / f'page-{page}.png').write_bytes(b'x')

            def recognize(tesseract, image):
                if 'page-2' in image.name:
                    raise RuntimeError('boom')
                return 'text'
            texts = ocr_crosscheck.read_pages('s.pdf', [1, 2, 3], 'tesseract', tmp, render=render, recognize=recognize)
            self.assertEqual(sorted(texts), [1, 3])
            self.assertEqual(os.listdir(tmp), [])

    def test_missing_engine_is_reported_without_failing(self):
        from context_forge.extractors import Extraction, Section
        extraction = Extraction([Section('page:1', 'no. 12345', 'Page 1')], {'ocr': {'recognized_pages': [1]}}, [])
        with tempfile.TemporaryDirectory() as tmp, patch.object(ocr_crosscheck, 'tesseract_path', return_value=None):
            self.assertEqual(ocr_crosscheck.check_recognized_pages('s.pdf', extraction, tmp)['status'], 'unavailable')
            self.assertEqual(os.listdir(tmp), [])

    def test_no_ocr_pages_means_no_check(self):
        from context_forge.extractors import Extraction
        self.assertIsNone(ocr_crosscheck.check_recognized_pages('s.pdf', Extraction([], {}, []), '.'))


if __name__ == '__main__':
    unittest.main()
