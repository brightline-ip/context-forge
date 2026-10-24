from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib import error, request
from urllib.parse import urlsplit

from agent.context_compressor import ContextCompressor

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43,256}")


def _default_token_file():
    """CONTEXT_FORGE_HOME/security/service-token.txt, or None (fail closed) when unconfigured."""
    home = os.getenv("CONTEXT_FORGE_HOME")
    return Path(home) / "security" / "service-token.txt" if home else None


def _load_service_token():
    direct = os.getenv("CONTEXT_FORGE_TOKEN")
    configured_file = os.getenv("CONTEXT_FORGE_TOKEN_FILE")
    if direct and configured_file:
        return ""
    if direct:
        return direct if TOKEN_PATTERN.fullmatch(direct) else ""
    token_file = Path(configured_file) if configured_file else _default_token_file()
    if token_file is None:
        return ""
    try:
        value = token_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    return value if TOKEN_PATTERN.fullmatch(value) else ""


class ContextForgeEngine(ContextCompressor):
    """Built-in compressor plus request-only normalization of the newest user turn."""

    def __init__(self):
        super().__init__(model="")
        self.endpoint = os.getenv(
            "CONTEXT_FORGE_URL", "http://127.0.0.1:8765/api/normalize/text"
        )
        if self.endpoint.rstrip("/").endswith(":8765"):
            self.endpoint = self.endpoint.rstrip("/") + "/api/normalize/text"
        self.token = _load_service_token()
        self.min_chars = max(0, int(os.getenv("CONTEXT_FORGE_MIN_CHARS", "800")))
        self.timeout = max(0.1, float(os.getenv("CONTEXT_FORGE_TIMEOUT_SECONDS", "3")))
        self._cache = {}

    @property
    def name(self):
        return "context-forge"

    def __deepcopy__(self, memo):
        clone = type(self)()
        for key, value in self.__dict__.items():
            setattr(clone, key, copy.deepcopy(value, memo))
        return clone

    def select_context(
        self,
        request_messages,
        *,
        conversation_messages=None,
        incoming_message=None,
        budget_tokens=0,
    ):
        index = self._latest_user_index(request_messages)
        if index is None:
            return None
        content = request_messages[index].get("content")
        text = self._plain_text(content)
        if text is None or len(text) < self.min_chars:
            self._record(text or "", text or "", applied=False, reason="below_threshold")
            return None

        result = self._normalize(text)
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            self._record(text, text, applied=False, reason="service_unavailable")
            return None
        normalized = result["text"]
        if not self._verified_lossless_response(text, normalized, result):
            self._record(text, text, applied=False, reason="non_lossless_response_blocked")
            return None
        if normalized == text:
            self._record(text, text, applied=False, reason="unchanged", service=result)
            return None

        selected = copy.deepcopy(request_messages)
        selected[index]["content"] = self._replace_plain_text(content, normalized)
        self._record(text, normalized, applied=True, reason="lossless_cleanup", service=result)
        return selected

    @staticmethod
    def _latest_user_index(messages):
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].get("role") == "user":
                return index
        return None

    @staticmethod
    def _plain_text(content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = [p for p in content if isinstance(p, dict) and p.get("type") == "text"]
            if len(text_parts) == 1 and isinstance(text_parts[0].get("text"), str):
                return text_parts[0]["text"]
        return None

    @staticmethod
    def _replace_plain_text(content, normalized):
        if isinstance(content, str):
            return normalized
        parts = copy.deepcopy(content)
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                part["text"] = normalized
                break
        return parts

    def _normalize(self, text):
        if not self.token:
            return None
        try:
            endpoint = urlsplit(self.endpoint)
        except ValueError:
            return None
        if (endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "localhost"}
                or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
                or endpoint.path != "/api/normalize/text" or len(text) > 250000):
            return None
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest in self._cache:
            return self._cache[digest]
        payload = json.dumps({"text": text, "mode": "lossless"}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            req = request.Request(self.endpoint, data=payload, headers=headers, method="POST")
            class NoRedirect(request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    raise ValueError("Redirect blocked")

            opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
            with opener.open(req, timeout=min(10, self.timeout)) as response:
                if response.headers.get("X-Context-Forge-Security-Revision") != "3":
                    return None
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    return None
                value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                return None
            self._cache[digest] = value
            if len(self._cache) > 16:
                self._cache.pop(next(iter(self._cache)))
            return value
        except (error.URLError, TimeoutError, ValueError, OSError):
            return None

    @staticmethod
    def _verified_lossless_response(original, normalized, result):
        canonical = original.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
        canonical = re.sub(r"[ \t]+(?=\n)", "", canonical)
        canonical = re.sub(r"\n{4,}", "\n\n", canonical).strip()
        return (
            result.get("mode") == "lossless"
            and result.get("original_sha256") == hashlib.sha256(original.encode("utf-8")).hexdigest()
            and normalized == canonical
        )

    def _record(self, original, normalized, *, applied, reason, service=None):
        try:
            from hermes_constants import get_hermes_home

            log_dir = Path(get_hermes_home()) / "state" / "context-forge"
            log_dir.mkdir(parents=True, exist_ok=True)
            entry = {
                "timestamp": time.time(),
                "applied": bool(applied),
                "reason": reason,
                "original_chars": len(original),
                "normalized_chars": len(normalized),
            }
            if service:
                entry.update({
                    "original_tokens_estimate": service.get("original_tokens_estimate"),
                    "normalized_tokens_estimate": service.get("normalized_tokens_estimate"),
                    "savings_percent": service.get("savings_percent"),
                    "original_sha256": service.get("original_sha256"),
                    "normalized_sha256": service.get("normalized_sha256"),
                })
            log_path = log_dir / "preflight.jsonl"
            if log_path.exists() and log_path.stat().st_size > 1024 * 1024:
                log_path.replace(log_dir / "preflight.previous.jsonl")
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            print('{"event":"preflight_telemetry_unavailable"}', file=sys.stderr)


def register(ctx):
    ctx.register_context_engine(ContextForgeEngine())
