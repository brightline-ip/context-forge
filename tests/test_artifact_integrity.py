from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from context_forge.artifacts import (ARTIFACT_SCHEMA_VERSION, ArtifactIntegrityError,
                                     build_artifacts, validate_artifacts, write_artifacts)
from context_forge.extractors import Extraction, Section


def metadata(bundle) -> dict:
    return {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "sections": len(bundle.section_descriptors),
        "section_descriptors": bundle.section_descriptors,
        "chunks": len(bundle.chunks),
        "chunk_chars": bundle.target_chars,
        "chunk_overlap_chars": bundle.overlap_chars,
        "artifact_hashes": bundle.artifact_hashes,
        "source_name": "book.xlsx",
    }


class ArtifactIntegrityTests(unittest.TestCase):
    def fixture(self) -> Extraction:
        return Extraction([
            Section("sheet:Data", "Row 1: C1=value \nRow 2: C1=   \n\n", "Repeated"),
            Section("sheet:Data", "same text", "Repeated"),
            Section("sheet:Unicode", "same text 😀 𐐷\nlast ", "Repeated"),
            Section("sheet:Space", "   ", "Space only"),
        ], {})

    def test_v2_round_trip_handles_spaces_duplicates_and_non_bmp_offsets(self):
        bundle = build_artifacts(self.fixture(), "book.xlsx", target_chars=12, overlap_chars=3)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_artifacts(root, bundle)
            validate_artifacts(root, metadata(bundle))

            self.assertEqual([row["section_index"] for row in bundle.section_descriptors], [1, 2, 3, 4])
            self.assertEqual(bundle.section_descriptors[0]["locator"], bundle.section_descriptors[1]["locator"])
            unicode_row = bundle.section_descriptors[2]
            payload = (root / "normalized.md").read_bytes()[unicode_row["markdown_payload_utf8_start"]:
                                                              unicode_row["markdown_payload_utf8_end"]]
            self.assertEqual(payload.decode("utf-8"), self.fixture().sections[2].text)
            self.assertGreater(len(payload), unicode_row["text_codepoint_end"])

    def test_tampered_markdown_is_rejected(self):
        bundle = build_artifacts(self.fixture(), "book.xlsx", target_chars=12, overlap_chars=3)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_artifacts(root, bundle)
            path = root / "normalized.md"
            path.write_bytes(path.read_bytes().replace(b"Row 1", b"Row X", 1))
            with self.assertRaises(ArtifactIntegrityError):
                validate_artifacts(root, metadata(bundle))

    def test_tampered_chunk_span_is_rejected_even_with_updated_file_hash(self):
        bundle = build_artifacts(self.fixture(), "book.xlsx", target_chars=12, overlap_chars=3)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_artifacts(root, bundle)
            path = root / "chunks.jsonl"
            value = path.read_text(encoding="utf-8").replace('"section_codepoint_start":9',
                                                              '"section_codepoint_start":8', 1)
            path.write_text(value, encoding="utf-8", newline="\n")
            changed = metadata(bundle)
            import hashlib
            changed["artifact_hashes"] = changed["artifact_hashes"] | {
                "chunks.jsonl": hashlib.sha256(path.read_bytes()).hexdigest()
            }
            with self.assertRaises(ArtifactIntegrityError):
                validate_artifacts(root, changed)

    def test_ordinary_single_section_keeps_existing_markdown_shape(self):
        extraction = Extraction([Section("document", "ordinary text", "")], {})
        bundle = build_artifacts(extraction, "source.txt")
        self.assertEqual(bundle.normalized,
                         "# Normalized source: source.txt\n\n<!-- source_locator: document -->\n\nordinary text\n")
        self.assertEqual(bundle.chunks[0]["text"], "ordinary text")


if __name__ == "__main__":
    unittest.main()
