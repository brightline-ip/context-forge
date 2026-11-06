from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .extractors import Extraction
from .normalize import markdown


ARTIFACT_SCHEMA_VERSION = 2


class ArtifactIntegrityError(ValueError):
    pass


@dataclass(frozen=True)
class ArtifactBundle:
    normalized: str
    sections_bytes: bytes
    chunks_bytes: bytes
    section_descriptors: list[dict]
    chunks: list[dict]
    artifact_hashes: dict[str, str]
    target_chars: int
    overlap_chars: int


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _section_prefix(heading: str, locator: str) -> str:
    heading_text = f"## {heading}\n\n" if heading else ""
    return f"{heading_text}<!-- source_locator: {locator} -->\n\n"


def build_artifacts(extraction: Extraction, source_name: str, target_chars: int = 6000,
                    overlap_chars: int = 400, source_sha256: str | None = None) -> ArtifactBundle:
    if target_chars <= overlap_chars or overlap_chars < 0:
        raise ValueError("chunk_chars must be greater than nonnegative overlap_chars")

    normalized = markdown(extraction, source_name)
    assembled = f"# Normalized source: {source_name}\n\n" if extraction.sections else f"# Normalized source: {source_name}\n"
    descriptors: list[dict] = []
    section_rows: list[dict] = []
    chunks: list[dict] = []

    for zero_index, section in enumerate(extraction.sections):
        section_index = zero_index + 1
        prefix = _section_prefix(section.heading, section.locator)
        payload_start = len((assembled + prefix).encode("utf-8"))
        payload_end = payload_start + len(section.text.encode("utf-8"))
        descriptor = {
            "section_index": section_index,
            "locator": section.locator,
            "heading": section.heading,
            "text_sha256": _sha256(section.text.encode("utf-8")),
            "text_codepoint_start": 0,
            "text_codepoint_end": len(section.text),
            "markdown_payload_utf8_start": payload_start,
            "markdown_payload_utf8_end": payload_end,
        }
        descriptors.append(descriptor)
        section_rows.append(descriptor | {"text": section.text})
        assembled += prefix + section.text + "\n"
        if zero_index + 1 < len(extraction.sections):
            assembled += "\n"

        if section.text:
            step = target_chars - overlap_chars
            for start in range(0, len(section.text), step):
                text = section.text[start:start + target_chars]
                record = {
                    "index": len(chunks) + 1,
                    "section_index": section_index,
                    "locator": section.locator,
                    "heading": section.heading,
                    "section_codepoint_start": start,
                    "section_codepoint_end": start + len(text),
                    "text": text,
                    "sha256": _sha256(text.encode("utf-8")),
                    "source_file": source_name,
                }
                if source_sha256 is not None:
                    record["source_sha256"] = source_sha256
                chunks.append(record)
                if start + target_chars >= len(section.text):
                    break

    if assembled != normalized:
        raise ArtifactIntegrityError("markdown serializer diverged from canonical section framing")

    sections_bytes = (json.dumps({"artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
                                  "sections": section_rows}, ensure_ascii=False,
                                 separators=(",", ":")) + "\n").encode("utf-8")
    chunks_bytes = "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
                           for item in chunks).encode("utf-8")
    normalized_bytes = normalized.encode("utf-8")
    hashes = {
        "sections.json": _sha256(sections_bytes),
        "normalized.md": _sha256(normalized_bytes),
        "chunks.jsonl": _sha256(chunks_bytes),
    }
    return ArtifactBundle(normalized, sections_bytes, chunks_bytes, descriptors, chunks, hashes,
                          target_chars, overlap_chars)


def write_artifacts(directory: Path, bundle: ArtifactBundle) -> None:
    (directory / "sections.json").write_bytes(bundle.sections_bytes)
    (directory / "normalized.md").write_bytes(bundle.normalized.encode("utf-8"))
    (directory / "chunks.jsonl").write_bytes(bundle.chunks_bytes)


def validate_artifacts(directory: Path, metadata: dict) -> None:
    try:
        if metadata.get("artifact_schema_version") != ARTIFACT_SCHEMA_VERSION:
            raise ArtifactIntegrityError("unexpected artifact schema")
        target = metadata["chunk_chars"]
        overlap = metadata["chunk_overlap_chars"]
        if not isinstance(target, int) or not isinstance(overlap, int) or target <= overlap or overlap < 0:
            raise ArtifactIntegrityError("invalid chunk policy")

        raw_sections = (directory / "sections.json").read_bytes()
        raw_normalized = (directory / "normalized.md").read_bytes()
        raw_chunks = (directory / "chunks.jsonl").read_bytes()
        files = {"sections.json": raw_sections, "normalized.md": raw_normalized, "chunks.jsonl": raw_chunks}
        if any(_sha256(value) != metadata["artifact_hashes"].get(name) for name, value in files.items()):
            raise ArtifactIntegrityError("artifact hash mismatch")

        section_document = json.loads(raw_sections.decode("utf-8"))
        if section_document.get("artifact_schema_version") != ARTIFACT_SCHEMA_VERSION:
            raise ArtifactIntegrityError("section schema mismatch")
        sections = section_document.get("sections")
        if not isinstance(sections, list):
            raise ArtifactIntegrityError("invalid sections")
        expected_indices = list(range(1, len(sections) + 1))
        if [row.get("section_index") for row in sections] != expected_indices:
            raise ArtifactIntegrityError("section identities are not unique and sequential")
        descriptors = [{key: value for key, value in row.items() if key != "text"} for row in sections]
        if descriptors != metadata.get("section_descriptors"):
            raise ArtifactIntegrityError("section metadata mismatch")

        assembled = (f"# Normalized source: {metadata['source_name']}\n\n".encode("utf-8")
                     if sections else f"# Normalized source: {metadata['source_name']}\n".encode("utf-8"))
        for row in sections:
            text = row.get("text")
            if not isinstance(text, str):
                raise ArtifactIntegrityError("invalid section text")
            if row.get("text_codepoint_start") != 0 or row.get("text_codepoint_end") != len(text):
                raise ArtifactIntegrityError("section code-point span mismatch")
            encoded = text.encode("utf-8")
            if _sha256(encoded) != row.get("text_sha256"):
                raise ArtifactIntegrityError("section text hash mismatch")
            start, end = row.get("markdown_payload_utf8_start"), row.get("markdown_payload_utf8_end")
            if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
                raise ArtifactIntegrityError("invalid Markdown payload span")
            prefix = _section_prefix(row["heading"], row["locator"]).encode("utf-8")
            expected_start = len(assembled) + len(prefix)
            expected_end = expected_start + len(encoded)
            if (start, end) != (expected_start, expected_end):
                raise ArtifactIntegrityError("Markdown payload span is not canonical")
            if raw_normalized[start:end] != encoded:
                raise ArtifactIntegrityError("Markdown payload mismatch")
            assembled += prefix + encoded + b"\n"
            if row["section_index"] < len(sections):
                assembled += b"\n"
        if assembled != raw_normalized:
            raise ArtifactIntegrityError("Markdown framing mismatch")

        chunks = [json.loads(line) for line in raw_chunks.decode("utf-8").splitlines()]
        if len(chunks) != metadata.get("chunks"):
            raise ArtifactIntegrityError("chunk count mismatch")
        if [row.get("index") for row in chunks] != list(range(1, len(chunks) + 1)):
            raise ArtifactIntegrityError("chunk identities are not unique and sequential")
        by_section: dict[int, list[dict]] = {index: [] for index in expected_indices}
        for row in chunks:
            section_index = row.get("section_index")
            if section_index not in by_section:
                raise ArtifactIntegrityError("chunk references an unknown section")
            by_section[section_index].append(row)

        step = target - overlap
        for section in sections:
            text = section["text"]
            rows = by_section[section["section_index"]]
            expected_starts = []
            if text:
                for start in range(0, len(text), step):
                    expected_starts.append(start)
                    if start + target >= len(text):
                        break
            if [row.get("section_codepoint_start") for row in rows] != expected_starts:
                raise ArtifactIntegrityError("chunk spans are not gap-free")
            for row, start in zip(rows, expected_starts):
                value = text[start:start + target]
                if (row.get("section_codepoint_end") != start + len(value)
                        or row.get("locator") != section["locator"]
                        or row.get("heading") != section["heading"]
                        or row.get("source_file") != metadata.get("source_name")
                        or (metadata.get("source_sha256") is not None
                            and row.get("source_sha256") != metadata["source_sha256"])
                        or row.get("text") != value
                        or row.get("sha256") != _sha256(value.encode("utf-8"))):
                    raise ArtifactIntegrityError("chunk content or overlap mismatch")
            if rows and rows[-1]["section_codepoint_end"] != len(text):
                raise ArtifactIntegrityError("chunk coverage does not reach section end")
    except ArtifactIntegrityError:
        raise
    except OSError as exc:
        # A file briefly held by antivirus or a sync client is not corruption; only a missing one is.
        if not isinstance(exc, (FileNotFoundError, NotADirectoryError)):
            raise
        raise ArtifactIntegrityError("artifact package failed integrity validation") from exc
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactIntegrityError("invalid artifact structure") from exc
