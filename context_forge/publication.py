from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Callable


JOURNAL_SCHEMA_VERSION = 1
STATES = {"RESERVED", "STAGED", "VALIDATED", "PUBLISHED", "FAILED", "CONFLICT", "RECONCILIATION_REQUIRED"}
LEGACY_INTEGRITY_KEY = "_publication_legacy_integrity"


class PublicationError(RuntimeError):
    pass


def _encoded(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: dict) -> str:
    return hashlib.sha256(_encoded(value).encode("utf-8")).hexdigest()


def atomic_write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_legacy_artifacts(directory: Path, manifest: dict, expected: dict | None = None) -> dict:
    """Validate the pre-artifact-schema package and return exact byte bindings."""
    if LEGACY_INTEGRITY_KEY in manifest or manifest.get("artifact_schema_version") is not None:
        raise PublicationError("legacy manifest identity is invalid")
    directory = directory.resolve()
    if Path(manifest.get("output_dir", "")).resolve() != directory:
        raise PublicationError("legacy output directory binding mismatch")
    original = Path(manifest.get("original_path", "")).resolve()
    if original.parent != directory or not original.is_file():
        raise PublicationError("legacy original binding mismatch")
    source_sha256 = manifest.get("source_sha256")
    if not isinstance(source_sha256, str) or _file_sha256(original) != source_sha256:
        raise PublicationError("legacy original hash mismatch")
    normalized = directory / "normalized.md"
    chunks_path = directory / "chunks.jsonl"
    normalized_bytes = normalized.read_bytes()
    normalized_bytes.decode("utf-8")
    chunk_bytes = chunks_path.read_bytes()
    lines = chunk_bytes.decode("utf-8").splitlines()
    if len(lines) != manifest.get("chunks"):
        raise PublicationError("legacy chunk count mismatch")
    source_name = manifest.get("source_name")
    for index, line in enumerate(lines, 1):
        row = json.loads(line)
        text = row.get("text")
        if (row.get("index") != index or not isinstance(text, str)
                or row.get("sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest()
                or row.get("source_sha256") != source_sha256 or row.get("source_file") != source_name):
            raise PublicationError("legacy chunk integrity mismatch")
    observed = {
        "manifest_sha256": _digest(manifest),
        "original_name": original.name,
        "original_sha256": source_sha256,
        "normalized_sha256": hashlib.sha256(normalized_bytes).hexdigest(),
        "chunks_sha256": hashlib.sha256(chunk_bytes).hexdigest(),
    }
    if expected is not None and observed != expected:
        raise PublicationError("legacy artifact byte binding mismatch")
    return observed


class PublicationJournal:
    """Process-crash reconciliation for immutable document directories.

    Atomic rename and SQLite transactions make restart decisions deterministic. This
    intentionally does not claim power-loss durability or filesystem/network isolation.
    """

    def __init__(self, store, output_root: Path, validator: Callable[[Path, dict], None], clock: Callable[[], str]):
        self.store = store
        self.output_root = output_root.resolve()
        self.validator = validator
        self.clock = clock

    def _paths(self, row: dict) -> tuple[Path, Path]:
        staging = Path(row["staging_dir"]).resolve()
        target = Path(row["target_dir"]).resolve()
        if not staging.is_relative_to(self.output_root) or not target.is_relative_to(self.output_root):
            raise PublicationError("publication path escapes output root")
        return staging, target

    def reserve(self, publication_id: str, job_id: str, source: Path, batch_dir: Path, name: str) -> tuple[Path, Path]:
        target = (batch_dir / f"{name}-{publication_id}").resolve()
        staging = (batch_dir / f".{name}-{publication_id}.staging").resolve()
        if not target.is_relative_to(self.output_root) or not staging.is_relative_to(self.output_root):
            raise PublicationError("publication path escapes output root")
        stamp = self.clock()
        with self.store.lock, self.store.connect() as db:
            old = db.execute("SELECT * FROM publication_journal WHERE publication_id=?", (publication_id,)).fetchone()
            if old:
                old = dict(old)
                if (old["job_id"], old["source"], old["staging_dir"], old["target_dir"]) != (job_id, str(source), str(staging), str(target)):
                    db.execute("UPDATE publication_journal SET state='CONFLICT',detail=?,updated_at=? WHERE publication_id=?",
                               ("reservation identity mismatch", stamp, publication_id))
                    raise PublicationError("publication reservation conflicts with existing identity")
                return staging, target
            db.execute("INSERT INTO publication_journal(publication_id,job_id,source,state,staging_dir,target_dir,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       (publication_id, job_id, str(source), "RESERVED", str(staging), str(target), stamp, stamp))
        return staging, target

    def transition(self, publication_id: str, expected: str, state: str, *, manifest: dict | None = None, detail: str | None = None) -> None:
        if expected not in STATES or state not in STATES:
            raise ValueError("unknown publication state")
        manifest_json = _encoded(manifest) if manifest is not None else None
        manifest_sha256 = _digest(manifest) if manifest is not None else None
        with self.store.lock, self.store.connect() as db:
            result = db.execute(
                "UPDATE publication_journal SET state=?,manifest_json=COALESCE(?,manifest_json),manifest_sha256=COALESCE(?,manifest_sha256),detail=?,updated_at=? WHERE publication_id=? AND state=?",
                (state, manifest_json, manifest_sha256, detail, self.clock(), publication_id, expected),
            )
            if result.rowcount != 1:
                row = db.execute("SELECT state FROM publication_journal WHERE publication_id=?", (publication_id,)).fetchone()
                actual = row[0] if row else "missing"
                raise PublicationError(f"publication transition {expected}->{state} refused from {actual}")

    def staged(self, publication_id: str) -> None:
        self.transition(publication_id, "RESERVED", "STAGED")

    def validated(self, publication_id: str, manifest: dict) -> None:
        self.transition(publication_id, "STAGED", "VALIDATED", manifest=manifest)

    def fail(self, publication_id: str, detail: str) -> None:
        with self.store.lock, self.store.connect() as db:
            db.execute("UPDATE publication_journal SET state='FAILED',detail=?,updated_at=? WHERE publication_id=? AND state IN ('RESERVED','STAGED')",
                       (detail[:1000], self.clock(), publication_id))

    def _row(self, publication_id: str) -> dict:
        rows = self.store.rows("SELECT * FROM publication_journal WHERE publication_id=?", (publication_id,))
        if not rows:
            raise PublicationError("publication reservation is missing")
        return rows[0]

    def publish(self, publication_id: str) -> dict:
        row = self._row(publication_id)
        if row["state"] not in {"VALIDATED", "PUBLISHED"} or not row["manifest_json"]:
            raise PublicationError("publication has not been validated")
        manifest = json.loads(row["manifest_json"])
        if _digest(manifest) != row["manifest_sha256"]:
            self.transition(publication_id, row["state"], "RECONCILIATION_REQUIRED", detail="journal manifest digest mismatch")
            raise PublicationError("journal manifest digest mismatch")
        publication = manifest.get("publication", {})
        if publication.get("journal_schema_version") != JOURNAL_SCHEMA_VERSION or publication.get("publication_id") != publication_id:
            self.transition(publication_id, row["state"], "RECONCILIATION_REQUIRED",
                            detail="manifest publication identity mismatch")
            raise PublicationError("manifest publication identity mismatch")
        staging, target = self._paths(row)
        if staging.exists() and target.exists():
            self.transition(publication_id, row["state"], "CONFLICT", detail="staging and target both exist")
            raise PublicationError("publication target conflicts with retained staging directory")
        candidate = target if target.exists() else staging
        if not candidate.is_dir():
            self.transition(publication_id, row["state"], "RECONCILIATION_REQUIRED", detail="validated directory is missing")
            raise PublicationError("validated publication directory is missing")
        try:
            disk_manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
            if _digest(disk_manifest) != row["manifest_sha256"]:
                raise PublicationError("manifest differs from validated journal record")
            self.validator(candidate, disk_manifest)
        except Exception as exc:
            self.transition(publication_id, row["state"], "RECONCILIATION_REQUIRED", detail=str(exc)[:1000])
            raise PublicationError("validated publication failed reconciliation") from exc
        if candidate == staging:
            os.replace(staging, target)
            self.validator(target, disk_manifest)
        stamp = self.clock()
        with self.store.lock, self.store.connect() as db:
            current = db.execute("SELECT state FROM publication_journal WHERE publication_id=?", (publication_id,)).fetchone()
            if not current or current[0] not in {"VALIDATED", "PUBLISHED"}:
                raise PublicationError("publication state changed before database commit")
            db.execute("UPDATE files SET status='completed',output_dir=?,manifest_json=?,error=NULL WHERE id=?",
                       (str(target), json.dumps(manifest, ensure_ascii=False), publication_id))
            db.execute("UPDATE publication_journal SET state='PUBLISHED',detail=NULL,updated_at=? WHERE publication_id=?", (stamp, publication_id))
            db.execute("UPDATE jobs SET completed=(SELECT count(*) FROM files WHERE job_id=jobs.id AND status='completed'),failed=(SELECT count(*) FROM files WHERE job_id=jobs.id AND status IN ('failed','reconciliation_required')),updated_at=? WHERE id=?",
                       (stamp, row["job_id"]))
            counts = db.execute("SELECT count(*),sum(status='completed'),sum(status IN ('failed','reconciliation_required')) FROM files WHERE job_id=?",
                                (row["job_id"],)).fetchone()
            job = db.execute("SELECT status FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
            if job and job[0] == "interrupted" and counts[0] == (counts[1] or 0) + (counts[2] or 0):
                terminal = "completed_with_errors" if counts[2] else "completed"
                db.execute("UPDATE jobs SET status=?,updated_at=? WHERE id=?", (terminal, stamp, row["job_id"]))
        return manifest

    def reconcile(self) -> list[dict]:
        outcomes = []
        # Startup recovery is bounded to unfinished work. Historical PUBLISHED rows
        # are checked on access or by the explicit bounded scrub below.
        for row in self.store.rows("SELECT * FROM publication_journal WHERE state IN ('RESERVED','STAGED','VALIDATED') ORDER BY created_at,publication_id"):
            publication_id = row["publication_id"]
            staging, target = self._paths(row)
            try:
                if row["state"] == "RESERVED":
                    state = "RECONCILIATION_REQUIRED" if staging.exists() or target.exists() else "FAILED"
                    self.transition(publication_id, "RESERVED", state, detail="process stopped before staging completed")
                elif row["state"] == "STAGED":
                    candidate = target if target.exists() else staging
                    manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
                    self.validator(candidate, manifest)
                    self.validated(publication_id, manifest)
                    self.publish(publication_id)
                else:
                    self.publish(publication_id)
                outcomes.append({"publication_id": publication_id, "state": self._row(publication_id)["state"]})
            except Exception as exc:
                current = self._row(publication_id)["state"]
                if current == "STAGED":
                    self.transition(publication_id, "STAGED", "RECONCILIATION_REQUIRED",
                                    detail=("staged package failed validation: " + str(exc))[:1000])
                    current = "RECONCILIATION_REQUIRED"
                outcomes.append({"publication_id": publication_id, "state": current, "error": str(exc)})
        return outcomes

    def _revoke(self, row: dict, detail: str) -> None:
        stamp = self.clock()
        with self.store.lock, self.store.connect() as db:
            db.execute("UPDATE publication_journal SET state='RECONCILIATION_REQUIRED',detail=?,updated_at=? WHERE publication_id=?",
                       (detail[:1000], stamp, row["publication_id"]))
            db.execute("UPDATE files SET status='reconciliation_required',error=? WHERE id=?",
                       ("Published artifacts failed integrity validation", row["publication_id"]))
            counts = db.execute(
                "SELECT count(*),sum(status='completed'),sum(status IN ('failed','reconciliation_required')) "
                "FROM files WHERE job_id=?", (row["job_id"],),
            ).fetchone()
            completed, failed = counts[1] or 0, counts[2] or 0
            current = db.execute("SELECT status FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
            status = "completed_with_errors" if counts[0] == completed + failed else current[0]
            db.execute(
                "UPDATE jobs SET completed=?,failed=?,status=?,error='One or more publications are unavailable',updated_at=? WHERE id=?",
                (completed, failed, status, stamp, row["job_id"]),
            )

    def check_available(self, publication_id: str) -> dict:
        try:
            row = self._row(publication_id)
        except PublicationError:
            try:
                row = self._adopt_legacy(publication_id)
            except Exception as exc:
                files = self.store.rows("SELECT job_id,status FROM files WHERE id=?", (publication_id,))
                if files and files[0]["status"] == "completed":
                    self._revoke({"publication_id": publication_id, "job_id": files[0]["job_id"]}, str(exc))
                raise PublicationError("legacy publication failed integrity validation") from exc
        if row["state"] != "PUBLISHED" or not row["manifest_json"]:
            raise PublicationError("publication is unavailable")
        try:
            _, target = self._paths(row)
            stored = json.loads(row["manifest_json"])
            disk = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            if _digest(stored) != row["manifest_sha256"]:
                raise PublicationError("journal manifest binding mismatch")
            legacy_integrity = stored.get(LEGACY_INTEGRITY_KEY)
            if legacy_integrity is not None:
                clean = dict(stored)
                clean.pop(LEGACY_INTEGRITY_KEY)
                if _digest(disk) != legacy_integrity.get("manifest_sha256") or _digest(clean) != _digest(disk):
                    raise PublicationError("legacy manifest binding mismatch")
                _validate_legacy_artifacts(target, disk, legacy_integrity)
                return disk
            if _digest(disk) != row["manifest_sha256"]:
                raise PublicationError("manifest binding mismatch")
            self.validator(target, disk)
            return stored
        except Exception as exc:
            self._revoke(row, str(exc))
            raise PublicationError("publication failed integrity validation") from exc

    def _adopt_legacy(self, publication_id: str) -> dict:
        """Validate a pre-journal completed package completely, then adopt it once."""
        files = self.store.rows("SELECT * FROM files WHERE id=?", (publication_id,))
        if not files or files[0]["status"] != "completed" or not files[0].get("output_dir"):
            raise PublicationError("publication reservation is missing")
        file_row = files[0]
        target = Path(file_row["output_dir"]).resolve()
        if not target.is_relative_to(self.output_root):
            raise PublicationError("legacy publication path escapes output root")
        disk = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        recorded = json.loads(file_row["manifest_json"])
        if _digest(disk) != _digest(recorded):
            raise PublicationError("legacy manifest binding mismatch")
        if disk.get("artifact_schema_version") is None:
            integrity = _validate_legacy_artifacts(target, disk)
            stored = dict(disk)
            stored[LEGACY_INTEGRITY_KEY] = integrity
            detail = "validated pre-schema legacy adoption"
        else:
            self.validator(target, disk)
            stored = disk
            detail = "validated journal adoption"
        stamp = self.clock()
        staging = target.with_name("." + target.name + ".legacy-adopted")
        with self.store.lock, self.store.connect() as db:
            current = db.execute("SELECT status,output_dir,manifest_json,job_id,source FROM files WHERE id=?", (publication_id,)).fetchone()
            if not current or current[0] != "completed" or Path(current[1]).resolve() != target or _digest(json.loads(current[2])) != _digest(disk):
                raise PublicationError("legacy publication changed during adoption")
            db.execute(
                "INSERT OR IGNORE INTO publication_journal(publication_id,job_id,source,state,staging_dir,target_dir,manifest_json,manifest_sha256,detail,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (publication_id, current[3], current[4], "PUBLISHED", str(staging), str(target),
                 _encoded(stored), _digest(stored), detail, stamp, stamp),
            )
        return self._row(publication_id)

    def scrub(self, limit: int = 25, after: str = "") -> list[dict]:
        rows = self.store.rows("SELECT publication_id FROM publication_journal WHERE state='PUBLISHED' AND publication_id>? ORDER BY publication_id LIMIT ?",
                               (after, min(max(int(limit), 1), 100)))
        outcomes = []
        for row in rows:
            try:
                self.check_available(row["publication_id"])
                outcomes.append({"publication_id": row["publication_id"], "state": "PUBLISHED"})
            except PublicationError as exc:
                outcomes.append({"publication_id": row["publication_id"], "state": "RECONCILIATION_REQUIRED", "error": str(exc)})
        return outcomes
