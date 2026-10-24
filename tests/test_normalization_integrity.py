from __future__ import annotations

import hashlib
import unittest

from context_forge.extractors import Extraction, Section
from context_forge.normalize import Chunk, chunk, markdown, normalize


def reconstruct_chunk_text(chunks: list[Chunk], overlap_chars: int) -> dict[str, str]:
    reconstructed: dict[str, str] = {}
    for item in chunks:
        previous = reconstructed.get(item.locator, "")
        overlap = min(overlap_chars, len(previous), len(item.text)) if previous else 0
        if overlap:
            assert previous[-overlap:] == item.text[:overlap]
        reconstructed[item.locator] = previous + item.text[overlap:]
    return reconstructed


def payload_from_markdown(rendered: str, section: Section) -> str:
    heading = f"## {section.heading}\n\n" if section.heading else ""
    prefix = f"{heading}<!-- source_locator: {section.locator} -->\n\n"
    start = rendered.index(prefix) + len(prefix)
    return rendered[start:start + len(section.text)]


class NormalizationIntegrityTests(unittest.TestCase):
    def test_lossless_markdown_preserves_canonical_payloads_exactly(self):
        sections = [
            Section("case:trailing-space", "value ends here ", "Repeated heading"),
            Section("case:trailing-newlines", "line one\nline two\n\n", "Repeated heading"),
            Section("case:space-only", "   ", "Whitespace"),
            Section("case:repeated-a", "same payload", "Same"),
            Section("case:repeated-b", "same payload", "Same"),
            Section("case:unicode", "naïve café — 😀 𐐷\n", "Unicode 😀"),
        ]
        extraction = normalize(Extraction(sections, {}), mode="lossless")
        rendered = markdown(extraction, "integrity.xlsx")

        for section in extraction.sections:
            self.assertEqual(payload_from_markdown(rendered, section), section.text)

        self.assertTrue(rendered.endswith("naïve café — 😀 𐐷\n\n"))
        self.assertEqual(rendered.count("## Repeated heading\n"), 2)
        self.assertEqual(rendered.count("same payload"), 2)

    def test_chunks_and_markdown_reconstruct_the_same_payloads(self):
        sections = [
            Section("section:1", "alpha  \n" + "A" * 31 + "\n\n", "Repeated"),
            Section("section:2", "alpha  \n" + "A" * 31 + "\n\n", "Repeated"),
            Section("section:3", "  ", "Space only"),
            Section("section:4", "emoji 😀 𐐷\nlast ", "Unicode"),
        ]
        extraction = normalize(Extraction(sections, {}), mode="lossless")
        rendered = markdown(extraction, "source.md")
        chunks = chunk(extraction, target_chars=16, overlap_chars=4)
        reconstructed = reconstruct_chunk_text(chunks, overlap_chars=4)

        self.assertEqual(set(reconstructed), {section.locator for section in sections})
        for section in extraction.sections:
            self.assertEqual(reconstructed[section.locator], section.text)
            self.assertEqual(payload_from_markdown(rendered, section), reconstructed[section.locator])
        for item in chunks:
            self.assertEqual(item.sha256, hashlib.sha256(item.text.encode("utf-8")).hexdigest())


if __name__ == "__main__":
    unittest.main()
