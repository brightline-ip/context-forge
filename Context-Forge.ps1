<#
.SYNOPSIS
  Command-line wrapper for a locally running Context Forge service (starts it on demand).
.EXAMPLE
  .\Context-Forge.ps1 -Status
  .\Context-Forge.ps1 -Path "$HOME\Documents\contract.pdf"
  .\Context-Forge.ps1 -JobId JOB_ID -WaitSeconds 10
  .\Context-Forge.ps1 -DocumentId FILE_ID -Offset 0 -Limit 2
#>
param(
    [string[]]$Path,
    [string[]]$JobId,
    [string]$DocumentId,
    [string]$ContinueOcr,
    [string]$CompleteOcr,
    [ValidateRange(1,16)][int]$MaxOcrBatches = 8,
    [ValidateRange(10,600)][int]$OcrTimeoutSeconds = 300,
    [int]$Offset = 0,
    [int]$Limit = 2,
    [string]$Text,
    [switch]$Recursive,
    [switch]$Status,
    [int]$WaitSeconds = 2
)
$ErrorActionPreference = 'Stop'
$request = if ($Status) {
    @{action = 'status'}
} elseif ($ContinueOcr) {
    @{action = 'continue-ocr'; document_id = $ContinueOcr}
} elseif ($CompleteOcr) {
    @{action = 'complete-ocr'; document_id = $CompleteOcr; max_batches = $MaxOcrBatches; timeout_seconds = $OcrTimeoutSeconds}
} elseif ($DocumentId) {
    @{action = 'read'; document_id = $DocumentId; offset = $Offset; limit = $Limit}
} elseif ($JobId) {
    @{action = 'jobs'; job_ids = $JobId; wait_seconds = $WaitSeconds}
} elseif ($PSBoundParameters.ContainsKey('Text')) {
    @{action = 'text'; text = $Text}
} elseif ($Path) {
    @{action = 'files'; paths = $Path; recursive = [bool]$Recursive; wait_seconds = $WaitSeconds}
} else {
    throw 'Supply -Path, -Text, -JobId, -DocumentId, -ContinueOcr, -CompleteOcr, or -Status.'
}
$node = (Get-Command node -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $node) { throw 'Node.js was not found on PATH; install Node.js 20 or later.' }
$cli = Join-Path $PSScriptRoot 'integrations\cli.mjs'
$previousEncoding = $OutputEncoding
try {
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $request | ConvertTo-Json -Depth 10 -Compress | & $node $cli
} finally {
    $OutputEncoding = $previousEncoding
}
if ($LASTEXITCODE -ne 0) { throw 'Context Forge request failed.' }
