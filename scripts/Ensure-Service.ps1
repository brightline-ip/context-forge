[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TokenFile = (Join-Path (Split-Path -Parent $PSScriptRoot) 'security\service-token.txt'),
    [int]$Port = 8765
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($ProjectRoot)
$expectedRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if ($root -ne $expectedRoot) { throw 'Context Forge service task is bound to its reviewed project root.' }
$tokenPath = [IO.Path]::GetFullPath($TokenFile)
if (-not $tokenPath.StartsWith($root + [IO.Path]::DirectorySeparatorChar) -or -not (Test-Path -LiteralPath $tokenPath -PathType Leaf)) {
    throw 'Context Forge token file is missing or outside the project root.'
}
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Context Forge project Python is missing.' }
$token = [IO.File]::ReadAllText($tokenPath).Trim()
if ($token -notmatch '^[A-Za-z0-9_-]{43,256}$') { throw 'Context Forge token file is invalid.' }
$headers = @{ Authorization = 'Bearer ' + $token }
$uri = "http://127.0.0.1:$Port/api/status"
try {
    $status = Invoke-RestMethod -Uri $uri -Headers $headers -TimeoutSec 5
    if ($status.service -cne 'Context Forge' -or $status.agent_api_version -ne 1 -or
        $status.security_revision -ne 3 -or $status.auth_required -ne $true -or $status.ok -ne $true) {
        throw 'The listener is not the reviewed Context Forge service identity.'
    }
    return
} catch {
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($listeners.Count) { throw 'A listener exists but authenticated Context Forge identity validation failed.' }
}
$env:CONTEXT_FORGE_TOKEN_FILE = $tokenPath
& (Join-Path $root 'start.ps1') -HostAddress 127.0.0.1 -Port $Port
exit $LASTEXITCODE
