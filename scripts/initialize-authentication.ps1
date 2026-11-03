param([switch]$Apply)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$securityDir = Join-Path $root 'security'
$tokenFile = Join-Path $securityDir 'service-token.txt'
$broad = @('S-1-1-0', 'S-1-5-11', 'S-1-5-32-545')
$current = [Security.Principal.WindowsIdentity]::GetCurrent().User
$system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$administrators = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')

$rootItem = Get-Item -LiteralPath $root -Force
if ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'Refusing an authentication root reached through a reparse point.'
}
$rootAcl = Get-Acl -LiteralPath $root
$approved = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
foreach ($sid in @($current, $system, $administrators)) { $null = $approved.Add($sid.Value) }
# Default: the credential is readable only by this user, SYSTEM and Administrators.
# If you deliberately protected the install directory's ACL, its existing non-broad grants are kept.
if ($rootAcl.AreAccessRulesProtected) {
    foreach ($rule in $rootAcl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -notin $broad) {
            $null = $approved.Add($rule.IdentityReference.Value)
        }
    }
}
if (Test-Path -LiteralPath $tokenFile) {
    if ($Apply) { throw 'Authentication is already initialized; this script never rotates or overwrites credentials.' }
    [pscustomobject]@{Applied=$false;AlreadyInitialized=$true;TokenFile=$tokenFile;ApprovedPrincipals=$approved.Count} | ConvertTo-Json
    exit 0
}
if (-not $Apply) {
    [pscustomobject]@{Applied=$false;AlreadyInitialized=$false;TokenFile=$tokenFile;ApprovedPrincipals=$approved.Count} | ConvertTo-Json
    exit 0
}
# Refuse only while THIS installation's service runs (its .venv interpreter); another program on port 8765,
# or a Context Forge installed elsewhere on another port, is not affected by this installation's token.
$ownVenv = Join-Path $root '.venv\Scripts\'
$running = @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -and $_.ExecutablePath.StartsWith($ownVenv, [StringComparison]::OrdinalIgnoreCase) -and $_.CommandLine -match 'context_forge\.server'
})
if ($running.Count) {
    throw 'Stop the Context Forge service before initializing authentication.'
}

function New-ProtectedAcl([bool]$Container) {
    $acl = if ($Container) { [Security.AccessControl.DirectorySecurity]::new() } else { [Security.AccessControl.FileSecurity]::new() }
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($current)
    foreach ($sidValue in $approved) {
        $sid = [Security.Principal.SecurityIdentifier]::new($sidValue)
        $full = $sidValue -in @($current.Value, $system.Value, $administrators.Value)
        $rights = if ($full) { [Security.AccessControl.FileSystemRights]::FullControl } elseif ($Container) { [Security.AccessControl.FileSystemRights]::ReadAndExecute } else { [Security.AccessControl.FileSystemRights]::Read }
        $inheritance = if ($Container) { [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit' } else { [Security.AccessControl.InheritanceFlags]::None }
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, $rights, $inheritance, [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow)
        $null = $acl.AddAccessRule($rule)
    }
    return $acl
}

$temporary = $null
$bytes = [byte[]]::new(32)
try {
    if (-not (Test-Path -LiteralPath $securityDir)) { $null = New-Item -ItemType Directory -Path $securityDir }
    Set-Acl -LiteralPath $securityDir -AclObject (New-ProtectedAcl $true)
    # RandomNumberGenerator.Fill does not exist in Windows PowerShell 5.1 (.NET Framework).
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
    $temporary = Join-Path $securityDir ('.service-token-' + [guid]::NewGuid().ToString('N') + '.tmp')
    [IO.File]::WriteAllText($temporary, $token, [Text.UTF8Encoding]::new($false))
    Set-Acl -LiteralPath $temporary -AclObject (New-ProtectedAcl $false)
    Move-Item -LiteralPath $temporary -Destination $tokenFile
    $temporary = $null
    $token = $null
} finally {
    [Array]::Clear($bytes, 0, $bytes.Length)
    if ($temporary -and (Test-Path -LiteralPath $temporary)) { Remove-Item -LiteralPath $temporary -Force }
}

$fileAcl = Get-Acl -LiteralPath $tokenFile
$bad = @($fileAcl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | Where-Object {
    $_.AccessControlType -eq 'Allow' -and $_.IdentityReference.Value -in $broad
})
if (-not $fileAcl.AreAccessRulesProtected -or $bad.Count) {
    throw 'Credential ACL verification failed.'
}
$metadata = Get-Item -LiteralPath $tokenFile
if ($metadata.Length -ne 43) { throw 'Credential length verification failed.' }
[pscustomobject]@{Applied=$true;AlreadyInitialized=$false;TokenFile=$tokenFile;ApprovedPrincipals=$approved.Count;Bytes=$metadata.Length} | ConvertTo-Json
