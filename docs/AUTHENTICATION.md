# Local Bearer Authentication

Context Forge security revision 3 requires one 256-bit URL-safe bearer token for every API request. The credential belongs only to Context Forge. Do not reuse a provider, Windows, or other application credential.

The supported local store is `security\service-token.txt`. That directory is excluded from source control, inventory scans, state backups, and generated output. The token is never accepted as a command-line argument and no supplied script prints it.

Path intake rejects the entire default `security` directory and any explicitly configured `CONTEXT_FORGE_TOKEN_FILE`, including during recursive discovery and bound-file intake. Upload intake rejects security path components, configured credential filenames, and an exact credential payload even if renamed; rejected upload staging is removed. This is a defense against accidental self-ingestion, not a content classifier for arbitrary files that happen to contain a credential among other text.

## Initialize without changing the running service

`install.ps1` does this for you. To do it by hand, run these commands as the Windows account that will operate Context Forge, from the install directory. Stop any Context Forge listener first; the apply step refuses to run while this installation's own service is running.

```powershell
.\scripts\initialize-authentication.ps1
.\scripts\initialize-authentication.ps1 -Apply
```

The first command is a metadata-only preview. The apply command creates a new random 32-byte token, writes it atomically, protects inheritance, grants full control to the operator/SYSTEM/Administrators only (plus any extra non-broad grants if you deliberately protected the install directory's ACL), and verifies that Everyone, Authenticated Users, and builtin Users have no allow rule. Its JSON result contains only path, size, and principal-count metadata.

The script refuses to overwrite an existing credential. Rotation requires a separate maintenance action: stop intake and the service, create a separately protected replacement, update all explicitly configured clients, restart, run the negative/positive checks below, and only then remove the old credential. Never place either value in a command argument, URL, documentation, report, settings file, source file, or shell history.

## Start and connect

The default foreground launcher and local clients automatically use the protected file:

```powershell
.\start.ps1
.\Context-Forge.ps1 -Status
```

The Node/MCP client and PowerShell clients use the same default path, so MCP client configuration files need no embedded secret or token-file entry. The optional Hermes plugin finds the token through `CONTEXT_FORGE_TOKEN_FILE` or `CONTEXT_FORGE_HOME`.

For a different protected location, set only the file path before launching a client or service:

```powershell
$env:CONTEXT_FORGE_TOKEN_FILE = 'C:\protected\context-forge-token.txt'
```

`CONTEXT_FORGE_TOKEN` remains available for managed process environments that cannot mount a protected file. Configure either the value or the file path, never both. Child parser processes receive neither.

The browser cannot read a local credential file. Open `http://127.0.0.1:8765`, enter the credential through a trusted local secret-handling method, and use **Connect or save settings**. The page holds it only in that tab’s memory. Do not put it in the URL or browser storage, and clear any clipboard used to transfer it.

## Verify a running service

Use a harmless fixture. Do not print the credential.

```powershell
node integrations\mcp\verify-live.mjs
.\.venv\Scripts\python.exe -m unittest tests.test_security -v
node --test integrations\client.test.mjs
```

The live verifier requires security revision 3, proves missing and wrong bearer values return 401, proves the configured credential succeeds through two MCP clients, verifies a lossless inline result, and, if `CONTEXT_FORGE_INSTALLED_PLUGIN` names an installed Hermes plugin file, checks that it matches the source. It creates no file job and makes no provider/model call.

If startup says authentication is not initialized, inspect only file existence, length, owner, and ACL metadata. Do not dump the file. If a client reports an older security revision, stop the old process through its normal owner/terminal and start the current service; do not weaken the client check.
