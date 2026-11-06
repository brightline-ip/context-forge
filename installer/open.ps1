# Starts Context Forge if needed, copies its token to the clipboard and opens the local page.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$paths = Get-CfPaths $root
$code = 0
try {
    $config = Read-CfConfig $root
    Write-Host 'Starting Context Forge...'
    if (-not (Start-CfService $root ([int]$config.port) @($config.input_roots))) {
        throw "Context Forge did not start. If port $($config.port) is used by another program, stop it. Log: $(Join-Path $root 'data\service.log')"
    }
    Set-Clipboard -Value (Read-CfToken $paths.Token)
    Start-Process "http://127.0.0.1:$($config.port)"
    Write-Host 'The page is opening. Your access token is on the clipboard: paste it into the page when asked.'
    Write-Host 'Clipboard history (Win+V) may keep a copy; treat the token like a password.'
    Start-Sleep -Seconds 6
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    Read-Host 'Press Enter to close' | Out-Null
    $code = 1
}
exit $code
