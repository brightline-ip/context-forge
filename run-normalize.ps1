param(
    [Parameter(Mandatory=$true)][string]$Path,
    [switch]$NoEnrichment,
    [switch]$Enrich,
    [string]$Server = "http://127.0.0.1:8765"
)
$headers = @{}
$directToken = $env:CONTEXT_FORGE_TOKEN
$tokenFile = $env:CONTEXT_FORGE_TOKEN_FILE
if ($directToken -and $tokenFile) { throw 'Configure CONTEXT_FORGE_TOKEN or CONTEXT_FORGE_TOKEN_FILE, not both.' }
if (-not $directToken) {
    if (-not $tokenFile) { $tokenFile = Join-Path $PSScriptRoot 'security\service-token.txt' }
    if (-not (Test-Path -LiteralPath $tokenFile -PathType Leaf)) { throw 'Context Forge authentication is not initialized.' }
    $directToken = [IO.File]::ReadAllText($tokenFile, [Text.Encoding]::UTF8)
}
if ($directToken -notmatch '^[A-Za-z0-9_-]{43,256}$') { throw 'Context Forge bearer credential is invalid.' }
$endpoint = [Uri]$Server
if ($endpoint.Scheme -ne 'http' -or $endpoint.Host -notin @('127.0.0.1','localhost') -or $endpoint.UserInfo -or $endpoint.Query -or $endpoint.Fragment -or $endpoint.AbsolutePath -ne '/') {
    throw 'Context Forge requires a loopback HTTP base URL without credentials or query parameters.'
}
$headers.Authorization = "Bearer $directToken"
$status = Invoke-RestMethod -Uri "$($Server.TrimEnd('/'))/api/status" -Method Get -Headers $headers -TimeoutSec 10 -MaximumRedirection 0
if ($status.service -ne 'Context Forge' -or $status.security_revision -ne 3 -or $status.auth_required -ne $true) {
    throw 'The listener is not authenticated Context Forge security revision 3.'
}
$body = @{ path = $Path; recursive = $true; enrich = [bool]$Enrich -and -not $NoEnrichment; normalization_mode = 'lossless' } | ConvertTo-Json
Invoke-RestMethod -Uri "$($Server.TrimEnd('/'))/api/jobs/path" -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 10 -MaximumRedirection 0
