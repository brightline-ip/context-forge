from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import threading
import time
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .audit import append_event
from .enrich import enrich
from .extractors import Extraction, Section
from .artifacts import (ARTIFACT_SCHEMA_VERSION, ArtifactIntegrityError, build_artifacts,
                        validate_artifacts, write_artifacts)
from .normalize import normalize, salience_pack
from .ocr_crosscheck import check_recognized_pages, conflict_pages
from .publication import JOURNAL_SCHEMA_VERSION, PublicationError, PublicationJournal, atomic_write_json
from .safety import BusyError, InputError, MAX_FILES, MAX_TEXT_CHARS, safe_error, validate_file
from .worker_runtime import WorkerRuntimeError, read_extraction_output, run_extraction_worker


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_name(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in "-_. " else "_" for character in value).strip(" .")
    return cleaned[:160] or "document"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_document_package(directory: Path, manifest: dict) -> None:
    validate_artifacts(directory, manifest)
    extension = manifest.get("format")
    if not isinstance(extension, str) or not re.fullmatch(r"\.[a-z0-9]{1,10}", extension):
        raise ArtifactIntegrityError("invalid original format")
    original = directory / ("original" + extension)
    if (not original.is_file()
            or original.stat().st_size != manifest.get("source_bytes")
            or sha256_file(original) != manifest.get("source_sha256")):
        raise ArtifactIntegrityError("archived original failed integrity validation")


MAX_ACTIVE_JOBS = 2
MAX_WAITING_JOBS = 8


def quality_status(manifest: dict) -> tuple[str, list[str]]:
    """Report extraction quality separately from the processing lifecycle."""
    if manifest.get("format") != ".pdf":
        return "ready", []
    metadata = manifest.get("metadata", {})
    ocr = metadata.get("ocr", {})
    reasons = []
    if ocr.get("pending_pages"):
        reasons.append("pending_ocr_pages")
        return "needs_ocr", reasons
    if ocr.get("unresolved_pages"):
        reasons.append("blank_or_failed_ocr_pages")
    if ocr.get("recognized_pages"):
        reasons.append("unverified_ocr_text")
    if conflict_pages(metadata.get("ocr_numeric_check")):
        reasons.append("ocr_numeric_disagreement")
    if metadata.get("pdf_layout_pages"):
        reasons.append("layout_sensitive_native_text")
    if metadata.get("pdf_layout_risk_pages"):
        reasons.append("layout_extraction_rejected")
    return ("needs_review", reasons) if reasons else ("ready", [])


def quality_tier(manifest: dict) -> str:
    """Which review a document needs. quality_status keeps its values for existing consumers,
    but says needs_review for every OCR page; this separates clean OCR from real problems."""
    if manifest.get("format") != ".pdf":
        return "ready"
    metadata = manifest.get("metadata", {})
    ocr = metadata.get("ocr", {})
    if ocr.get("pending_pages"):
        return "incomplete"
    if ocr.get("unresolved_pages"):
        return "ocr_problems"
    if conflict_pages(metadata.get("ocr_numeric_check")):
        return "ocr_numbers_disputed"
    if metadata.get("pdf_layout_pages") or metadata.get("pdf_layout_risk_pages"):
        return "layout_review"
    return "ocr_unverified" if ocr.get("recognized_pages") else "ready"


class Store:
    def __init__(self, database: Path):
        database.parent.mkdir(parents=True, exist_ok=True)
        self.database = database
        self.lock = threading.Lock()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, source TEXT NOT NULL, output_dir TEXT,
                    total INTEGER DEFAULT 0, completed INTEGER DEFAULT 0,
                    failed INTEGER DEFAULT 0, options_json TEXT NOT NULL, error TEXT
                );
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
                    status TEXT NOT NULL, output_dir TEXT, error TEXT, manifest_json TEXT,
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
                CREATE TABLE IF NOT EXISTS bound_requests (
                    operation_key TEXT PRIMARY KEY, request_json TEXT NOT NULL,
                    job_id TEXT NOT NULL REFERENCES jobs(id)
                );
                CREATE TABLE IF NOT EXISTS publication_journal (
                    publication_id TEXT PRIMARY KEY REFERENCES files(id),
                    job_id TEXT NOT NULL REFERENCES jobs(id), source TEXT NOT NULL,
                    state TEXT NOT NULL, staging_dir TEXT NOT NULL, target_dir TEXT NOT NULL,
                    manifest_json TEXT, manifest_sha256 TEXT, detail TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS publication_state ON publication_journal(state,updated_at);
            """)
            db.execute("UPDATE jobs SET status='interrupted',updated_at=? WHERE status IN ('queued','running')", (now(),))
            db.execute("UPDATE files SET status='interrupted' WHERE status IN ('queued','running')")
            db.commit()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def execute(self, sql: str, values=()):
        with self.lock, self.connect() as db:
            db.execute(sql, values)
            db.commit()

    def rows(self, sql: str, values=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, values).fetchall()]


class Pipeline:
    def __init__(self, root: Path, settings: dict):
        self.root = root
        self.settings = settings
        self.store = Store(root / "data" / "context-forge.sqlite3")
        self.slots = threading.BoundedSemaphore(MAX_ACTIVE_JOBS)
        self.capacity = threading.BoundedSemaphore(MAX_ACTIVE_JOBS + MAX_WAITING_JOBS)
        self.threads = set()
        self.thread_lock = threading.Lock()
        self.resume_lock = threading.Lock()
        self.binding_lock = threading.Lock()
        self.publications = PublicationJournal(self.store, Path(settings["output_dir"]), validate_document_package, now)
        self.reconciliation = self.publications.reconcile()

    @property
    def active_jobs(self):
        with self.thread_lock:
            return len(self.threads)

    def close(self):
        with self.thread_lock:
            threads = list(self.threads)
        for thread in threads:
            thread.join()

    def bound_receipt(self, operation_key: str) -> dict:
        if not isinstance(operation_key, str) or not re.fullmatch(r'[a-f0-9]{64}', operation_key):
            raise InputError('Supply an exact operation key')
        rows = self.store.rows('SELECT request_json,job_id FROM bound_requests WHERE operation_key=?', (operation_key,))
        return {'found': bool(rows), 'operation_key': operation_key, 'job_id': rows[0]['job_id'] if rows else None,
                'request': json.loads(rows[0]['request_json']) if rows else None}

    def create_bound_job(self, source: Path, expected_sha256: str, operation_key: str) -> str:
        if not isinstance(expected_sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', expected_sha256):
            raise InputError('Supply the exact original SHA-256')
        source = source.resolve()
        request = {'path': str(source), 'expected_sha256': expected_sha256, 'recursive': False,
                   'enrich': False, 'normalization_mode': 'lossless'}
        with self.binding_lock:
            old = self.bound_receipt(operation_key)
            if old['found']:
                if old['request'] != request:
                    raise InputError('Operation key is bound to a different request')
                return old['job_id']  # No restart, including interrupted/failed jobs.
            validate_file(source)
            if sha256_file(source) != expected_sha256:
                raise InputError('Source hash does not match the request')
            return self.create_job([source], str(source), False, normalization_mode='lossless',
                                   bound_request=(operation_key, request))

    def create_job(self, paths: list[Path], source_label: str, enrich_with_qwen: bool = True, cleanup_root: Path | None = None, normalization_mode: str = "legacy", resumed_from: str | None = None, bound_request: tuple | None = None) -> str:
        if normalization_mode not in {"legacy", "lossless"}:
            raise InputError("Unsupported normalization mode")
        paths = list(dict.fromkeys(path.resolve() for path in paths))
        if not 1 <= len(paths) <= MAX_FILES:
            raise InputError("A job must contain 1-100 distinct files")
        if sum(p.stat().st_size for p in paths) > 500 * 1024 * 1024:
            raise InputError("Total job input exceeds 500 MiB")
        if not self.capacity.acquire(blocking=False):
            raise BusyError(f"{MAX_ACTIVE_JOBS} jobs are running and {MAX_WAITING_JOBS} are waiting; poll them before submitting another job")
        job_id = uuid.uuid4().hex[:12]
        options = {"enrich": enrich_with_qwen, "normalization_mode": normalization_mode}
        if bound_request:
            options['expected_sha256'] = bound_request[1]['expected_sha256']
        if resumed_from:
            options['resumed_from'] = resumed_from
        stamp = now()
        try:
            with self.store.lock, self.store.connect() as db:
                db.execute("INSERT INTO jobs(id,status,created_at,updated_at,source,total,options_json) VALUES(?,?,?,?,?,?,?)",
                           (job_id, "queued", stamp, stamp, source_label, len(paths), json.dumps(options)))
                db.executemany("INSERT INTO files(id,job_id,source,status) VALUES(?,?,?,?)",
                               [(uuid.uuid4().hex, job_id, str(path), "queued") for path in paths])
                if bound_request:
                    db.execute('INSERT INTO bound_requests VALUES(?,?,?)',
                               (bound_request[0], json.dumps(bound_request[1]), job_id))
            worker = threading.Thread(target=self._run, args=(job_id, paths, options, cleanup_root, dict(self.settings)), daemon=True)
            with self.thread_lock:
                self.threads.add(worker)
            worker.start()
        except Exception:
            with self.thread_lock:
                if 'worker' in locals():
                    self.threads.discard(worker)
            self.capacity.release()
            self.store.execute("UPDATE jobs SET status='failed',error='Worker startup failed',updated_at=? WHERE id=?", (now(), job_id))
            raise
        return job_id

    def continue_ocr(self, document_id: str) -> str:
        if not isinstance(document_id, str) or not re.fullmatch(r'[a-f0-9]{32}', document_id):
            raise InputError('Invalid document ID')
        with self.resume_lock:
            existing = self.store.rows("SELECT id,status FROM jobs WHERE json_extract(options_json,'$.resumed_from')=? ORDER BY created_at DESC LIMIT 1", (document_id,))
            if existing and existing[0]['status'] not in {'failed','interrupted','completed_with_errors'}:
                return existing[0]['id']
            rows = self.store.rows("SELECT * FROM files WHERE id=? AND status='completed'", (document_id,))
            if not rows:
                raise InputError('Continue OCR requires a completed document ID')
            manifest = json.loads(rows[0]['manifest_json'])
            ocr = manifest.get('metadata', {}).get('ocr', {})
            pending = set(ocr.get('requested_pages', [])) - set(ocr.get('attempted_pages', []))
            if manifest.get('format') != '.pdf' or not pending:
                raise InputError('No unattempted OCR pages; inspect blank/failed pages manually')
            directory = Path(rows[0]['output_dir']).resolve(strict=True)
            original = Path(manifest['original_path']).resolve(strict=True)
            if original.parent != directory or original.name != 'original.pdf' or not directory.is_relative_to(Path(self.settings['output_dir']).resolve()):
                raise InputError('Stored original is outside the configured output directory')
            if sha256_file(original) != manifest['source_sha256']:
                raise InputError('Stored original failed its integrity check')
            return self.create_job([original], 'OCR continuation: ' + document_id, False,
                                   normalization_mode='lossless', resumed_from=document_id)

    def _run(self, job_id: str, paths: list[Path], options: dict, cleanup_root: Path | None, settings: dict) -> None:
        self.slots.acquire()  # the job stays 'queued' until a slot is free
        try:
            self._run_batch(job_id, paths, options, cleanup_root, settings)
        except Exception as exc:
            message = safe_error(exc)
            try:
                with self.store.lock, self.store.connect() as db:
                    db.execute("UPDATE files SET status='failed',error=? WHERE job_id=? AND status IN ('queued','running')", (message, job_id))
                    db.execute("UPDATE jobs SET status='failed',error=?,updated_at=?,failed=(SELECT count(*) FROM files WHERE job_id=? AND status='failed') WHERE id=?", (message, now(), job_id, job_id))
            except sqlite3.Error:
                print(json.dumps({"event": "job_state_write_failed", "job_id": job_id}), file=sys.stderr)
        finally:
            with self.thread_lock:
                self.threads.discard(threading.current_thread())
            self.slots.release()
            self.capacity.release()

    def _run_batch(self, job_id: str, paths: list[Path], options: dict, cleanup_root: Path | None, settings: dict) -> None:
        output_root = Path(settings["output_dir"]).resolve()
        batch_dir = output_root / f"batch-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{job_id}"
        batch_dir.mkdir(parents=True, exist_ok=True)
        self.store.execute("UPDATE jobs SET status='running',updated_at=?,output_dir=? WHERE id=?", (now(), str(batch_dir), job_id))
        corpus = {"job_id": job_id, "created_at": now(), "documents": []}
        for source in paths:
            file_id = self.store.rows("SELECT id FROM files WHERE job_id=? AND source=?", (job_id, str(source)))[0]["id"]
            try:
                self.store.execute("UPDATE files SET status='running' WHERE job_id=? AND source=?", (job_id, str(source)))
                manifest = self._process_one(source, batch_dir, bool(options["enrich"]), options["normalization_mode"], settings,
                                             expected_sha256=options.get('expected_sha256'),
                                             resumed_from=options.get('resumed_from'),
                                             publication_id=file_id, job_id=job_id)
                corpus["documents"].append(manifest)
            except Exception as exc:
                message = safe_error(exc)
                self.publications.fail(file_id, message)
                self.store.execute("UPDATE files SET status='failed',error=? WHERE job_id=? AND source=?", (message, job_id, str(source)))
                self.store.execute("UPDATE jobs SET failed=failed+1,updated_at=? WHERE id=?", (now(), job_id))
                corpus["documents"].append({"source": str(source), "status": "failed", "error": message})
        (batch_dir / "corpus-manifest.json").write_text(json.dumps(corpus, ensure_ascii=False, indent=2), encoding="utf-8")
        # In-memory results are advisory: an integrity check can revoke an earlier
        # file while a later file is still running.  Finalize from one authoritative
        # database snapshot so that reconciliation_required is never overwritten.
        with self.store.lock, self.store.connect() as db:
            counts = db.execute(
                "SELECT count(*),sum(status='completed'),"
                "sum(status IN ('failed','reconciliation_required')) FROM files WHERE job_id=?",
                (job_id,),
            ).fetchone()
            total, completed, failed = counts[0], counts[1] or 0, counts[2] or 0
            final_status = "completed" if total == completed else "completed_with_errors"
            db.execute(
                "UPDATE jobs SET status=?,completed=?,failed=?,error=?,updated_at=? WHERE id=?",
                (final_status, completed, failed,
                 None if not failed else "One or more files failed or are unavailable", now(), job_id),
            )
        if cleanup_root and final_status == "completed" and cleanup_root.resolve().is_relative_to((self.root / "data" / "uploads").resolve()):
            shutil.rmtree(cleanup_root, ignore_errors=True)

    def _process_one(self, source: Path, batch_dir: Path, use_qwen: bool, normalization_mode: str = "legacy", settings: dict | None = None, expected_sha256: str | None = None, resumed_from: str | None = None, *, publication_id: str | None = None, job_id: str | None = None) -> dict:
        settings = settings or dict(self.settings)
        publication_id = publication_id or uuid.uuid4().hex
        job_id = job_id or "direct-" + publication_id
        validate_file(source)
        if expected_sha256 is not None and sha256_file(source) != expected_sha256:
            raise InputError('Source changed after bound request acceptance')
        if shutil.disk_usage(batch_dir).free < source.stat().st_size * 3 + 100 * 1024 * 1024:
            raise InputError("Insufficient free disk space for safe processing")
        started = time.monotonic()
        source_stat = source.stat()
        document_dir, published_dir = self.publications.reserve(publication_id, job_id, source, batch_dir,
                                                                 safe_name(source.stem))
        document_dir.mkdir(parents=True, exist_ok=False)
        original = document_dir / ("original" + source.suffix.lower())
        shutil.copyfile(source, original)
        digest = sha256_file(original)
        if expected_sha256 is not None and digest != expected_sha256:
            raise InputError('Source changed after bound request acceptance')
        if digest != sha256_file(source):
            raise InputError("Source changed during intake; retry after the source is stable")
        extraction_path = document_dir / "extraction.json"
        checkpoint_args = []
        if source.suffix.lower() == '.pdf':
            from .pdf_ocr import CHECKPOINT_VERSION
            checkpoint_root = self.root / 'data' / 'ocr-progress' / CHECKPOINT_VERSION
            checkpoint_root.mkdir(parents=True, exist_ok=True)
            checkpoint_args = ['--ocr-checkpoint', str(checkpoint_root / (digest + '.sqlite3'))]
        timed_out = False
        while True:
            try:
                extraction_path.unlink(missing_ok=True)
                runtime = run_extraction_worker(Path(sys.executable),
                                                [str(original), str(extraction_path), *checkpoint_args],
                                                Path(__file__).resolve().parents[1], document_dir, 120)
                result = read_extraction_output(extraction_path, MAX_TEXT_CHARS * 8)
                extraction = Extraction([Section(**s) for s in result["sections"]], result["metadata"], result["warnings"])
                extraction.metadata["worker_runtime"] = runtime.metadata()
                break
            except WorkerRuntimeError as exc:
                if exc.code == "timeout" and not timed_out:
                    # Usually a busy machine; OCR checkpoints mean the retry resumes rather than restarts.
                    timed_out = True
                    append_event(self.root / "data" / "intake-audit.jsonl", {"event": "extraction_timeout_retried", "job_id": job_id, "format": source.suffix.lower()})
                    continue
                if exc.code == "timeout":
                    raise InputError("Extraction exceeded the 120-second limit twice; worker tree was stopped. Submit the file again when the machine is less busy") from exc
                raise InputError("Extraction worker failed resource or output validation") from exc
            finally:
                extraction_path.unlink(missing_ok=True)
        if timed_out:
            extraction.warnings.append("Extraction timed out once and succeeded on an automatic retry.")
        numeric_check = check_recognized_pages(original, extraction, document_dir,
                                               cache_path=self.root / 'data' / 'ocr-progress' / 'numeric-check-v1' / (digest + '.json'))
        if numeric_check:
            extraction.metadata["ocr_numeric_check"] = numeric_check
            pages = conflict_pages(numeric_check)
            if pages:
                extraction.warnings.append(f"Two OCR engines read different numbers on page(s) {', '.join(map(str, pages))}; check dates and citations there against the original image.")
        extraction = normalize(extraction, mode=normalization_mode)
        try:
            artifacts = build_artifacts(extraction, source.name, int(settings.get("chunk_chars", 6000)),
                                        source_sha256=digest)
            write_artifacts(document_dir, artifacts)
        except ArtifactIntegrityError as exc:
            raise InputError("artifact_integrity_failed") from exc
        normalized = artifacts.normalized

        enrichment = None
        enrichment_error = None
        enrichment_attempts = 0
        if use_qwen and normalized.strip():
            result = enrich(
                salience_pack(extraction),
                settings.get("ollama_model", "qwen3.5:4b"),
                settings.get("ollama_base_url", "http://127.0.0.1:11434/v1"),
                int(settings.get("llm_timeout_seconds", 60)),
                retries=1,
            )
            enrichment, enrichment_error, enrichment_attempts = result.data, result.error, result.attempts
            if enrichment:
                (document_dir / "context-capsule.json").write_text(json.dumps(enrichment, ensure_ascii=False, indent=2), encoding="utf-8")
                capsule_lines = [f"# Context capsule: {source.name}", ""]
                for key, value in enrichment.items():
                    capsule_lines.append(f"## {key.replace('_', ' ').title()}\n")
                    if isinstance(value, list):
                        capsule_lines.extend(f"- {item}" for item in value)
                    else:
                        capsule_lines.append(str(value))
                    capsule_lines.append("")
                (document_dir / "context-capsule.md").write_text("\n".join(capsule_lines), encoding="utf-8")

        manifest = {
            "status": "completed",
            "normalization_mode": normalization_mode,
            "source": str(source),
            "original_path": str(published_dir / original.name),
            "source_name": source.name,
            "source_sha256": digest,
            "source_bytes": original.stat().st_size,
            "source_modified_utc": datetime.fromtimestamp(source_stat.st_mtime, timezone.utc).isoformat(),
            "format": source.suffix.lower(),
            "metadata": extraction.metadata,
            "warnings": extraction.warnings,
            "normalized_characters": len(normalized),
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "sections": len(artifacts.section_descriptors),
            "section_descriptors": artifacts.section_descriptors,
            "chunks": len(artifacts.chunks),
            "chunk_chars": artifacts.target_chars,
            "chunk_overlap_chars": artifacts.overlap_chars,
            "artifact_hashes": artifacts.artifact_hashes,
            "processing_seconds": round(time.monotonic() - started, 1),
            "enrichment": "completed" if enrichment else ("failed" if enrichment_error else "disabled"),
            "enrichment_error": enrichment_error,
            "enrichment_attempts": enrichment_attempts,
            "output_dir": str(published_dir),
            "outputs": [original.name, "sections.json", "normalized.md", "chunks.jsonl", "manifest.json"] + (["context-capsule.json", "context-capsule.md"] if enrichment else []),
        }
        if resumed_from:
            manifest["resumed_from"] = resumed_from
        manifest["publication"] = {"journal_schema_version": JOURNAL_SCHEMA_VERSION,
                                   "publication_id": publication_id}
        manifest["quality_status"], manifest["quality_reasons"] = quality_status(manifest)
        self.publications.staged(publication_id)
        try:
            validate_artifacts(document_dir, manifest)
        except ArtifactIntegrityError as exc:
            raise InputError("artifact_integrity_failed") from exc
        # The manifest is written last, after all canonical artifacts validate.
        atomic_write_json(document_dir / "manifest.json", manifest)
        self.publications.validated(publication_id, manifest)
        return self.publications.publish(publication_id)

    def job(self, job_id: str) -> dict | None:
        jobs = self.store.rows("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not jobs:
            return None
        job = jobs[0]
        job["options"] = json.loads(job.pop("options_json"))
        job["files"] = self.store.rows("SELECT id,source,status,output_dir,error FROM files WHERE job_id=? ORDER BY source", (job_id,))
        for item in job["files"]:
            if item["status"] == "completed":
                try:
                    self.publications.check_available(item["id"])
                except PublicationError:
                    item["status"] = "reconciliation_required"
                    item["error"] = "Published artifacts failed integrity validation"
                    continue
                manifest = self.store.rows("SELECT manifest_json FROM files WHERE id=?", (item["id"],))[0]
                details = json.loads(manifest["manifest_json"])
                item.update({key: details[key] for key in ("source_sha256", "warnings", "chunks", "normalization_mode") if key in details})
                item['ocr'] = details.get('metadata', {}).get('ocr')
                item['ocr_summary'] = (item['ocr'] or {}).get('summary')
                item['quality_status'], item['quality_reasons'] = quality_status(details)
                item['quality_tier'] = quality_tier(details)
                item['ocr_numeric_check'] = details.get('metadata', {}).get('ocr_numeric_check')
        # check_available can transactionally revoke a publication and update the
        # parent job.  Return that same-call authoritative projection.
        with self.store.lock, self.store.connect() as db:
            fresh = dict(db.execute(
                "SELECT status,completed,failed,error,updated_at FROM jobs WHERE id=?", (job_id,)
            ).fetchone())
            file_projection = {
                row["id"]: dict(row) for row in db.execute(
                    "SELECT id,status,output_dir,error FROM files WHERE job_id=?", (job_id,)
                )
            }
        job.update(fresh)
        for item in job["files"]:
            item.update(file_projection[item["id"]])
        return job

    def read_document(self, document_id: str, offset: int = 0, limit: int = 2) -> dict:
        if offset < 0 or not 1 <= limit <= 5:
            raise InputError("Use offset >= 0 and limit from 1 to 5")
        rows = self.store.rows("SELECT * FROM files WHERE id=?", (document_id,))
        if not rows:
            raise InputError("Document not found")
        row = rows[0]
        if row["status"] != "completed":
            raise InputError("Document is not completed; inspect its job")
        try:
            manifest = self.publications.check_available(document_id)
        except PublicationError as exc:
            raise InputError("Document publication is unavailable; inspect its job") from exc
        manifest["quality_status"], manifest["quality_reasons"] = quality_status(manifest)
        manifest["quality_tier"] = quality_tier(manifest)
        chunks = []
        with (Path(row["output_dir"]) / "chunks.jsonl").open(encoding="utf-8") as stream:
            for index, line in enumerate(stream):
                if index < offset:
                    continue
                if len(chunks) >= limit:
                    break
                item = json.loads(line)
                if len(item["text"]) > 12000:
                    raise InputError("Legacy chunk exceeds the read limit; re-normalize the original")
                if hashlib.sha256(item["text"].encode("utf-8")).hexdigest() != item["sha256"]:
                    raise InputError("Stored chunk failed its integrity check; re-normalize the original")
                chunks.append(item)
        next_offset = offset + len(chunks)
        return {"document_id": document_id, "manifest": manifest,
                "normalized_path": str(Path(row["output_dir"]) / "normalized.md"),
                "chunks": chunks, "offset": offset,
                "next_offset": next_offset if next_offset < manifest["chunks"] else None}

    def jobs(self, limit: int = 50) -> list[dict]:
        return self.store.rows("SELECT id,status,created_at,updated_at,source,output_dir,total,completed,failed,error FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
