from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class InlineResult:
    text: str
    changed: bool
    original_chars: int
    normalized_chars: int
    original_tokens_estimate: int
    normalized_tokens_estimate: int
    savings_percent: float
    original_sha256: str
    normalized_sha256: str
    mode: str = "lossless"

    def to_dict(self) -> dict:
        return asdict(self)


def estimate_tokens(text: str) -> int:
    """Conservative tokenizer-independent estimate for monitoring.

    Natural language commonly averages near four characters per token, while
    code and punctuation tokenize more densely. Using the larger of the two
    simple estimates avoids presenting optimistic savings as exact billing.
    """
    if not text:
        return 0
    lexical_units = len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))
    return max(1, (len(text) + 3) // 4, lexical_units)


def normalize_inline(text: str) -> InlineResult:
    """Apply meaning-preserving cleanup suitable for an outbound chat turn."""
    original = str(text or "")
    normalized = original.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t]+(?=\n)", "", normalized)
    normalized = re.sub(r"\n{4,}", "\n\n", normalized).strip()

    before = estimate_tokens(original)
    after = estimate_tokens(normalized)
    savings = ((before - after) / before * 100.0) if before else 0.0
    return InlineResult(
        text=normalized,
        changed=normalized != original,
        original_chars=len(original),
        normalized_chars=len(normalized),
        original_tokens_estimate=before,
        normalized_tokens_estimate=after,
        savings_percent=round(max(0.0, savings), 2),
        original_sha256=hashlib.sha256(original.encode("utf-8")).hexdigest(),
        normalized_sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    )
