# Optional Hermes plugin

`context-forge/` is a context-engine plugin for the Hermes agent framework. It sends only the newest
outbound user message to the local Context Forge service (`POST /api/normalize/text`, lossless mode),
independently recomputes the permitted whitespace normalization, and falls back to the original text if
the service is unavailable or returns anything else. Stored conversation history is never rewritten.
It writes counts and hashes (no text) to `preflight.jsonl` under the Hermes state directory.

Context Forge itself does not depend on this plugin.

## Install

1. Install and start Context Forge (see the main README).
2. Copy the `context-forge` folder into your Hermes plugins directory (see your Hermes documentation).
3. Tell the plugin where the token is, using **one** of:
   - `CONTEXT_FORGE_TOKEN_FILE` = full path to the token file, or
   - `CONTEXT_FORGE_HOME` = the Context Forge install directory (the plugin reads `security\service-token.txt`), or
   - `CONTEXT_FORGE_TOKEN` = the token value (managed environments only).
   With none of these set, the plugin finds no token and leaves messages untouched.
4. Restart Hermes sessions so the plugin loads.

The plugin only talks to the loopback service. It requires the `agent.context_compressor` module that
Hermes provides, so it cannot be imported outside Hermes.
