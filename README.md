# Context Forge

**Public preview 0.1.0** · Apache-2.0 · Windows 10/11 only · by Brightline IP

Context Forge turns messy documents into clean, traceable text that you and your AI tools can work
with. Point it at PDFs, Word, PowerPoint, Excel, EPUB, HTML, CSV, JSON, Markdown or plain-text files
and it extracts the text, records where each piece came from (page, slide, sheet, section), hashes
the original and every chunk, and splits the result into model-sized chunks. For IP practitioners
this means a file you received can be handed to an assistant (or searched, or cited) without losing
track of which page said what, and without the original ever being modified.

It runs entirely on your own computer. Extraction is deterministic: no language model is involved
unless you explicitly turn on optional local enrichment (Ollama). Scanned PDF pages are recognized
with the Windows built-in OCR engine, and every OCR-derived page is flagged for human review.

You use it three ways: through an **MCP server** (so Claude Desktop, Claude Code or any MCP client can
call it), through a **PowerShell/Node command line**, or through a **local web page** at
`http://127.0.0.1:8765`.

> Not legal advice, and extraction and OCR can be incomplete or wrong. Read the [disclaimer](DISCLAIMER.md).

## Requirements

- Windows 10 or 11 (64-bit). Other operating systems are not supported.
- **OCR of scanned PDFs needs the Windows English (en-US) OCR language pack.** Without it, scanned
  pages are reported as failed and native-text files still work. Check or add it under Settings,
  Time and language, Language and region, English (United States), Language options, Optical character recognition.
- Python 3.12 or later (tested with 3.12).
- Node.js 20 or later (tested with 24). Needed for the MCP server and command-line client, not for the HTTP service alone.
- Internet access during `install.ps1` only, to download pinned packages from PyPI and npm.
- Optional: [Ollama](https://ollama.com/) on this machine for context-capsule enrichment (off by default).

## Install

From the folder where you unpacked the release, in PowerShell:

```powershell
.\install.ps1
```

This creates `.venv` from `requirements-lock.txt`, runs `npm ci --ignore-scripts`, creates a random
service token readable only by you (stored at `security\service-token.txt`), and prints an MCP
registration snippet. It does not start the service, schedule anything, or change any other settings.
If PowerShell blocks the script, run `powershell -ExecutionPolicy Bypass -File .\install.ps1` for this one run.

## Quick start

```powershell
.\start.ps1                       # starts the service on 127.0.0.1:8765 (leave this window open)
.\Context-Forge.ps1 -Status       # from another window
.\Context-Forge.ps1 -Path "$HOME\Documents\sample.pdf"
.\Context-Forge.ps1 -JobId <job id from the previous command> -WaitSeconds 10
.\Context-Forge.ps1 -DocumentId <file id> -Offset 0 -Limit 2   # read normalized chunks
```

The service only reads files under an **allowed input root**. By default that is your `Documents`
folder; see Configuration to change it. The web page at `http://127.0.0.1:8765` asks for the token once
per tab (open `security\service-token.txt` and paste it; it is kept in memory only).

Each run writes a new timestamped batch folder under `output\`. A file's folder contains:

- `original.*`: the archived source bytes, with a verified SHA-256.
- `normalized.md`: extracted text with source locators. Spacing and repeated lines are preserved.
- `chunks.jsonl`: roughly 6,000-character chunks for model context or indexing.
- `manifest.json`: provenance, hashes, metadata, warnings and OCR/enrichment status.
- `context-capsule.json` / `.md`: only if optional enrichment was requested; a routing aid, never a substitute for the text.

Supported inputs: PDF, DOCX, PPTX, XLSX/XLSM, EPUB, HTML, XML, JSON/JSONL, CSV/TSV, Markdown, RST, logs, plain text.

## Use it from an AI assistant (MCP)

The MCP server (`integrations/mcp/server.mjs`) exposes `normalize_text`, `normalize_files`,
`get_normalization_jobs`, `read_normalized_document`, `context_forge_status`, and the OCR tools
`continue_pdf_ocr` / `complete_pdf_ocr` and `normalize_attachment_bytes`. It starts the local service
on demand once the token exists. `install.ps1` prints a snippet with your real paths; the generic form is:

```json
{
  "mcpServers": {
    "context-forge": {
      "command": "C:\\Program Files\\nodejs\\node.exe",
      "args": ["C:\\Tools\\context-forge\\integrations\\mcp\\server.mjs"]
    }
  }
}
```

- **Claude Desktop**: Settings, Developer, Edit Config; add the entry above under `mcpServers` in
  `claude_desktop_config.json`, then restart Claude Desktop.
- **Claude Code**: `claude mcp add context-forge -- "C:\Program Files\nodejs\node.exe" "C:\Tools\context-forge\integrations\mcp\server.mjs"`
  (add `--scope user` to make it available in every project).
- **Other clients**: any client that launches a stdio MCP server accepts the same `command` and `args`.

Details, tool list and limits: [docs/AGENT_TOOL.md](docs/AGENT_TOOL.md).

## Configuration

Settings are environment variables set in the shell or MCP client that launches the service
(`.env.example` lists them; the file is not read automatically). A first run also writes
`data\settings.json`, which holds chunk size, the Ollama endpoint/model and roots; an environment
variable overrides the file at each start.

| Setting | Default | Meaning |
|---|---|---|
| `CONTEXT_FORGE_INPUT_ROOTS` | `%USERPROFILE%\Documents` | Folders the service may read, `;`-separated absolute local paths |
| `CONTEXT_FORGE_OUTPUT_DIR` | `<install dir>\output` | Where batches are written |
| `CONTEXT_FORGE_TOKEN_FILE` | `<install dir>\security\service-token.txt` | Bearer token file (use this *or* `CONTEXT_FORGE_TOKEN`) |
| `CONTEXT_FORGE_TOKEN` | unset | Token value for managed environments only |
| `CONTEXT_FORGE_URL` | `http://127.0.0.1:8765` | Client target; must be loopback HTTP |
| `CONTEXT_FORGE_PYTHON` | `<install dir>\.venv\Scripts\python.exe` | Python the MCP client uses to start the service |
| `start.ps1 -HostAddress / -Port` | `127.0.0.1` / `8765` | Listen address (keep it loopback) |
| `data\settings.json` | see file | `chunk_chars` (1000-12000), `ollama_base_url` (loopback only), `ollama_model` |

## Data locations

Everything lives under the install directory unless you configure otherwise: `data\` (SQLite job
database, settings, uploads, OCR checkpoints), `output\` (normalized batches and archived originals),
`security\` (token). Originals, normalized text and OCR checkpoints contain your documents in plaintext;
use an encrypted disk and keep the folder out of source control and shared sync. Nothing is sent
anywhere. There is no telemetry.

## Security model

- The service binds to IPv4 loopback only, checks Host and Origin headers, and requires a bearer token
  on every API call. The token is a random 256-bit value in a file only your account, SYSTEM and
  Administrators can read; it is never passed on a command line.
- All local programs running as you are trusted equally; there are no per-client or per-tenant
  permissions. Do not use it on a machine with untrusted local users, and do not expose the port through
  forwarding, a proxy or container port publication. Remote use needs a separate, reviewed design.
- Input is limited to the allowed input roots; network paths, alternate data streams, credential files
  and dependency folders are refused. Parsing runs in a child process with a time limit and memory cap.
  This is not a complete sandbox: do not ingest deliberately hostile files on your main machine.
- The optional scheduled-task installer (`scripts\Install-ServiceTask.ps1`) is not run by `install.ps1`.
  See [docs/OPERATIONS.md](docs/OPERATIONS.md). More: [SECURITY.md](SECURITY.md), [docs/AUTHENTICATION.md](docs/AUTHENTICATION.md).

## Limitations

- Extraction is not guaranteed complete: DOCX headers/footnotes/embedded objects, slide images, and
  spreadsheet calculated values may be missing. Always read the warnings in the manifest.
- OCR is the Windows engine, English only, approximate, with no confidence scores. At most 64 new pages
  or 75 seconds per call; continue with `-ContinueOcr` / `-CompleteOcr`. OCR text is marked as needing review.
- Single-user, single-machine. At most 2 active jobs, 50 MiB per file. See [docs/OPERATIONS.md](docs/OPERATIONS.md) and [docs/PDF_OCR.md](docs/PDF_OCR.md).
- Public preview: clean-machine installation and long-term upgrade paths have not been verified.

## Optional: Hermes plugin

`integrations/hermes-plugin/` contains an optional plugin for the Hermes agent framework that
normalizes outbound chat text. Core Context Forge does not depend on it. See its README.

## Develop and test

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --test integrations\client.test.mjs
node integrations\mcp\verify.mjs
```

## Uninstall

```powershell
.\uninstall.ps1            # removes .venv, node_modules, the optional scheduled task; keeps your data
.\uninstall.ps1 -RemoveData   # also deletes data\, output\ and security\ (asks first)
```

Then delete the folder and remove the `context-forge` entry from your MCP client configuration.

## License and disclaimer

Apache License 2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE). Read the [DISCLAIMER](DISCLAIMER.md) before relying on any output.
