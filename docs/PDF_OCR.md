# Local PDF OCR fallback

Implementation note; basic mechanical checking is not substantive verification.

## Pinned configuration

The recognizer is not the limiting factor; partial results come from the per-call
page cap, so every page gets an explicit status record.

- Engine `Windows.Media.Ocr`, language `en-US`, render 200 dpi (`RENDER_DPI`),
  longest side capped at 2600 px. 300 dpi was measured and gave
  no agreement gain at 1.4x the time, so 200 dpi stays. The helper echoes
  `render_dpi` and `max_render_pixels`; the adapter rejects any other value.
- The engine has no version API. `ocr.config.engine_build` records the Windows
  build; `config_sha256` hashes the configuration, adapter, helper and build.
  A checkpoint written under another `config_sha256` is discarded and rebuilt
  (one warning), never reused silently. Checkpoints now live under
  `data/ocr-progress/windows-ocr-checkpoint-v2/`; v1 files are left untouched.
- One extraction call attempts up to 64 new pages (`MAX_PAGES_PER_CALL`) in
  helper batches of eight, and starts no new batch after 75 seconds
  (`TIME_BUDGET_SECONDS`); the worker deadline stays 120 seconds. A 50-page
  scan completes in one job (24 s measured) instead of seven.
- `ocr.pages` holds one record per page: `status` (`native`, `recognized`,
  `blank`, `failed`, `pending`), `chars`, `text_sha256`, `render` and `shape`
  (word count and alphabetic-word ratio: cheap text-shape metrics, not a
  recognition confidence). `confidence` is null and `confidence_available` is
  false on every record because the engine exposes none.
- `ocr.status` is `complete`, `incomplete`, `pages_unresolved`,
  `unavailable_or_failed` or `not_needed`; `ocr.summary` and the job file's
  `ocr_summary` say it in one line. A helper failure mid-run keeps the earlier
  batches, marks only the failing batch `failed`, and leaves the rest `pending`.
- The per-page "no extractable text" warnings are consolidated into one line
  with page ranges; `metadata.pdf_empty_native_pages` lists the pages.
- A checkpoint path longer than the Windows path limit fails with
  `OcrCheckpointPathError` instead of a bare SQLite error.
- Regression: `tests/test_pdf_ocr_benchmark.py` runs two hash-pinned synthetic scanned PDFs from
  `test-fixtures/synthetic-scans/` (invented text rendered as images by
  `scripts/make_ocr_fixtures.py`) and checks every page's status, the pinned configuration,
  word recall against the known rendered text, and anchor strings. It is skipped when the
  Windows en-US OCR language pack is unavailable.

The bounded OCR adapter is local only. It never sends document images to a model or cloud OCR service.

## Processing

First extract native text with pypdf in all standard orientations. Only pages whose extracted text is empty are eligible for OCR. The Windows PDF renderer rasterizes selected pages in memory and the installed Windows English OCR engine recognizes their text. Original PDF bytes are not rewritten. Native-text sections stay unchanged by OCR; subsequent normalization follows the caller's existing whitespace policy.

Provenance records requested, attempted, recognized and unresolved page numbers; engine/language; policy/version; adapter/helper hashes; and `recognition_verified: false`. Recognized text has an explicit review warning and retains its original page locator. Earlier empty-native-text warnings remain as evidence of why OCR was invoked.

Context Forge retains its job status `completed` when processing finishes, even if the manifest has warnings or no chunks. Read `metadata.ocr`, warnings and chunks before claiming usable coverage. Job files with OCR-derived text report `quality_status` `needs_review`; still-empty pages report `needs_ocr`. The text is retained but is never silently treated as verified extraction.

## Limits

- Windows only, using the already installed English recognizer. Missing backend/language fails visibly without discarding native text.
- At most eight previously unattempted empty-text pages per extraction call are attempted. The bounded completion command may chain 1-16 calls and stops on its cap, timeout, failure, or lack of progress. There is no unbounded all-pages loop.
- Helper timeout: sixty seconds per batch, with per-operation waits. The extraction-worker deadline remains 120 seconds per call.
- Helper starts hidden with no profile, no shell-built document commands and no inherited provider credentials. A kill-on-close Windows job applies a process memory limit; helper output is bounded and scratch files are cleaned. These controls are not a complete filesystem/network sandbox.
- Render dimensions are capped. No automatic language detection, handwriting recovery, mathematical-formula interpretation, table reconstruction or OCR correction is claimed.
- A page containing native text plus a scanned image is not automatically OCRed. Existing incorrect OCR text is not automatically replaced. Such mixed-content or damaged-text pages still need review or a separately requested re-OCR operation.
- OCR is approximate. “Lossless normalization” refers to preserving the extracted text under the normalizer's rules, not to perfect recognition of images.

## Resumable OCR

Each service keeps its own protected `data/ocr-progress/windows-ocr-checkpoint-v2/` directory. Checkpoints contain OCR-derived source text and must receive the same access and backup protections as the originals. They are not appropriate for source control. The source SHA-256, native-section fingerprint, adapter hash and helper hash bind each checkpoint. A SQLite transaction serializes work on the same checkpoint; an interrupted batch rolls back while earlier committed batches remain available. Changed or malformed checkpoint identities fail closed. Checkpoints are not portable across adapter changes without an explicit migration.

The manifest adds `current_batch_pages`, cumulative `attempted_pages`, `pending_pages`, `failed_pages`, `continuation_available`, and a full checkpoint digest. Empty recognized pages and failed attempts do not loop automatically. No pending pages means all eligible pages were attempted, not that the text is complete or correct. A killed batch may be retried; a completed failed/blank attempt requires review rather than automatic retry.

The `continue_pdf_ocr` MCP tool and the HTTP/PowerShell equivalents continue a document:

- Pass the completed `document_id`. HTTP: `POST /api/ocr/continue` with `{"document_id":"..."}`. PowerShell: `.\Context-Forge.ps1 -ContinueOcr DOCUMENT_ID`. Poll the returned job; the new file ID is the next continuation target. Repeating the same predecessor ID reuses its continuation job (failed/interrupted jobs may be replaced).

Continuation uses the hash-checked archived original without re-reading the source, preserves earlier normalized files, and involves no LLM. Resubmitting an input is an explicit new normalization and may advance its checkpoint.

The MCP server also exposes `complete_pdf_ocr`. It performs the same continuation operation repeatedly, defaults to at most eight batches, permits at most sixteen, and reports `coverage_complete`, `batch_cap_reached`, `ocr_page_failures`, `timeout`, `no_progress`, or `failed`. A newly failed or blank attempted page stops further chaining. Coverage completion does not verify recognition, and command-line callers receive review exit code 3 when the result still needs review or has unresolved pages. `quality_status` is reported separately from the completed processing lifecycle.

Native pages with repeated dot leaders use pypdf layout extraction to preserve table-of-contents and index columns only when layout output contains exactly the same multiset of non-whitespace characters as plain output. Other pages retain plain all-orientation extraction. Selected pages are recorded in `metadata.pdf_layout_pages`; rejected candidates are recorded in `metadata.pdf_layout_risk_pages`. Both are warned and marked `needs_review`.

Older v1 results have no checkpoint. Their first explicitly requested continuation rebuilds the first bounded batch once; subsequent calls reuse saved progress. Previously completed records are never swept or replayed. The continuation route requires a completed record; if an extraction worker was killed before producing one, resubmit the retained input through its existing authorized intake route to reuse committed progress where available.

## Verification and operation

Synthetic tests exercise native-only, scanned and separate native/scanned pages; blank output, failed/oversized/wrong-page results, limits, original preservation, cache reuse and snapshot equality. Continuation tests include nine-page scans through both pipelines, duplicate calls, immutable prior outputs, transaction interruption, checkpoint corruption, changed source bytes and concurrent checkpoint locking. These are software regression checks, not independent validation of OCR accuracy.

Windows OCR reference: [Microsoft Windows OCR API](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine). The implementation uses the installed Windows engine rather than adding a Tesseract installation.
