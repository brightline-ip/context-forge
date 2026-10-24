param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Project Python environment missing; follow docs/OPERATIONS.md setup."
}
if ($env:CONTEXT_FORGE_TOKEN -and $env:CONTEXT_FORGE_TOKEN_FILE) {
    throw 'Configure CONTEXT_FORGE_TOKEN or CONTEXT_FORGE_TOKEN_FILE, not both.'
}
if (-not $env:CONTEXT_FORGE_TOKEN -and -not $env:CONTEXT_FORGE_TOKEN_FILE) {
    $env:CONTEXT_FORGE_TOKEN_FILE = Join-Path $projectDir 'security\service-token.txt'
}
if (-not $env:CONTEXT_FORGE_TOKEN -and -not (Test-Path -LiteralPath $env:CONTEXT_FORGE_TOKEN_FILE -PathType Leaf)) {
    throw 'Context Forge authentication is not initialized; run scripts\initialize-authentication.ps1 -Apply as the intended operator.'
}
Set-Location -LiteralPath $projectDir
$serverArgs = @("-m", "context_forge.server", "--host", $HostAddress, "--port", $Port)
& $pythonExe @serverArgs
