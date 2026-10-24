import shutil
import json
import time
import unittest
import uuid
from pathlib import Path

from context_forge.pipeline import Pipeline
from context_forge.safety import InputError


class PublicationAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path.cwd() / (".test-forge-availability-" + uuid.uuid4().hex)
        (self.root / "output").mkdir(parents=True)
        self.source = self.root / "source.txt"
        self.source.write_text("integrity availability fixture", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def pipeline(self):
        return Pipeline(self.root, {"output_dir": str(self.root / "output"), "chunk_chars": 6000})

    def completed(self, pipeline):
        job_id = pipeline.create_job([self.source], "fixture", False, normalization_mode="lossless")
        for _ in range(200):
            job = pipeline.job(job_id)
            if job["status"] not in {"queued", "running"}:
                break
            time.sleep(.02)
        pipeline.close()
        self.assertEqual(job["status"], "completed")
        return job_id, job["files"][0]

    def test_corrupt_selected_output_is_revoked_and_stays_unavailable_after_restart(self):
        pipeline = self.pipeline()
        job_id, item = self.completed(pipeline)
        output = Path(item["output_dir"])
        (output / "normalized.md").write_text("corrupt", encoding="utf-8")
        with self.assertRaises(InputError):
            pipeline.read_document(item["id"])
        job = pipeline.job(job_id)
        self.assertEqual(job["files"][0]["status"], "reconciliation_required")
        self.assertEqual((job["completed"], job["failed"], job["status"]), (0, 1, "completed_with_errors"))
        restarted = self.pipeline()
        with self.assertRaises(InputError):
            restarted.read_document(item["id"])
        restarted_job = restarted.job(job_id)
        self.assertEqual(restarted_job["files"][0]["status"], "reconciliation_required")
        self.assertNotIn("quality_status", restarted_job["files"][0])

    def test_missing_canonical_files_fail_closed_through_pipeline_api(self):
        for name in ("manifest.json", "sections.json", "chunks.jsonl", "normalized.md", "original.txt"):
            with self.subTest(name=name):
                shutil.rmtree(self.root, ignore_errors=True)
                (self.root / "output").mkdir(parents=True)
                self.source = self.root / "source.txt"
                self.source.write_text("fixture", encoding="utf-8")
                pipeline = self.pipeline()
                _, item = self.completed(pipeline)
                (Path(item["output_dir"]) / name).unlink()
                with self.assertRaises(InputError):
                    pipeline.read_document(item["id"])

    def test_multifile_finalization_preserves_concurrent_revocation(self):
        second = self.root / "second.txt"
        second.write_text("second fixture", encoding="utf-8")
        pipeline = self.pipeline()
        original = pipeline._process_one
        first = {}

        def interleaved(source, *args, **kwargs):
            if first:
                (Path(first["output_dir"]) / "normalized.md").write_text("corrupt", encoding="utf-8")
                with self.assertRaises(Exception):
                    pipeline.publications.check_available(first["id"])
            result = original(source, *args, **kwargs)
            if not first:
                first.update(id=kwargs["publication_id"], output_dir=result["output_dir"])
            return result

        pipeline._process_one = interleaved
        job_id = pipeline.create_job([self.source, second], "fixture", False, normalization_mode="lossless")
        for _ in range(200):
            job = pipeline.job(job_id)
            if job["status"] not in {"queued", "running"}:
                break
            time.sleep(.02)
        pipeline.close()
        self.assertEqual((job["completed"], job["failed"], job["status"]),
                         (1, 1, "completed_with_errors"))

    def test_valid_legacy_package_is_adopted_once(self):
        pipeline = self.pipeline()
        job_id, item = self.completed(pipeline)
        pipeline.store.execute("DELETE FROM publication_journal WHERE publication_id=?", (item["id"],))
        document = pipeline.read_document(item["id"])
        self.assertEqual(document["document_id"], item["id"])
        rows = pipeline.store.rows(
            "SELECT state,detail FROM publication_journal WHERE publication_id=?", (item["id"],)
        )
        self.assertEqual(rows, [{"state": "PUBLISHED", "detail": "validated journal adoption"}])
        self.assertEqual(pipeline.job(job_id)["status"], "completed")

    def test_corrupt_legacy_package_is_persistently_quarantined(self):
        pipeline = self.pipeline()
        job_id, item = self.completed(pipeline)
        pipeline.store.execute("DELETE FROM publication_journal WHERE publication_id=?", (item["id"],))
        (Path(item["output_dir"]) / "normalized.md").write_text("corrupt", encoding="utf-8")
        with self.assertRaises(InputError):
            pipeline.read_document(item["id"])
        job = pipeline.job(job_id)
        self.assertEqual((job["files"][0]["status"], job["status"], job["completed"], job["failed"]),
                         ("reconciliation_required", "completed_with_errors", 0, 1))

    def test_pre_schema_package_is_bound_then_tamper_is_quarantined(self):
        pipeline = self.pipeline()
        job_id, item = self.completed(pipeline)
        output = Path(item["output_dir"])
        manifest_path = output / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for key in ("artifact_schema_version", "artifact_hashes", "section_descriptors",
                    "chunk_chars", "chunk_overlap_chars", "publication"):
            manifest.pop(key, None)
        manifest["outputs"] = [name for name in manifest["outputs"] if name != "sections.json"]
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / "sections.json").unlink()
        pipeline.store.execute("DELETE FROM publication_journal WHERE publication_id=?", (item["id"],))
        pipeline.store.execute("UPDATE files SET manifest_json=? WHERE id=?",
                               (json.dumps(manifest, ensure_ascii=False), item["id"]))
        self.assertEqual(pipeline.read_document(item["id"])["document_id"], item["id"])
        adopted = pipeline.store.rows(
            "SELECT detail FROM publication_journal WHERE publication_id=?", (item["id"],)
        )[0]
        self.assertEqual(adopted["detail"], "validated pre-schema legacy adoption")
        (output / "normalized.md").write_text("tampered after adoption", encoding="utf-8")
        with self.assertRaises(InputError):
            pipeline.read_document(item["id"])
        job = pipeline.job(job_id)
        self.assertEqual((job["status"], job["completed"], job["failed"]),
                         ("completed_with_errors", 0, 1))


if __name__ == "__main__":
    unittest.main()
