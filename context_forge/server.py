from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import re
import shutil
import socket
import sys
import threading
import uuid
import urllib.parse
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .extractors import SUPPORTED_EXTENSIONS, discover
from .inline import normalize_inline
from .pipeline import Pipeline
from .safety import (BusyError, InputError, MAX_FILES, MAX_UPLOAD_BYTES,
                     input_path, safe_error, validate_settings)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SECURITY_REVISION = 3
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43,256}")
DEFAULT_SETTINGS = {
    "output_dir": str(PROJECT_ROOT / "output"),
    "ollama_base_url": "http://127.0.0.1:11434/v1",
    "ollama_model": "qwen3.5:4b",
    "chunk_chars": 6000,
    "llm_timeout_seconds": 60,
    "allowed_input_roots": [str(Path.home() / "Documents")],
    "allowed_output_roots": [str(PROJECT_ROOT / "output")],
}


def validate_service_token(value: str | None) -> str:
    if not isinstance(value, str) or not TOKEN_PATTERN.fullmatch(value):
        raise InputError("Context Forge requires a 256-bit URL-safe bearer token")
    return value


def configured_service_token_path(root: Path = PROJECT_ROOT, environment: dict[str, str] | None = None) -> Path | None:
    """Return credential location metadata without reading credential contents."""
    env = os.environ if environment is None else environment
    if env.get("CONTEXT_FORGE_TOKEN"):
        return None
    return Path(env.get("CONTEXT_FORGE_TOKEN_FILE") or root / "security" / "service-token.txt").resolve()


def load_service_token(root: Path = PROJECT_ROOT, environment: dict[str, str] | None = None) -> str:
    """Load one credential without putting its value in arguments, settings, or output."""
    env = os.environ if environment is None else environment
    direct = env.get("CONTEXT_FORGE_TOKEN")
    configured_file = env.get("CONTEXT_FORGE_TOKEN_FILE")
    if direct and configured_file:
        raise InputError("Configure CONTEXT_FORGE_TOKEN or CONTEXT_FORGE_TOKEN_FILE, not both")
    if direct:
        return validate_service_token(direct)
    token_path = Path(configured_file) if configured_file else root / "security" / "service-token.txt"
    try:
        if not token_path.is_file():
            raise InputError("Context Forge authentication is not initialized")
        value = token_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InputError("Context Forge authentication is not initialized") from exc
    return validate_service_token(value)


def environment_overrides(environment: dict[str, str] | None = None) -> dict:
    """Optional CONTEXT_FORGE_INPUT_ROOTS (os.pathsep-separated) and CONTEXT_FORGE_OUTPUT_DIR."""
    env = os.environ if environment is None else environment
    result: dict = {}
    roots = [item.strip() for item in (env.get("CONTEXT_FORGE_INPUT_ROOTS") or "").split(os.pathsep) if item.strip()]
    if roots:
        result["allowed_input_roots"] = roots
    output = (env.get("CONTEXT_FORGE_OUTPUT_DIR") or "").strip()
    if output:
        result["output_dir"] = output
        result["allowed_output_roots"] = [output]
    return result


def load_settings(root: Path = PROJECT_ROOT) -> dict:
    path = root / "data" / "settings.json"
    defaults = DEFAULT_SETTINGS | {"output_dir": str(root / "output"), "allowed_output_roots": [str(root / "output")]}
    if root != PROJECT_ROOT:
        defaults["allowed_input_roots"] = [str(root)]
    overrides = environment_overrides() if root == PROJECT_ROOT else {}
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return validate_settings(defaults | json.loads(path.read_text(encoding="utf-8")) | overrides)
    path.write_text(json.dumps(defaults, indent=2), encoding="utf-8")
    return validate_settings(defaults | overrides)


class App:
    def __init__(self, token: str | None, root: Path = PROJECT_ROOT, token_file: Path | None = None):
        token = validate_service_token(token)
        self.root = root.resolve()
        self.settings = load_settings(self.root)
        self.lock_file = (self.root / "data" / "service.lock").open("a+b")
        try:
            if self.lock_file.tell() == 0:
                self.lock_file.write(b"0")
                self.lock_file.flush()
            self.lock_file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.lock_file.close()
            raise InputError("Another Context Forge service owns this data directory") from exc
        self.settings_lock = threading.Lock()
        self.pipeline = Pipeline(self.root, self.settings)
        self.token = token
        self.token_file = token_file.resolve() if token_file else None
        self.upload_root = self.root / "data" / "uploads"
        self.upload_root.mkdir(parents=True, exist_ok=True)

    def excluded_input_paths(self) -> list[Path]:
        paths = [self.root / "data", self.root / "security", Path(self.settings["output_dir"])]
        if self.token_file:
            paths.append(self.token_file)
        return paths

    def credential_upload_names(self) -> set[str]:
        names = {"service-token.txt", "auth.json", "credentials.json", ".env"}
        if self.token_file:
            names.add(self.token_file.name.lower())
        return names

    def close(self):
        self.pipeline.close()
        self.lock_file.close()


def handler_factory(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ContextForge/0.1"

        def setup(self):
            self.request.settimeout(10)
            super().setup()

        def log_message(self, fmt, *args):
            # Never log the request target, query, headers, body, or parser messages.
            return

        def log_request(self, code="-", size="-"):
            print(json.dumps({"event": "http_response", "status": str(code)}), file=sys.stderr)

        def trusted_request(self) -> bool:
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            host = self.headers.get("Host", "").lower()
            if len(self.headers.get_all("Host", [])) != 1 or host not in hosts:
                self.json_response({"error": "Untrusted Host"}, 403)
                return False
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + host:
                self.json_response({"error": "Cross-origin requests are not permitted"}, 403)
                return False
            if self.headers.get("Sec-Fetch-Site") not in {None, "same-origin", "none"}:
                self.json_response({"error": "Cross-site requests are not permitted"}, 403)
                return False
            return True

        def authorized(self) -> bool:
            if not app.token:
                return False
            supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
            return hmac.compare_digest(supplied.encode("utf-8"), app.token.encode("utf-8"))

        def json_response(self, value, status=200):
            if status >= 400 and self.command == "POST" and not getattr(self, "body_read", False):
                length = self.headers.get("Content-Length", "")
                if length.isdigit() and int(length) <= 2 * 1024 * 1024:
                    try:
                        self.connection.settimeout(0.2)
                        self.rfile.read(int(length))
                    except (OSError, TimeoutError):
                        self.close_connection = True
                    finally:
                        self.connection.settimeout(10)
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Context-Forge-Security-Revision", str(SECURITY_REVISION))
            self.end_headers()
            self.wfile.write(payload)

        def read_body(self, maximum):
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isdigit():
                raise InputError("Exactly one nonnegative Content-Length is required; transfer encoding is unsupported")
            size = int(lengths[0])
            if not 0 <= size <= maximum:
                raise InputError("Request body exceeds the size limit")
            raw = self.rfile.read(size)
            self.body_read = True
            if len(raw) != size:
                raise InputError("Incomplete request body")
            return raw

        def read_json(self):
            if self.headers.get_content_type() != "application/json":
                raise InputError("Content-Type must be application/json")
            body = json.loads(self.read_body(2 * 1024 * 1024) or b"{}")
            if not isinstance(body, dict):
                raise InputError("JSON body must be an object")
            return body

        def do_GET(self):
            if not self.trusted_request():
                return
            try:
                self.get_response()
            except Exception as exc:
                self.json_response({"error": safe_error(exc)}, 500)

        def get_response(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path.startswith("/api/") and not self.authorized():
                return self.json_response({"error": "Unauthorized"}, 401)
            if parsed.path == "/api/status":
                app.pipeline.store.rows("SELECT 1")
                return self.json_response({
                    "service": "Context Forge", "agent_api_version": 1, "security_revision": SECURITY_REVISION,
                    "active_jobs": app.pipeline.active_jobs,
                    "ok": True, "version": "0.1.0", "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
                    "output_dir": app.settings["output_dir"], "ollama_model": app.settings["ollama_model"],
                    "auth_required": True,
                })
            if parsed.path == "/api/jobs":
                return self.json_response(app.pipeline.jobs())
            if parsed.path == '/api/bound-receipt':
                if not app.token:
                    return self.json_response({'error': 'Bound requests require configured authentication'}, 403)
                try:
                    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                    if set(query) != {'operation_key'} or len(query['operation_key']) != 1:
                        raise InputError('Supply only one operation_key')
                    return self.json_response(app.pipeline.bound_receipt(query['operation_key'][0]))
                except ValueError as exc:
                    return self.json_response({'error': safe_error(exc)}, 400)
            if parsed.path.startswith("/api/documents/"):
                try:
                    query = urllib.parse.parse_qs(parsed.query)
                    result = app.pipeline.read_document(parsed.path.rsplit("/", 1)[-1], int(query.get("offset", [0])[0]), int(query.get("limit", [2])[0]))
                    return self.json_response(result)
                except ValueError as exc:
                    return self.json_response({"error": safe_error(exc)}, 400)
            if parsed.path.startswith("/api/jobs/"):
                job = app.pipeline.job(parsed.path.rsplit("/", 1)[-1])
                return self.json_response(job or {"error": "Job not found"}, 200 if job else 404)
            if parsed.path == "/api/settings":
                return self.json_response(app.settings)
            if parsed.path in {"/", "/index.html"}:
                return self.static(PROJECT_ROOT / "web" / "index.html")
            if parsed.path.startswith("/assets/"):
                path = (PROJECT_ROOT / "web" / parsed.path.removeprefix("/")).resolve()
                if PROJECT_ROOT / "web" not in path.parents:
                    return self.send_error(403)
                return self.static(path)
            self.send_error(404)

        def do_POST(self):
            if not self.trusted_request():
                return
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path.startswith("/api/") and not self.authorized():
                return self.json_response({"error": "Unauthorized"}, 401)
            try:
                if parsed.path == '/api/jobs/bound-path':
                    if not app.token:
                        return self.json_response({'error': 'Bound requests require configured authentication'}, 403)
                    body = self.read_json()
                    if parsed.query or set(body) != {'path', 'expected_sha256', 'operation_key'}:
                        raise InputError('Supply only path, expected_sha256 and operation_key')
                    if not isinstance(body['path'], str) or not body['path'].strip():
                        raise InputError('Supply one file path')
                    excluded = app.excluded_input_paths()
                    source = input_path(body['path'], app.settings['allowed_input_roots'], excluded)
                    if not source.is_file():
                        raise InputError('Bound requests accept one file, not a folder')
                    job_id = app.pipeline.create_bound_job(source, body['expected_sha256'], body['operation_key'])
                    return self.json_response({'job_id': job_id, 'files': 1}, 202)
                if parsed.path == '/api/ocr/continue':
                    body = self.read_json()
                    if set(body) != {'document_id'}:
                        raise InputError('Supply only document_id')
                    job_id = app.pipeline.continue_ocr(body['document_id'])
                    return self.json_response({'job_id': job_id, 'files': 1}, 202)
                if parsed.path == "/api/jobs/path":
                    body = self.read_json()
                    allowed = {"path", "recursive", "enrich", "normalization_mode"}
                    if set(body) - allowed:
                        raise InputError("Only path, recursive, enrich, and normalization_mode are accepted")
                    if not isinstance(body.get("path"), str) or not body["path"].strip():
                        raise InputError("A nonempty local path is required")
                    for key in ("recursive", "enrich"):
                        if key in body and type(body[key]) is not bool:
                            raise InputError(f"{key} must be boolean")
                    excluded = app.excluded_input_paths()
                    source = input_path(body["path"], app.settings["allowed_input_roots"], excluded)
                    paths = []
                    for candidate in discover(source, body.get("recursive", False), excluded):
                        paths.append(input_path(str(candidate), app.settings["allowed_input_roots"], excluded))
                        if len(paths) > MAX_FILES:
                            raise InputError("A job may contain at most 100 files; split the folder")
                    if not paths:
                        return self.json_response({"error": "No supported files found"}, 400)
                    job_id = app.pipeline.create_job(paths, str(source), body.get("enrich", False), normalization_mode=body.get("normalization_mode", "lossless"))
                    return self.json_response({"job_id": job_id, "files": len(paths)}, 202)
                if parsed.path == "/api/jobs/upload":
                    return self.handle_upload()
                if parsed.path == "/api/settings":
                    body = self.read_json()
                    editable = {"output_dir", "ollama_model", "chunk_chars", "llm_timeout_seconds"}
                    if set(body) - editable:
                        raise InputError("Only output_dir, ollama_model, chunk_chars, and llm_timeout_seconds may be edited over HTTP")
                    with app.settings_lock:
                        settings = validate_settings(app.settings | body)
                        settings_path = app.root / "data" / "settings.json"
                        temporary = settings_path.with_suffix(".tmp")
                        temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
                        temporary.replace(settings_path)
                        app.settings = settings
                        app.pipeline.settings = settings
                    return self.json_response(app.settings)
                if parsed.path == "/api/normalize/text":
                    body = self.read_json()
                    if set(body) - {"text", "mode"}:
                        raise InputError("Only text and mode are accepted")
                    if not isinstance(body.get("text", ""), str):
                        return self.json_response({"error": "text must be a string"}, 400)
                    if body.get("mode", "lossless") != "lossless":
                        return self.json_response({
                            "error": "Only deterministic lossless normalization is supported"
                        }, 400)
                    return self.json_response(normalize_inline(body.get("text", "")).to_dict())
            except Exception as exc:
                return self.json_response({"error": safe_error(exc)}, 429 if isinstance(exc, BusyError) else 400)
            self.send_error(404)

        def handle_upload(self):
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                return self.json_response({"error": "Expected multipart/form-data"}, 415)
            raw = self.read_body(MAX_UPLOAD_BYTES)
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + content_type.encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw
            )
            if not message.is_multipart():
                raise InputError("Malformed multipart upload")
            upload_dir = app.upload_root / uuid.uuid4().hex
            upload_dir.mkdir(parents=True)
            paths = []
            enrich_value = False
            try:
                for part in message.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    if name == "enrich":
                        enrich_value = part.get_content().strip().lower() == "true"
                        continue
                    if name != "files":
                        continue
                    filename = part.get_filename() or "upload"
                    pieces = [piece for piece in re.split(r"[/\\]+", filename) if piece not in {"", ".", ".."}]
                    lowered = [piece.lower() for piece in pieces]
                    if (not pieces or len(pieces) > 16 or len(paths) >= MAX_FILES
                            or "security" in lowered or lowered[-1] in app.credential_upload_names()):
                        raise InputError("Credential paths are not upload sources")
                    payload = part.get_payload(decode=True)
                    if (len(payload) == len(app.token)
                            and hmac.compare_digest(payload, app.token.encode("utf-8"))):
                        raise InputError("Credential content is not an upload source")
                    relative = Path(*(safe_piece(piece) for piece in pieces))
                    target = upload_dir / relative
                    if not target.resolve().is_relative_to(upload_dir.resolve()):
                        raise InputError("Invalid upload path")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as stream:
                        stream.write(payload)
                    if target.suffix.lower() in SUPPORTED_EXTENSIONS:
                        paths.append(target)
            except Exception:
                shutil.rmtree(upload_dir, ignore_errors=True)
                raise
            if not paths:
                shutil.rmtree(upload_dir, ignore_errors=True)
                return self.json_response({"error": "No supported files uploaded"}, 400)
            job_id = app.pipeline.create_job(paths, f"web upload ({len(paths)} files)", enrich_value, cleanup_root=upload_dir, normalization_mode="lossless")
            return self.json_response({"job_id": job_id, "files": len(paths)}, 202)

        def static(self, path: Path):
            if not path.exists() or not path.is_file():
                return self.send_error(404)
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; object-src 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

    return Handler


def safe_piece(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in "-_. " else "_" for character in value).strip(" .")
    return cleaned[:180] or "upload"


class ExclusiveHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def __init__(self, *args, **kwargs):
        self.request_slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()

    def handle_error(self, request, client_address):
        print(json.dumps({"event": "connection_failed"}), file=sys.stderr)

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    parser = argparse.ArgumentParser(description="Context Forge")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address; use 0.0.0.0 only behind a firewall/VPN")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-root", type=Path, default=PROJECT_ROOT, help="Isolated data/output root for tests or a separate local installation")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        parser.error("Only IPv4 loopback binding is supported; remote deployment requires a separate authenticated TLS integration")
    # Acquire the port before recovering jobs, including during concurrent startup.
    server = ExclusiveHTTPServer((args.host, args.port), BaseHTTPRequestHandler)
    app = App(load_service_token(args.data_root), args.data_root,
              configured_service_token_path(args.data_root))
    server.RequestHandlerClass = handler_factory(app)
    print(f"Context Forge listening on http://{args.host}:{server.server_port}", flush=True)
    print(f"Output directory: {app.settings['output_dir']}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()


if __name__ == "__main__":
    main()
