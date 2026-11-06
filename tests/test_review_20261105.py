"""Regressions for the 2026-11-05 pre-release review."""
import hashlib
import json
import shutil
import unittest
import uuid
from pathlib import Path
from unittest import mock

from context_forge.pipeline import Pipeline, Store, now, safe_name
from context_forge.publication import PublicationBusyError, PublicationError, PublicationJournal, atomic_write_json
from context_forge.safety import InputError
from context_forge.worker_runtime import read_extraction_output


def manifest_for(payload: bytes) -> dict:
    return {"status": "completed", "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "publication": {"journal_schema_version": 1, "publication_id": "file"}}


class PublishedAccessTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / (".test-review-" + uuid.uuid4().hex[:8])
        (self.root / "output" / "batch").mkdir(parents=True)
        self.store = Store(self.root / "state.sqlite3")
        self.validator = mock.Mock()
        self.journal = PublicationJournal(self.store, self.root / "output", self.validator, now)
        stamp = now()
        with self.store.connect() as db:
            db.execute("INSERT INTO jobs(id,status,created_at,updated_at,source,total,options_json) VALUES(?,?,?,?,?,?,?)",
                       ("job", "completed", stamp, stamp, "fixture", 1, "{}"))
            db.execute("INSERT INTO files(id,job_id,source,status) VALUES(?,?,?,?)", ("file", "job", "fixture.txt", "running"))
        staging, self.target = self.journal.reserve("file", "job", Path("fixture.txt"), self.root / "output" / "batch", "fixture")
        staging.mkdir()
        (staging / "payload.txt").write_bytes(b"canonical")
        manifest = manifest_for(b"canonical")
        atomic_write_json(staging / "manifest.json", manifest)
        self.journal.staged("file")
        self.journal.validated("file", manifest)
        self.journal.publish("file")
        self.validator.reset_mock()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def state(self):
        return self.store.rows("SELECT state FROM publication_journal WHERE publication_id='file'")[0]["state"]

    def test_a_locked_file_is_not_revoked(self):
        self.validator.side_effect = PermissionError(32, "being used by another process")
        with mock.patch("context_forge.publication.LOCK_RETRY_SECONDS", 0):
            with self.assertRaises(PublicationBusyError):
                self.journal.check_available("file")
        self.assertEqual(self.state(), "PUBLISHED")
        self.assertEqual(self.store.rows("SELECT status FROM files WHERE id='file'")[0]["status"], "completed")
        self.validator.side_effect = None
        self.assertEqual(self.journal.check_available("file")["status"], "completed")

    def test_a_lock_that_clears_is_retried(self):
        self.validator.side_effect = [PermissionError(32, "busy"), None]
        with mock.patch("context_forge.publication.LOCK_RETRY_SECONDS", 0):
            self.assertEqual(self.journal.check_available("file")["status"], "completed")

    def test_a_missing_file_still_revokes(self):
        (self.target / "manifest.json").unlink()
        with self.assertRaises(PublicationError) as caught:
            self.journal.check_available("file")
        self.assertNotIsInstance(caught.exception, PublicationBusyError)
        self.assertEqual(self.state(), "RECONCILIATION_REQUIRED")

    def test_real_corruption_still_revokes(self):
        self.validator.side_effect = ValueError("hash mismatch")
        with self.assertRaises(PublicationError):
            self.journal.check_available("file")
        self.assertEqual(self.state(), "RECONCILIATION_REQUIRED")

    def test_full_validation_runs_once_until_a_file_changes(self):
        for _ in range(5):
            self.journal.check_available("file")
        self.assertEqual(self.validator.call_count, 1)
        (self.target / "payload.txt").write_bytes(b"changed!!")
        self.validator.side_effect = ValueError("hash mismatch")
        with self.assertRaises(PublicationError):
            self.journal.check_available("file")
        self.assertEqual(self.state(), "RECONCILIATION_REQUIRED")


class ExtractionReasonTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / (".test-review-" + uuid.uuid4().hex[:8])
        self.root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_worker_reason_reaches_the_caller(self):
        path = self.root / "out.json"
        path.write_text(json.dumps({"error": "PDF exceeds 2000 pages"}), encoding="utf-8")
        with self.assertRaisesRegex(InputError, "PDF exceeds 2000 pages"):
            read_extraction_output(path, 10000)

    def test_failed_document_leaves_no_copy_of_the_original(self):
        source = self.root / "input.pdf"
        source.write_bytes(b"%PDF-1.7\nthis is not really a pdf\n")
        pipeline = Pipeline(self.root, {"output_dir": str(self.root / "output"), "allowed_output_roots": [str(self.root / "output")]})
        settings = dict(pipeline.settings)
        settings["output_dir"] = str(self.root / "output")
        job_id = pipeline.create_job([source], "test", False)
        for thread in list(pipeline.threads):
            thread.join(120)
        job = pipeline.job(job_id)
        self.assertEqual(job["files"][0]["status"], "failed")
        self.assertNotIn("resource or output validation", job["files"][0]["error"])
        leftovers = [p for p in (self.root / "output").rglob("original*")]
        self.assertEqual(leftovers, [])
        self.assertEqual([p for p in (self.root / "output").rglob("*.staging")], [])


class NameTests(unittest.TestCase):
    def test_long_names_are_capped(self):
        self.assertLessEqual(len(safe_name("x" * 300)), 50)
        self.assertEqual(safe_name("..."), "document")


class EncryptedPdfTests(unittest.TestCase):
    def test_aes_pdf_with_an_empty_user_password_is_read(self):
        try:
            import cryptography  # noqa: F401
        except ImportError:
            self.skipTest("cryptography is in requirements-lock.txt but not in this environment")
        from pypdf import PdfWriter
        from context_forge.extractors import extract
        root = Path.cwd() / (".test-review-" + uuid.uuid4().hex[:8])
        root.mkdir()
        try:
            writer = PdfWriter()
            writer.add_blank_page(612, 792)
            writer.encrypt(user_password="", owner_password="owner-secret", algorithm="AES-256")
            path = root / "aes.pdf"
            with path.open("wb") as stream:
                writer.write(stream)
            extraction = extract(path)
            self.assertTrue(extraction.metadata.get("page_count", 1) >= 1)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
