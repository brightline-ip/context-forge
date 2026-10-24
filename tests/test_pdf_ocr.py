"""Synthetic fixtures only. No production corpus, service, credentials or URLs."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import unittest
import uuid
import json
import sqlite3
from contextlib import contextmanager, closing

from unittest import mock

from context_forge.extractors import Extraction, Section, extract_pdf_page
from context_forge import pdf_ocr
from context_forge.pdf_ocr import apply_pdf_ocr, recognize_pages
from context_forge.pipeline import quality_status

# Several tests exercise continuation across calls; cap one call at eight pages for them.
eight_per_call = mock.patch.object(pdf_ocr, 'MAX_PAGES_PER_CALL', 8)


class PdfOcrTests(unittest.TestCase):
    def test_table_leaders_select_layout_without_losing_plain_fallback(self):
        class Page:
            def extract_text(self, **kwargs):
                if kwargs.get('extraction_mode') == 'layout':
                    return '100  Secrecy ........ 100-1\n200  Priority ....... 200-1'
                return 'Secrecy ........ 100-1100\nPriority ....... 200-1200'
        text,mode=extract_pdf_page(Page())
        self.assertEqual(mode,'layout')
        self.assertIn('100-1',text)
        class Lossy(Page):
            def extract_text(self, **kwargs):
                return 'x' if kwargs.get('extraction_mode') == 'layout' else super().extract_text(**kwargs)
        self.assertEqual(extract_pdf_page(Lossy())[1],'layout_rejected')

    def test_quality_status_separates_processing_from_coverage_and_review(self):
        manifest={'format':'.pdf','metadata':{'ocr':{'pending_pages':[9],'unresolved_pages':[9]}}}
        self.assertEqual(quality_status(manifest)[0],'needs_ocr')
        manifest['metadata']['ocr']={'pending_pages':[],'recognized_pages':[1],'unresolved_pages':[]}
        self.assertEqual(quality_status(manifest)[0],'needs_review')
        manifest['metadata']={'ocr':{'pending_pages':[]},'pdf_layout_pages':[1]}
        self.assertEqual(quality_status(manifest)[0],'needs_review')
        manifest['metadata']={'ocr':{'pending_pages':[]},'pdf_layout_risk_pages':[1]}
        self.assertEqual(quality_status(manifest)[0],'needs_review')

    @contextmanager
    def checkpoint_fixture(self):
        parent=Path(__file__).resolve().parent
        root=parent/('oc-'+uuid.uuid4().hex[:8]);root.mkdir()
        try:
            source=root/'original.pdf';source.write_bytes(b'synthetic identity only, mocked parser')
            yield source,root/'progress.sqlite3'
        finally:
            if root.resolve().parent!=parent:raise RuntimeError('Unsafe fixture cleanup')
            shutil.rmtree(root)

    @eight_per_call
    def test_checkpoint_continues_without_repeating_pages_and_preserves_native(self):
        with self.checkpoint_fixture() as (source,checkpoint):
            calls=[]
            def native():return Extraction([Section('page:1',' EXACT native ')] + [Section(f'page:{n}','') for n in range(2,12)])
            def backend(path,ids):
                calls.append(ids)
                return [{'page':n,'text':f'Page {n}','status':'recognized'} for n in ids]
            first=apply_pdf_ocr(source,native(),backend,checkpoint)
            second=apply_pdf_ocr(source,native(),backend,checkpoint)
            third=apply_pdf_ocr(source,native(),backend,checkpoint)
            self.assertEqual(calls,[list(range(2,10)),[10,11]])
            self.assertEqual(first.metadata['ocr']['pending_pages'],[10,11])
            self.assertEqual(second.metadata['ocr']['pending_pages'],[])
            self.assertEqual(second.metadata['ocr']['recognized_pages'],list(range(2,12)))
            self.assertEqual(third.sections[0].text,' EXACT native ')
            self.assertEqual(second.metadata['ocr']['checkpoint_sha256'],third.metadata['ocr']['checkpoint_sha256'])
            self.assertEqual(first.metadata['ocr']['status'],'incomplete')
            self.assertEqual(second.metadata['ocr']['status'],'complete')
            self.assertEqual([r['status'] for r in second.metadata['ocr']['pages']],['native']+['recognized']*10)
            self.assertEqual(len(second.metadata['ocr']['config_sha256']),64)

    def test_one_call_chains_helper_batches_up_to_the_page_cap(self):
        calls=[]
        def backend(path,ids):
            calls.append(list(ids))
            return [{'page':n,'text':f'Page {n}','status':'recognized'} for n in ids]
        result=apply_pdf_ocr('unused.pdf',Extraction([Section(f'page:{n}','') for n in range(1,71)]),backend)
        self.assertEqual([len(c) for c in calls],[8]*8)
        self.assertEqual(result.metadata['ocr']['attempted_pages'],list(range(1,65)))
        self.assertEqual(result.metadata['ocr']['pending_pages'],[65,66,67,68,69,70])
        self.assertEqual(result.metadata['ocr']['status'],'incomplete')
        self.assertIn('65-70',' '.join(result.warnings))
        self.assertEqual([r['status'] for r in result.metadata['ocr']['pages'][63:66]],['recognized','pending','pending'])

    def test_time_budget_stops_new_batches_and_reports_pending(self):
        clock=iter([0.0]+[100.0]*10)
        with mock.patch.object(pdf_ocr.time,'monotonic',lambda:next(clock)):
            result=apply_pdf_ocr('unused.pdf',Extraction([Section(f'page:{n}','') for n in range(1,17)]),
                                 lambda p,ids:[{'page':n,'text':'t','status':'recognized'} for n in ids])
        self.assertEqual(result.metadata['ocr']['attempted_pages'],list(range(1,9)))
        self.assertEqual(result.metadata['ocr']['pending_pages'],list(range(9,17)))
        self.assertTrue(result.metadata['ocr']['continuation_available'])

    def test_failure_in_a_later_batch_keeps_earlier_batches_and_names_failed_pages(self):
        def backend(path,ids):
            if ids[0]>8: raise RuntimeError('helper died')
            return [{'page':n,'text':'t','status':'recognized'} for n in ids]
        result=apply_pdf_ocr('unused.pdf',Extraction([Section(f'page:{n}','') for n in range(1,21)]),backend)
        ocr=result.metadata['ocr']
        self.assertEqual(ocr['recognized_pages'],list(range(1,9)))
        self.assertEqual(ocr['failed_pages'],list(range(9,17)))
        self.assertEqual(ocr['pending_pages'],list(range(17,21)))
        self.assertEqual(ocr['status'],'unavailable_or_failed')
        self.assertTrue(any('failed for pages 9-16' in w for w in result.warnings))

    @eight_per_call
    def test_interrupted_batch_rolls_back_but_keeps_prior_batch(self):
        with self.checkpoint_fixture() as (source,checkpoint):
            def native():return Extraction([Section(f'page:{n}','') for n in range(1,10)])
            def backend(path,ids):return [{'page':n,'text':str(n),'status':'recognized'} for n in ids]
            apply_pdf_ocr(source,native(),backend,checkpoint)
            def interrupted(*args):raise KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):apply_pdf_ocr(source,native(),interrupted,checkpoint)
            result=apply_pdf_ocr(source,native(),backend,checkpoint)
            self.assertEqual(result.metadata['ocr']['current_batch_pages'],[9])
            self.assertEqual(result.metadata['ocr']['recognized_pages'],list(range(1,10)))

    def test_failed_blank_pages_do_not_loop(self):
        with self.checkpoint_fixture() as (source,checkpoint):
            def native():return Extraction([Section('page:1',''),Section('page:2','')])
            def backend(*args):return [{'page':1,'text':'','status':'recognized'},{'page':2,'text':'','status':'failed'}]
            apply_pdf_ocr(source,native(),backend,checkpoint)
            result=apply_pdf_ocr(source,native(),lambda *args:self.fail('Must not retry'),checkpoint)
            self.assertEqual(result.metadata['ocr']['unresolved_pages'],[1,2])
            self.assertEqual(result.metadata['ocr']['failed_pages'],[2])
            self.assertEqual(result.metadata['ocr']['blank_pages'],[1])
            self.assertEqual(result.metadata['ocr']['status'],'pages_unresolved')
            self.assertFalse(result.metadata['ocr']['continuation_available'])

    def test_checkpoint_from_other_config_is_rebuilt_not_trusted(self):
        with self.checkpoint_fixture() as (source,checkpoint):
            native=lambda:Extraction([Section('page:1','')])
            apply_pdf_ocr(source,native(),lambda *a:[{'page':1,'text':'OLD','status':'recognized'}],checkpoint)
            with closing(sqlite3.connect(checkpoint)) as db, db:
                state=json.loads(db.execute('SELECT payload FROM progress').fetchone()[0]);state['identity']['config_sha256']='0'*64
                db.execute('UPDATE progress SET payload=?',(json.dumps(state),))
            calls=[]
            def backend(*a):calls.append(1);return [{'page':1,'text':'NEW','status':'recognized'}]
            result=apply_pdf_ocr(source,native(),backend,checkpoint)
            self.assertEqual((calls,result.sections[0].text),([1],'NEW'))
            self.assertTrue(any('earlier configuration' in w for w in result.warnings))

    @unittest.skipUnless(os.name=='nt','Windows path limit')
    def test_overlong_checkpoint_path_fails_with_a_named_error(self):
        with self.checkpoint_fixture() as (source,checkpoint):
            long_path=checkpoint.parent/('x'*max(1,270-len(str(checkpoint.parent)))+'.sqlite3')
            with self.assertRaises(pdf_ocr.OcrCheckpointPathError):
                apply_pdf_ocr(source,Extraction([Section('page:1','')]),lambda *a:[],long_path)

    def test_changed_source_and_corrupt_checkpoint_fail_closed(self):
        with self.checkpoint_fixture() as (source,checkpoint):
            def native():return Extraction([Section('page:1','')])
            backend=lambda *args:[{'page':1,'text':'OCR','status':'recognized'}]
            apply_pdf_ocr(source,native(),backend,checkpoint)
            original=source.read_bytes();source.write_bytes(b'changed')
            with self.assertRaises(ValueError):apply_pdf_ocr(source,native(),backend,checkpoint)
            source.write_bytes(original)
            with closing(sqlite3.connect(checkpoint)) as db, db:
                state=json.loads(db.execute('SELECT payload FROM progress').fetchone()[0]);state['rows'][0]['page']=999
                db.execute('UPDATE progress SET payload=?',(json.dumps(state),))
            with self.assertRaises(ValueError):apply_pdf_ocr(source,native(),backend,checkpoint)

    def test_concurrent_checkpoint_is_not_overwritten(self):
        with self.checkpoint_fixture() as (source,checkpoint):
            native=lambda:Extraction([Section('page:1','')])
            apply_pdf_ocr(source,native(),lambda *a:[{'page':1,'text':'OCR','status':'recognized'}],checkpoint)
            with closing(sqlite3.connect(checkpoint)) as db, db:
                db.execute('BEGIN IMMEDIATE')
                with self.assertRaises(sqlite3.OperationalError):
                    apply_pdf_ocr(source,native(),lambda *a:self.fail('Lock must prevent OCR'),checkpoint)

    def test_native_text_never_calls_ocr(self):
        source=Extraction([Section('page:1','  native\ntext  ')])
        def forbidden(*args): self.fail('OCR must not replace native text')
        result=apply_pdf_ocr('unused.pdf',source,backend=forbidden)
        self.assertEqual(result.sections[0].text,'  native\ntext  ')
        self.assertEqual(result.metadata['ocr']['status'],'not_needed')

    def test_mixed_document_has_explicit_derivative_and_native_preserved(self):
        source=Extraction([Section('page:1','Native exact text'),Section('page:2','')])
        result=apply_pdf_ocr('unused.pdf',source,backend=lambda p,ids:[{'page':2,'text':'OCR text','status':'recognized'}])
        self.assertEqual(result.sections[0].text,'Native exact text')
        self.assertEqual(result.sections[1].text,'OCR text')
        self.assertEqual(result.metadata['ocr']['recognized_pages'],[2])
        self.assertFalse(result.metadata['ocr']['recognition_verified'])
        self.assertTrue(result.warnings)

    @eight_per_call
    def test_page_cap_and_empty_ocr_never_claim_complete(self):
        source=Extraction([Section(f'page:{n}','') for n in range(1,11)])
        result=apply_pdf_ocr('unused.pdf',source,backend=lambda p,ids:[{'page':n,'text':'','status':'recognized'} for n in ids])
        self.assertEqual(result.metadata['ocr']['attempted_pages'],list(range(1,9)))
        self.assertEqual(result.metadata['ocr']['unresolved_pages'],list(range(1,11)))
        self.assertEqual(result.metadata['ocr']['blank_pages'],list(range(1,9)))
        self.assertTrue(any('pending' in w for w in result.warnings))
        self.assertNotEqual(result.metadata['ocr']['status'],'complete')

    def test_failed_and_malformed_results_preserve_original_and_redact_errors(self):
        def failure(*args): raise subprocess.TimeoutExpired('secret-source-value',60)
        for backend in [failure,lambda *a:[{'page':99,'text':'untrusted','status':'recognized'}],
                        lambda *a:[{'page':1,'text':'x'*200001,'status':'recognized'}]]:
            result=apply_pdf_ocr('unused.pdf',Extraction([Section('page:1','')]),backend=backend)
            self.assertEqual(result.sections[0].text,'')
            self.assertEqual(result.metadata['ocr']['status'],'unavailable_or_failed')
            self.assertNotIn('secret-source-value',str(result))

    @unittest.skipUnless(os.name=='nt','Windows local OCR backend')
    def test_nine_page_pipeline_continuation_is_idempotent_and_versioned(self):
        from PIL import Image, ImageDraw, ImageFont
        from context_forge.pipeline import Pipeline
        import time
        with self.checkpoint_fixture() as (source,_):
            image=Image.new('RGB',(1200,700),'white')
            ImageDraw.Draw(image).text((80,100),'SYNTHETIC LONG PDF 18123456',fill='black',font=ImageFont.truetype(r'C:\Windows\Fonts\arial.ttf',46))
            image.save(source,'PDF',resolution=150,save_all=True,append_images=[image]*65)  # 66 pages: cap is 64 per call
            digest=hashlib.sha256(source.read_bytes()).hexdigest()
            root=source.parent
            pipeline=Pipeline(root,{'output_dir':str(root/'output')})
            def wait(job_id):
                deadline=time.monotonic()+120
                while time.monotonic()<deadline:
                    job=pipeline.job(job_id)
                    if job['status'] not in {'queued','running'}:return job
                    time.sleep(.05)
                self.fail('Continuation job did not finish')
            try:
                first=wait(pipeline.create_job([source],'synthetic long PDF',False,normalization_mode='lossless'))
                self.assertEqual(first['status'],'completed',first)
                first_file=first['files'][0];old_path=Path(first_file['output_dir'])/'normalized.md'
                old_hash=hashlib.sha256(old_path.read_bytes()).hexdigest()
                # One call attempts at most 64 pages and starts no new helper batch after a
                # 75-second wall-clock budget (a subprocess, so the test cannot patch it). On a
                # slow or loaded machine the first call may therefore stop earlier than page 64.
                # Assert the invariants that hold either way, not a fixed split point.
                pending=first_file['ocr']['pending_pages']
                self.assertTrue(pending and pending==list(range(pending[0],67)) and 9<=pending[0]<=65,pending)
                self.assertEqual(first_file['ocr']['attempted_pages'],list(range(1,pending[0])))
                self.assertEqual(first_file['quality_status'],'needs_ocr')
                self.assertIn(f'{len(pending)} pending',first_file['ocr_summary'])
                job_id=pipeline.continue_ocr(first_file['id'])
                self.assertEqual(pipeline.continue_ocr(first_file['id']),job_id)
                second=wait(job_id);self.assertEqual(second['status'],'completed',second)
                item=second['files'][0]
                self.assertEqual(item['ocr']['current_batch_pages'],pending[:len(item['ocr']['current_batch_pages'])])
                self.assertEqual(item['ocr']['current_batch_pages'][:1],pending[:1])
                self.assertEqual(pipeline.continue_ocr(first_file['id']),job_id)
                self.assertNotEqual(first_file['output_dir'],item['output_dir'])
                for _ in range(6):  # extra continuations only if the machine was slow
                    if not item['ocr']['pending_pages']:break
                    item=wait(pipeline.continue_ocr(item['id']))['files'][0]
                self.assertEqual(item['ocr']['recognized_pages'],list(range(1,67)))
                self.assertEqual(item['ocr']['pending_pages'],[])
                self.assertEqual(item['ocr']['status'],'complete')
                self.assertEqual(len(item['ocr']['pages']),66)
                self.assertEqual(item['ocr']['pages'][0]['render']['dpi'],200)
                with self.assertRaises(ValueError):pipeline.continue_ocr(item['id'])
                self.assertEqual(hashlib.sha256(old_path.read_bytes()).hexdigest(),old_hash)
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),digest)
            finally:
                pipeline.close()

    def test_helper_launch_bypasses_execution_policy_for_the_fixed_helper_only(self):
        import tempfile
        captured = {}
        def fake_popen(command, **kwargs):
            captured['command'] = command
            raise RuntimeError('stop before running the helper')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.pdf'; source.write_bytes(b'%PDF-1.4')
            with mock.patch.object(pdf_ocr.os, 'name', 'nt'), mock.patch.object(pdf_ocr.subprocess, 'Popen', fake_popen):
                with self.assertRaises(RuntimeError):
                    recognize_pages(source, [1])
        command = captured['command']
        self.assertEqual(command[command.index('-ExecutionPolicy') + 1], 'Bypass')
        self.assertLess(command.index('-ExecutionPolicy'), command.index('-File'))
        self.assertEqual(command[command.index('-File') + 1], str(pdf_ocr.HELPER))
        self.assertNotIn('-Command', command)

    @unittest.skipUnless(os.name=='nt','Windows local OCR backend')
    def test_real_windows_ocr_preserves_original(self):
        from PIL import Image, ImageDraw, ImageFont
        parent=Path(__file__).resolve().parent
        root=parent/('of-'+uuid.uuid4().hex[:8]);root.mkdir()
        try:
            original=root/'scanned.pdf'
            image=Image.new('RGB',(1600,900),'white')
            draw=ImageDraw.Draw(image);font=ImageFont.truetype(r'C:\Windows\Fonts\arial.ttf',54)
            draw.multiline_text((90,100),'PUBLIC PATENT TEST\nApplication 18123456\nSynthetic OCR fixture',font=font,fill='black',spacing=35)
            image.save(original,'PDF',resolution=150)
            before=hashlib.sha256(original.read_bytes()).hexdigest()
            rows=recognize_pages(original,[1])
            self.assertEqual(rows[0]['status'],'recognized')
            self.assertIn('PUBLIC PATENT TEST',rows[0]['text'])
            self.assertIn('18123456',rows[0]['text'])
            self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(),before)
            self.assertEqual(list(root.iterdir()),[original])
        finally:
            if root.resolve().parent!=parent:raise RuntimeError('Fixture cleanup outside test directory')
            shutil.rmtree(root)


if __name__=='__main__':unittest.main()
