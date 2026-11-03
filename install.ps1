<#
.SYNOPSIS
  Local setup for Context Forge: Python venv, Node packages, service token, MCP snippet.
.DESCRIPTION
  Run once from the install directory on Windows 10/11. This script downloads packages from PyPI
  and npm (pinned lock files). It does not start the service, register a scheduled task, edit
  any MCP client configuration, or change firewall or system settings.
  Re-running is safe: an existing venv is reused and an existing token is never replaced.
#>
[CmdletBinding()]
param(
    [string]$Python,
    [switch]$SkipNode,
    [switch]$SkipToken
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($PSScriptRoot)
Set-Location -LiteralPath $root

if ($PSVersionTable.PSEdition -eq 'Core' -and -not $IsWindows) { throw 'Context Forge supports Windows 10/11 only.' }

function Find-Python([string]$Requested) {
    $candidates = [Collections.Generic.List[object]]::new()
    if ($Requested) { $candidates.Add(@($Requested)) }
    else { foreach ($c in @('py', '-3.13'), @('py', '-3.12'), @('python'), @('python3')) { $candidates.Add($c) } }
    foreach ($c in $candidates) {
        $exe = $c[0]
        $extra = @($c | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        # No double quotes in arguments: Windows PowerShell 5.1 strips them when calling native programs.
        try { $v = (& $exe @extra -c 'import sys;print(sys.version_info[0],sys.version_info[1],sep=chr(46))' 2>$null) } catch { continue }
        if ($v -match '^(\d+)\.(\d+)$' -and ([int]$Matches[1] -gt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 12))) {
            return @{ Exe = $exe; Extra = $extra }
        }
    }
    throw 'Python 3.12 or later was not found. Install it from https://www.python.org/downloads/windows/ or pass -Python <path>.'
}

$venvPython = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    $py = Find-Python $Python
    Write-Host ('Creating virtual environment with ' + $py.Exe + ' ' + ($py.Extra -join ' '))
    & $py.Exe @($py.Extra) -m venv (Join-Path $root '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Creating the virtual environment failed.' }
}
Write-Host 'Installing pinned Python dependencies (requirements-lock.txt)'
& $venvPython -m pip install --disable-pip-version-check --only-binary=:all: -r (Join-Path $root 'requirements-lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installing Python dependencies failed.' }

$node = (Get-Command node -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $SkipNode) {
    if (-not $node) { throw 'Node.js 20 or later was not found. Install it from https://nodejs.org/ or re-run with -SkipNode (no MCP server or CLI).' }
    $major = [int]((& $node -p 'process.versions.node').Split('.')[0])
    if ($major -lt 20) { throw "Node.js 20 or later is required (found $major)." }
    $npm = (Get-Command npm.cmd, npm -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    Write-Host 'Installing pinned Node packages (integrations/mcp/package-lock.json)'
    & $npm ci --ignore-scripts --prefix (Join-Path $root 'integrations\mcp')
    if ($LASTEXITCODE -ne 0) { throw 'npm ci failed.' }
}

$tokenFile = Join-Path $root 'security\service-token.txt'
if ($SkipToken) {
    Write-Host 'Skipped service token creation (-SkipToken).'
} elseif (Test-Path -LiteralPath $tokenFile) {
    Write-Host 'Service token already exists; leaving it unchanged.'
} else {
    # Random 256-bit token readable only by you, SYSTEM and Administrators. The value is never printed.
    & (Join-Path $root 'scripts\initialize-authentication.ps1') -Apply | Out-Null
    Write-Host "Created service token: $tokenFile (user-only ACL; the value is not displayed)"
}

$nodePath = if ($node) { $node } else { 'node' }
$server = Join-Path $root 'integrations\mcp\server.mjs'
$snippet = [ordered]@{ mcpServers = [ordered]@{ 'context-forge' = [ordered]@{ command = $nodePath; args = @($server) } } } | ConvertTo-Json -Depth 5
Write-Host ''
Write-Host 'Install complete. Nothing was started or registered.'
Write-Host ''
Write-Host 'Start the service (the MCP server can also start it on demand):'
Write-Host "  $root\start.ps1"
Write-Host ''
Write-Host 'MCP client registration (merge into your client configuration):'
Write-Host $snippet
Write-Host ''
Write-Host "Claude Code: claude mcp add context-forge -- `"$nodePath`" `"$server`""
Write-Host ''
Write-Host 'Input files must live under an allowed input root (default: your Documents folder);'
Write-Host 'see README.md, Configuration. Scanned-PDF OCR needs the Windows en-US OCR language pack.'
