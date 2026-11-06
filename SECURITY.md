# Security policy

## Supported versions

Public preview 0.1.x. Only the latest 0.1.x release receives fixes.

## Reporting a vulnerability

Please report privately through a GitHub private security advisory on this repository (Security tab,
"Report a vulnerability"). Do not open a public issue for a suspected vulnerability, and do not attach real
client documents to a report; use synthetic files.

## What Context Forge trusts

- **Everything running as your Windows account.** The service has one bearer token and no per-client,
  per-profile or per-tenant authorization. Any local program that can read `security\service-token.txt` (or
  your environment) can read every allowed input root and every output.
- **The loopback interface.** The service binds to 127.0.0.1 only. Host and Origin checks defend it against
  hostile web pages in your browser; they are not a network access-control system. Remote access is not
  supported and needs a separate design.
- **The install directory's ACLs.** The token file is created readable only by your account, SYSTEM and
  Administrators. Output, archived originals, OCR checkpoints and the SQLite database are plaintext and
  inherit the folder's ACLs.
- **Token ACL inheritance.** `scripts\initialize-authentication.ps1` writes the token with inheritance removed and
  grants only your account, SYSTEM and Administrators, plus any extra non-broad grants that already exist on a
  deliberately protected install directory. It verifies that Everyone, Authenticated Users and builtin Users have no
  allow rule. Review the install directory's ACL before relying on this.
- **Supply chain.** `install.ps1` installs version-pinned Python and npm packages (`requirements-lock.txt`,
  `package-lock.json`); they are not hash-pinned. Review dependencies if your threat model includes a compromised
  package index. The OCR helper script and the optional page renderer run with `-ExecutionPolicy Bypass` for that one
  process only, from fixed paths inside the install; the OCR helper's SHA-256 is recorded in the OCR configuration.

- **Shared machines.** Clients send the token to whatever listens on the configured port before checking that it is
  Context Forge, so another program holding that port could capture it. The port is local to this computer; this is
  one more reason not to run Context Forge where other people or untrusted programs share your account or machine.
- **Clipboard.** The **Open Context Forge** shortcut puts the token on the clipboard. Clipboard history and cloud
  clipboard sync, if you turned them on, may keep or copy it.
- **Provenance in Markdown.** `normalized.md` marks sections with comments that a document's own text could imitate.
  `chunks.jsonl` carries locators as structured data; treat it as the authoritative citation source.

Documents are untrusted input. Parsing runs in a child process with a time limit and a memory cap, archives
are checked for size and compression ratio, and path intake is restricted to allowed roots. This is not a
filesystem or network sandbox; do not process deliberately hostile files on a machine you care about. Text
extracted from documents is data and must not be treated as instructions by any assistant that reads it.

## Local-only data

Context Forge has no telemetry and makes no outbound network connections of its own. The only optional
outbound-capable feature is enrichment against an Ollama endpoint, which is restricted to a numeric loopback
address. `install.ps1` contacts PyPI and npm to download pinned packages.
