from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import re
from urllib import request
from urllib.parse import urlsplit
import zipfile

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_FILES = 100
MAX_TEXT_CHARS = 8_000_000


class InputError(ValueError):
    """An operator-safe validation error, with no source payload or credentials."""


class BusyError(InputError):
    pass


def safe_error(exc: Exception) -> str:
    if isinstance(exc, InputError):
        return str(exc)
    return f"{type(exc).__name__}: processing failed; inspect the source and service configuration"


def local_url(value: str) -> str:
    parsed = urlsplit(value)
    try:
        loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        port = parsed.port
    except ValueError:
        loopback, port = False, None
    if (parsed.scheme != "http" or not loopback or not port or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise InputError("Model endpoint must be a numeric loopback HTTP URL with an explicit port and no credentials")
    return value.rstrip("/")


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise InputError("Redirects are not permitted")


def local_opener():
    return request.build_opener(request.ProxyHandler({}), NoRedirect())


def local_path(value: str) -> Path:
    if not isinstance(value, str) or not value.strip() or value.startswith(("\\\\", "//")):
        raise InputError("An absolute local filesystem path is required; network paths are disabled")
    path = Path(value)
    if not path.is_absolute() or (os.name == "nt" and ":" in str(path)[2:]):
        raise InputError("An absolute local filesystem path is required; alternate streams are disabled")
    path = path.resolve()
    if str(path).startswith(("\\\\", "//")):
        raise InputError("Network paths are disabled")
    return path


def within(path: Path, roots: list[str]) -> bool:
    return any(path.is_relative_to(local_path(root)) for root in roots)


def input_path(value: str, roots: list[str], excluded: list[Path] = ()) -> Path:
    path = local_path(value)
    if not within(path, roots) or any(path.is_relative_to(p.resolve()) for p in excluded):
        raise InputError("Input path is outside configured input roots or inside generated storage")
    relative = next(path.relative_to(local_path(root)) for root in roots if path.is_relative_to(local_path(root)))
    if any(p.lower() in {".git", ".hermes", ".codex", ".ssh", ".aws", ".gnupg", "node_modules", ".venv", "security", "secrets"} for p in relative.parts):
        raise InputError("Credential, agent configuration, and dependency directories are not intake sources")
    if (path.name.lower() in {"auth.json", "credentials.json", ".env", "service-token.txt"}
            or path.suffix.lower() in {".pem", ".key", ".pfx", ".p12", ".dpapi"}):
        raise InputError("Credential files are not intake sources")
    if not path.exists():
        raise InputError("Input path does not exist")
    return path


def validate_file(path: Path) -> None:
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise InputError("Input must be a regular file of at most 50 MiB")
    if path.suffix.lower() in {".docx", ".xlsx", ".xlsm", ".pptx", ".epub"}:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 10000 or sum(e.file_size for e in entries) > 100 * 1024 * 1024:
                raise InputError("Archive exceeds entry or expanded-size limits")
            if any(e.file_size > 1024 * 1024 and e.file_size > max(1, e.compress_size) * 200 for e in entries):
                raise InputError("Archive compression ratio exceeds the safety limit")


def validate_settings(settings: dict) -> dict:
    result = dict(settings)
    for key in ("allowed_input_roots", "allowed_output_roots"):
        roots = result.get(key)
        if not isinstance(roots, list) or not 1 <= len(roots) <= 20:
            raise InputError(f"{key} must contain 1-20 absolute local paths")
        result[key] = [str(local_path(root)) for root in roots]
    output = local_path(result["output_dir"])
    if not within(output, result["allowed_output_roots"]):
        raise InputError("Output path is outside configured output roots")
    result["output_dir"] = str(output)
    for key, low, high in (("chunk_chars", 1000, 12000), ("llm_timeout_seconds", 1, 60)):
        if type(result.get(key)) is not int or not low <= result[key] <= high:
            raise InputError(f"{key} must be an integer from {low} to {high}")
    result["ollama_base_url"] = local_url(result["ollama_base_url"])
    if not isinstance(result["ollama_model"], str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,120}", result["ollama_model"]):
        raise InputError("Invalid model name")
    return result
