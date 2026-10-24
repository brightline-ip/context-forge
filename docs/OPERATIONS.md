# Local Operations and Recovery

## Supported boundary

Context Forge is a single-user local service. Every authenticated agent shares its
allowed files and job history. Do not use the default configuration on a machine
with untrusted local users or expose its port through forwarding, a public proxy,
or container port publication. The service has no tenant identities, signed webhooks,
temporary credential issuer, scoped tokens, or replay protection for bearer tokens.
Those are not requirements of the present local-only deployment; they are required
design work before any remote hub-and-spoke deployment.

The mandatory bearer credential stays in the protected local token file or a
managed launching environment. Do not put it in URLs, CLI arguments, workflow
exports, settings.json, logs, output, backups, or source control. Initialize and
verify it with [AUTHENTICATION.md](AUTHENTICATION.md).
The parser subprocess receives a minimal operating-system environment and no
Context Forge token. No real credentials are needed for the test suite.

## Setup

Run `install.ps1` from the install directory (Windows PowerShell or PowerShell 7). It needs Python 3.12 or
later and, for the MCP server and CLI, Node.js 20 or later. It creates `.venv` from
`requirements-lock.txt` (binary wheels only), runs `npm ci --ignore-scripts` in `integrations/mcp`, creates
the service token with a user-only ACL, and prints an MCP registration snippet. It does not start the
service, register a scheduled task, or edit any client configuration. Manual equivalent:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r requirements-lock.txt
npm ci --ignore-scripts --prefix integrations/mcp
.\scripts\initialize-authentication.ps1 -Apply
.\start.ps1
```

The Python lock pins all ten runtime dependencies but does not include wheel hashes.
Audit and lint tools are developer dependencies, not needed to start the service.

## Updating

1. Stop new intake and allow existing jobs to finish. Record any interrupted jobs.
2. Make an offline backup of the database, settings, uploads and outputs (see below).
3. Stop the old Context Forge process through its owning terminal or process manager.
4. Replace the program files, keeping `data/`, `output/` and `security/`. Re-run `install.ps1` to refresh dependencies.
5. Start `start.ps1`. Do not run two services against the same data directory.
6. Through an authenticated client, confirm `/api/status` reports `security_revision: 3` and `auth_required: true`, then reload MCP connections.
7. If you use the optional Hermes plugin, reinstall it from `integrations/hermes-plugin/context-forge` (see its README).

`node integrations/mcp/verify-live.mjs` checks a running service using two MCP clients and harmless inline
text, without creating file jobs. The MCP adapter refuses an older or unauthenticated service instead of
silently using it; do not work around that by removing the revision or authentication check.

## Optional: start automatically at sign-in

`scripts/Install-ServiceTask.ps1` registers a current-user, limited-privilege scheduled task
("Context Forge Service") that starts the loopback service at sign-in and checks it every five minutes
through `scripts/Ensure-Service.ps1`. It is not run by `install.ps1`; run it yourself only if you want it
(`-WhatIf` previews). It refuses to overwrite a task it does not own. Remove it with
`Unregister-ScheduledTask -TaskName 'Context Forge Service'` or `uninstall.ps1`.

## File permissions and limits

Input roots default to the current user's `Documents` folder. Set `CONTEXT_FORGE_INPUT_ROOTS`
(absolute paths separated by `;`) to use other folders; the variable overrides `allowed_input_roots`
in `settings.json` each time the service starts. Agent credential/configuration directories and dependency directories
are excluded. The default `security` tree and an explicitly configured token file
are excluded before direct, bound, or recursive intake; uploads reject credential
paths/names and an exact token payload. Outputs default to the install directory's `output` folder (`CONTEXT_FORGE_OUTPUT_DIR` overrides it). Network paths
and NTFS alternate data streams are rejected. Real paths are checked against
allowed roots, including junction resolution.

To add a local folder permanently instead, edit `data/settings.json` while the
service is stopped. Add only the needed absolute local directories to
`allowed_input_roots` or `allowed_output_roots`, and keep `output_dir` within an
allowed output root. HTTP callers cannot expand these roots or change the model
endpoint. Secrets may exist in arbitrary document contents; directory restrictions
are not a substitute for the calling agent's authorization checks.

Limits: 2 active jobs, 16 HTTP request threads, 100 distinct files per job,
10,000 discovery entries, 50 MiB per file, 500 MiB input per job, 64 MiB upload body,
100 MiB total expanded archive data, 10,000 archive entries, and a compression
ratio guard. PDF page count is capped at 2,000 and spreadsheet cells at one million.
Each extraction runs in a child process with a 120-second timeout; extracted text
is capped at eight million characters. Chunks are 1,000-12,000 characters and reads
return at most five chunks. Model replies are capped at 64 KiB, with at most one
retry; external model endpoints, redirects and environment proxies are rejected.

Child processes have a deadline, not an OS memory sandbox. Crafted compressed PDF
streams can still cause peak memory pressure before the deadline. Do not ingest
intentionally adversarial samples on your main machine. Use a disposable,
low-privilege container or VM for such material.

## Failures and replay

Poll existing job IDs instead of re-submitting the same files. New submissions
create new archives; there is no durable idempotency key or automatic mutation
retry. HTTP 429 means the job was not admitted; wait for capacity before retrying.
If a connection is lost after POST, inspect recent jobs before submitting again.
There are no consequential external actions in this pipeline.

Completed-with-errors jobs require inspection of each file. A failed or interrupted
job may leave useful partial output. Failed upload batches keep their staged inputs;
successful uploads are removed only after their original bytes are archived.
After a crash, queued/running rows become interrupted on startup. Resume means a
new explicit submission of remaining files, not continuation of an old worker.
Never report `completed` as proof of full source coverage: inspect warnings.

The model capsule is untrusted metadata, not normalized source text or a tool
instruction. A model failure is recorded as enrichment failure; extraction results
remain available. No model API is called unless enrichment was explicitly selected.

## Storage, logs and backup

Originals, normalized text, chunks, metadata and the SQLite database contain
personal data in plaintext at the application layer. Use an encrypted disk and
restrict OS ACLs to the intended account. There is no automatic deletion of source
archives or incomplete uploads. Check disk usage weekly and define a retention
period appropriate to your data. Review inactive batch/upload folders before any
manual cleanup; do not delete originals still needed for provenance or recovery.

HTTP logs contain only event/status, not request paths, query strings, headers,
tokens or bodies. The protected bearer file is outside state/output backup and
inventory scopes; preserve it separately through an approved secret-recovery
method. Job IDs correlate work in the database. Preflight telemetry
contains counts and hashes and rotates at approximately 1 MiB plus one prior file.
Autostart suppresses console output, so use the foreground launcher to diagnose
startup failures. There is no external alerting, telemetry service or automatic
dead-letter consumer. A health response tests SQLite connectivity, not every future
parser invocation or available disk capacity.

For the simplest consistent backup, stop intake, wait for jobs, stop the service,
then back up `data/context-forge.sqlite3`, `data/settings.json`, `data/uploads`, and
all configured output roots together. Protect the backup like the originals.
Restore them to the same absolute paths with the service stopped, run SQLite
`PRAGMA quick_check`, start the service and read a known archived document to verify
its hashes. The test suite exercises SQLite backup/validation and original-file
hash preservation using disposable fixture data. Production restoration was not run.

## Checks

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --test integrations/client.test.mjs
node integrations/mcp/verify.mjs
node scripts/check_syntax.mjs
.\.venv\Scripts\ruff.exe check --isolated --select E,F --ignore E501 context_forge/*.py tests/*.py scripts/*.py integrations/hermes-plugin/context-forge/__init__.py
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pip_audit --disable-pip --no-deps -r requirements-lock.txt
npm audit --prefix integrations/mcp
```

Install `ruff` and `pip-audit` only when performing audits. Tests use mock data and an
ephemeral-port service; production state and running services are never used as test fixtures.
The OCR tests need the Windows en-US OCR language pack and are skipped without it.
