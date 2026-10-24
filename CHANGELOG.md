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
