from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass

from .extractors import Extraction, Section


@dataclass
class Chunk:
    index: int
    locator: str
    heading: str
    text: str
    sha256: str


def _clean(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?<=\w)-\n(?=[a-z])", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize(extraction: Extraction, mode: str = "legacy") -> Extraction:
    if mode == "lossless":
        # Preserve spacing, repeated text, and hyphens; normalize line endings only.
        extraction.sections = [Section(s.locator, s.text.replace("\r\n", "\n").replace("\r", "\n"), s.heading) for s in extraction.sections]
        return extraction
    if mode != "legacy":
        raise ValueError("Unsupported normalization mode")
    cleaned = [Section(s.locator, _clean(s.text), _clean(s.heading)) for s in extraction.sections]
    # Remove exact short headers/footers repeated across at least half of page-like sections.
    page_sections = [s for s in cleaned if s.locator.startswith("page:") and s.text]
    repeated: set[str] = set()
    if len(page_sections) >= 3:
        candidates = Counter()
        for section in page_sections:
            lines = [line.strip() for line in section.text.splitlines() if line.strip()]
            candidates.update(set(line for line in lines[:3] + lines[-3:] if 3 <= len(line) <= 120))
        repeated = {line for line, count in candidates.items() if count >= max(3, len(page_sections) // 2)}
    if repeated:
        for section in cleaned:
            section.text = "\n".join(line for line in section.text.splitlines() if line.strip() not in repeated).strip()
        extraction.warnings.append(f"Removed {len(repeated)} repeated header/footer line(s)")
    extraction.sections = cleaned
    return extraction


def chunk(extraction: Extraction, target_chars: int = 6000, overlap_chars: int = 400) -> list[Chunk]:
    if target_chars <= overlap_chars or overlap_chars < 0:
        raise ValueError("chunk_chars must be greater than nonnegative overlap_chars")
    chunks: list[Chunk] = []
    for section in extraction.sections:
        if not section.text:
            continue
        for start in range(0, len(section.text), target_chars - overlap_chars):
            _append(chunks, section, section.text[start:start + target_chars])
            if start + target_chars >= len(section.text):
                break
    return chunks


def _append(chunks: list[Chunk], section: Section, text: str) -> None:
    value = text
    if value:
        chunks.append(Chunk(len(chunks) + 1, section.locator, section.heading, value, hashlib.sha256(value.encode("utf-8")).hexdigest()))


def _markdown_section(section: Section) -> str:
    heading = f"## {section.heading}\n\n" if section.heading else ""
    # Framing is added around, never by trimming, the canonical payload.
    return f"{heading}<!-- source_locator: {section.locator} -->\n\n{section.text}\n"


def markdown(extraction: Extraction, source_name: str) -> str:
    preamble = f"# Normalized source: {source_name}\n\n"
    if not extraction.sections:
        return preamble.removesuffix("\n")
    return preamble + "\n".join(_markdown_section(section) for section in extraction.sections)


def salience_pack(extraction: Extraction, max_chars: int = 14000) -> str:
    headings = [s.heading for s in extraction.sections if s.heading][:80]
    sentences = []
    for section in extraction.sections:
        values = re.split(r"(?<=[.!?])\s+", section.text)
        candidates = values[:3] + (values[-2:] if len(values) > 3 else [])
        sentences.extend(f"[{section.locator}] {value.strip()}" for value in candidates if 40 <= len(value.strip()) <= 800)
    value = "HEADINGS\n" + "\n".join(headings) + "\n\nREPRESENTATIVE PASSAGES\n" + "\n".join(sentences)
    return value[:max_chars]
