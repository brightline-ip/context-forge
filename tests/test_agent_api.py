from pathlib import Path
import tempfile
import time
import unittest

from context_forge.extractors import Extraction, Section
from context_forge.normalize import chunk, normalize
from context_forge.pipeline import Pipeline


class AgentApiTests(unittest.TestCase):
    def test_lossless_keeps_repeated_lines_indentation_and_hyphens(self):
        text = "Repeated fact\r\n    indented\r\nnon-\r\nlinear\r\nRepeated fact"
        result = normalize(Extraction([Section(f"page:{i}", text) for i in range(3)], {}), mode="lossless")
        for section in result.sections:
            self.assertEqual(section.text, text.replace("\r\n", "\n"))
        self.assertFalse(result.warnings)

    def test_chunks_bound_long_paragraph_after_short_paragraph(self):
        source = "Intro\n\n" + "0123456789" * 2000 + "THE END"
        chunks = chunk(Extraction([Section("document", source)], {}))
        self.assertTrue(all(len(item.text) <= 6000 for item in chunks))
        self.assertTrue(chunks[-1].text.endswith("THE END"))
        self.assertEqual(chunks[0].text + ''.join(item.text[400:] for item in chunks[1:]), source)

    def wait_job(self, pipeline, job_id):
        for _ in range(1000):
            job = pipeline.job(job_id)
            if job["status"] not in {"queued", "running"}:
                return job
            time.sleep(0.01)
        self.fail("Job never reached a terminal state")

    def test_duplicate_names_bad_file_and_document_pagination(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            for name in ("a", "b"):
                (root / name).mkdir()
                (root / name / "same.txt").write_text("    spacing\nRepeated fact\n" + "a" * 14000)
            bad = root / "broken.pdf"
            bad.write_bytes(b"not a PDF")
            pipeline = Pipeline(root, {"output_dir": str(root / "output")})
            job = self.wait_job(pipeline, pipeline.create_job([root / "a/same.txt", root / "b/same.txt", bad], "test", False, normalization_mode="lossless"))
            self.assertEqual(job["status"], "completed_with_errors")
            self.assertEqual((job["completed"], job["failed"]), (2, 1))
            files = [item for item in job["files"] if item["status"] == "completed"]
            self.assertNotEqual(files[0]["output_dir"], files[1]["output_dir"])
            first = pipeline.read_document(files[0]["id"], limit=1)
            self.assertEqual(first["next_offset"], 1)
            self.assertEqual(first["manifest"]["enrichment"], "disabled")
            self.assertIn("    spacing", Path(first["normalized_path"]).read_text())
            last = pipeline.read_document(files[0]["id"], offset=first["manifest"]["chunks"] - 1)
            self.assertIsNone(last["next_offset"])
            with self.assertRaises(ValueError):
                pipeline.read_document("missing")
            with self.assertRaises(ValueError):
                pipeline.read_document(files[0]["id"], limit=6)

    def test_output_failure_does_not_leave_job_running(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            source = root / "source.txt"
            source.write_text("fact")
            output = root / "not-a-directory"
            output.write_text("block output")
            pipeline = Pipeline(root, {"output_dir": str(output)})
            job = self.wait_job(pipeline, pipeline.create_job([source], "test", False))
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["files"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
