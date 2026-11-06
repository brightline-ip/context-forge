"""Small append-only event log for rejected requests and retried extractions.

Holds route, status, and the service's own fixed reason text. Never the request target,
query, headers, body, or source path (same rule as the HTTP handler's logging).
"""
from datetime import datetime, timezone
import json
import threading

MAX_BYTES = 5 * 1024 * 1024
_lock = threading.Lock()


def _append(path, line, max_bytes):
    with _lock:
        if path.exists() and path.stat().st_size + len(line) > max_bytes:
            path.replace(path.with_name(path.name + '.1'))
        with path.open('a', encoding='utf-8') as stream:
            stream.write(line)


def append_event(path, event: dict, max_bytes: int = MAX_BYTES) -> None:
    """Best effort: a logging problem must never change a response or fail a job."""
    line = json.dumps({'at': datetime.now(timezone.utc).isoformat(), **event}, ensure_ascii=False) + '\n'
    try:
        _append(path, line, max_bytes)
    except OSError:
        pass


class ServiceLog:
    """Keeps service output however the service was started: the watchdog, MCP adapter and
    restart script all discard stdout and stderr, which is why the old log stopped on Sep 17."""
    def __init__(self, path, stream=None, max_bytes=MAX_BYTES):
        self.path, self.stream, self.max_bytes = path, stream, max_bytes

    def write(self, text):
        if self.stream is not None:
            try:
                self.stream.write(text)
            except (OSError, ValueError):
                pass
        if text.strip():
            try:
                _append(self.path, f"{datetime.now(timezone.utc).isoformat()} {text.rstrip()}\n", self.max_bytes)
            except OSError:
                pass
        return len(text)

    def flush(self):
        if self.stream is not None:
            try:
                self.stream.flush()
            except (OSError, ValueError):
                pass
