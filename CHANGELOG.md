# Changelog

## 0.1.0 — 2026-10-23

First public preview.

- Deterministic normalization of PDF, DOCX, PPTX, XLSX/XLSM, EPUB, HTML, XML, JSON/JSONL, CSV/TSV, Markdown,
  RST, logs and plain text into hashed, chunked text with source locators.
- Bounded Windows.Media.Ocr (en-US) fallback for scanned PDF pages, with per-page status and resumable checkpoints.
- Loopback HTTP service with bearer-token authentication, web page, MCP server, Node CLI and PowerShell wrappers.
- `install.ps1` / `uninstall.ps1`; optional scheduled-task installer; optional Hermes plugin under `integrations/`.
- Input and output roots configurable with `CONTEXT_FORGE_INPUT_ROOTS` and `CONTEXT_FORGE_OUTPUT_DIR`
  (default input root: your Documents folder).

## 0.1.0 (security review fixes) — 2026-10-24

- OCR helper is launched with a process-scoped `-ExecutionPolicy Bypass` so OCR works under the default Restricted
  policy; web copy no longer mentions NAS shares (UNC paths are refused); SECURITY.md documents token ACL
  inheritance and unpinned-hash installs.

## 0.1.0 (reliability and installer) — 2026-11-05

- `Setup.cmd`: one-click Windows installer with Start Menu shortcuts and a clean uninstaller.
- Second-engine number check: with Tesseract installed, dates and numbers the two OCR engines read differently are
  reported (`ocr_numeric_check`, `quality_tier`). Reports only; never changes the text.
- Extraction timeouts retry once and resume from OCR checkpoints; the limit counts running time, not sleep.
- Up to 8 jobs wait while 2 run; beyond that HTTP 429. Client errors now carry the service's reason.
- Rejected requests and retries are logged to `data/intake-audit.jsonl` (route, status, reason only); service output
  goes to `data/service.log`.
- More credential paths are refused as input (`security`, `secrets`, `.gnupg`, key files).
- `uninstall.ps1` now recognizes its own running service.

## 0.1.0 (pre-release review fixes) — 2026-11-05

- A file briefly locked by antivirus or a sync client no longer marks a document as corrupted; only a missing or
  changed file does. Publication retries a locked rename.
- Job polls and chunk reads no longer re-hash every original; full validation runs once per service start and again
  when a file's size or time changes.
- The real reason for a failed document (encrypted, too many pages, unsupported) now reaches the caller.
- AES-encrypted PDFs with an empty user password, typical of patent-office downloads, are read (`cryptography` added).
- A failed document's working copy of the original is removed. Long file names are shortened for the output folder.
- Setup saves `-InputRoots` in `data\settings.json`, so a service started on demand reads the same folders; on-demand
  start also honours a non-default port; repair removes stale program files; Setup refuses a non-empty folder it did
  not create; the service no longer holds Setup's output pipe open.
