import hashlib
import json
import shutil
import unittest
import uuid
from pathlib import Path

from context_forge.artifacts import ArtifactIntegrityError, build_artifacts, write_artifacts
from context_forge.extractors import Extraction, Section
from context_forge.pipeline import Store, now, validate_document_package
from context_forge.publication import PublicationError, PublicationJournal, atomic_write_json


def validate_fixture(directory: Path, manifest: dict) -> None:
    payload = (directory / "payload.txt").read_bytes()
    if hashlib.sha256(payload).hexdigest() != manifest["payload_sha256"]:
        raise ValueError("payload mismatch")


class PublicationJournalTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / (".test-publication-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.output = self.root / "output"
        self.output.mkdir()
        self.store = Store(self.root / "state.sqlite3")
        self.journal = PublicationJournal(self.store, self.output, validate_fixture, now)
        stamp = now()
        with self.store.connect() as db:
            db.execute("INSERT INTO jobs(id,status,created_at,updated_at,source,total,options_json) VALUES(?,?,?,?,?,?,?)",
                       ("job", "running", stamp, stamp, "fixture", 1, "{}"))
            db.execute("INSERT INTO files(id,job_id,source,status) VALUES(?,?,?,?)",
                       ("file", "job", "fixture.txt", "running"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def reserve(self):
        batch = self.output / "batch"
        batch.mkdir(exist_ok=True)
        return self.journal.reserve("file", "job", Path("fixture.txt"), batch, "fixture")

    @staticmethod
    def manifest(payload: bytes = b"canonical") -> dict:
        return {"status": "completed", "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "publication": {"journal_schema_version": 1, "publication_id": "file"}}

    def stage(self, *, write_manifest=True):
        staging, target = self.reserve()
        staging.mkdir()
        (staging / "payload.txt").write_bytes(b"canonical")
        manifest = self.manifest()
        self.journal.staged("file")
        if write_manifest:
            atomic_write_json(staging / "manifest.json", manifest)
        return staging, target, manifest

    def test_normal_publication_is_idempotent_and_selects_database_state(self):
        staging, target, manifest = self.stage()
        self.journal.validated("file", manifest)
        self.assertEqual(self.journal.publish("file"), manifest)
        self.assertEqual(self.journal.publish("file"), manifest)
        self.assertFalse(staging.exists())
        self.assertTrue(target.is_dir())
        row = self.store.rows("SELECT status,output_dir FROM files WHERE id='file'")[0]
        self.assertEqual(row, {"status": "completed", "output_dir": str(target)})
        self.assertEqual(self.store.rows("SELECT state FROM publication_journal")[0]["state"], "PUBLISHED")

    def test_startup_reconciles_crash_after_staged_manifest(self):
        _, target, _ = self.stage()
        outcome = self.journal.reconcile()
        self.assertEqual(outcome[0]["state"], "PUBLISHED")
        self.assertTrue(target.exists())
        self.assertEqual(self.store.rows("SELECT status FROM files WHERE id='file'")[0]["status"], "completed")

    def test_startup_repairs_database_after_directory_rename(self):
        staging, target, manifest = self.stage()
        self.journal.validated("file", manifest)
        staging.rename(target)  # Models a crash after atomic rename and before the DB transaction.
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET status='interrupted'")
            db.execute("UPDATE files SET status='interrupted'")
        self.journal.reconcile()
        self.assertEqual(self.store.rows("SELECT state FROM publication_journal")[0]["state"], "PUBLISHED")
        self.assertEqual(self.store.rows("SELECT status FROM files WHERE id='file'")[0]["status"], "completed")
        self.assertEqual(self.store.rows("SELECT status FROM jobs WHERE id='job'")[0]["status"], "completed")

    def test_conflicting_staging_and_target_are_retained(self):
        staging, target, manifest = self.stage()
        self.journal.validated("file", manifest)
        target.mkdir()
        with self.assertRaises(PublicationError):
            self.journal.publish("file")
        self.assertTrue(staging.exists())
        self.assertTrue(target.exists())
        self.assertEqual(self.store.rows("SELECT state FROM publication_journal")[0]["state"], "CONFLICT")

    def test_partial_reserved_tree_requires_reconciliation(self):
        staging, _, _ = self.stage(write_manifest=False)
        with self.store.connect() as db:
            db.execute("UPDATE publication_journal SET state='RESERVED'")
        self.journal.reconcile()
        self.assertTrue(staging.exists())
        self.assertEqual(self.store.rows("SELECT state FROM publication_journal")[0]["state"], "RECONCILIATION_REQUIRED")

    def test_staged_tree_without_manifest_requires_reconciliation(self):
        staging, _, _ = self.stage(write_manifest=False)
        self.assertTrue(staging.exists())
        self.journal.reconcile()
        self.assertEqual(self.store.rows("SELECT state FROM publication_journal")[0]["state"],
                         "RECONCILIATION_REQUIRED")

    def test_manifest_identity_mismatch_is_retained(self):
        _, _, manifest = self.stage()
        manifest["publication"]["publication_id"] = "different"
        atomic_write_json(self._row_path("staging_dir") / "manifest.json", manifest)
        self.journal.validated("file", manifest)
        with self.assertRaises(PublicationError):
            self.journal.publish("file")
        self.assertEqual(self.store.rows("SELECT state FROM publication_journal")[0]["state"],
                         "RECONCILIATION_REQUIRED")

    def _row_path(self, field: str) -> Path:
        return Path(self.store.rows("SELECT * FROM publication_journal")[0][field])

    def test_document_package_binds_the_archived_original(self):
        directory = self.root / "document"
        directory.mkdir()
        original = directory / "original.txt"
        original.write_bytes(b"source")
        source_sha256 = hashlib.sha256(b"source").hexdigest()
        artifacts = build_artifacts(Extraction([Section("document", "body")]), "source.txt", 6000,
                                    source_sha256=source_sha256)
        write_artifacts(directory, artifacts)
        manifest = {
            "format": ".txt", "source_name": "source.txt", "source_bytes": 6,
            "source_sha256": source_sha256,
            "artifact_schema_version": 2, "sections": len(artifacts.section_descriptors),
            "section_descriptors": artifacts.section_descriptors, "chunks": len(artifacts.chunks),
            "chunk_chars": artifacts.target_chars, "chunk_overlap_chars": artifacts.overlap_chars,
            "artifact_hashes": artifacts.artifact_hashes,
        }
        validate_document_package(directory, manifest)
        original.write_bytes(b"tamper")
        with self.assertRaises(ArtifactIntegrityError):
            validate_document_package(directory, manifest)


if __name__ == "__main__":
    unittest.main()
