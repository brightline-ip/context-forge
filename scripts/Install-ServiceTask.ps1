[CmdletBinding(SupportsShouldProcess)]
param([string]$TaskName = 'Context Forge Service')
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$runner = Join-Path $PSScriptRoot 'Ensure-Service.ps1'
$pwsh = (Get-Command pwsh.exe -CommandType Application -ErrorAction Stop).Source
$account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$accountSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
function Resolve-TaskPrincipalSid([string]$UserId) {
    if ($UserId -match '^S-1-') { return $UserId }
    try { return ([Security.Principal.NTAccount]$UserId).Translate([Security.Principal.SecurityIdentifier]).Value } catch {}
    $parts = $account -split '\\', 2
    if ($UserId -notmatch '[\\@]' -and $parts.Count -eq 2 -and $parts[0] -ieq $env:COMPUTERNAME -and $parts[1] -ieq $UserId) {
        $local = Get-LocalUser -SID ([Security.Principal.SecurityIdentifier]$accountSid) -ErrorAction Stop
        $qualifiedSid = (New-Object Security.Principal.NTAccount($env:COMPUTERNAME, $local.Name)).Translate([Security.Principal.SecurityIdentifier]).Value
        if ($local.Name -ieq $UserId -and $local.SID.Value -eq $accountSid -and $qualifiedSid -eq $accountSid) { return $local.SID.Value }
    }
    return $null
}
$arguments = "-NoProfile -NonInteractive -File `"$runner`""
$action = New-ScheduledTaskAction -Execute $pwsh -Argument $arguments -WorkingDirectory $root
$logon = New-ScheduledTaskTrigger -AtLogOn -User $account
$reconcile = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd -StartWhenAvailable
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    $existingSid = Resolve-TaskPrincipalSid $existing.Principal.UserId
    $owned = $existingSid -eq $accountSid -and $existing.Actions.Count -eq 1 -and
        ([IO.Path]::GetFullPath($existing.Actions[0].Execute) -eq [IO.Path]::GetFullPath($pwsh)) -and
        $existing.Actions[0].Arguments -eq $arguments -and
        $existing.Actions[0].WorkingDirectory -eq $root -and
        $existing.Principal.LogonType -eq 'Interactive' -and $existing.Principal.RunLevel -eq 'Limited' -and
        $existing.Description -eq 'Authenticated loopback Context Forge service reconciliation'
    if (-not $owned) { throw 'Refusing to overwrite a Context Forge task not owned by this project and user.' }
}
if ($PSCmdlet.ShouldProcess($TaskName, 'Register current-user Context Forge reconciliation task')) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($logon, $reconcile) -Principal $principal -Settings $settings -Description 'Authenticated loopback Context Forge service reconciliation' -Force | Out-Null
    $installed = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $installedSid = Resolve-TaskPrincipalSid $installed.Principal.UserId
    if ($installedSid -ne $accountSid -or $installed.Actions.Count -ne 1 -or $installed.Actions[0].Arguments -ne $arguments -or @($installed.Triggers).Count -ne 2 -or $installed.Settings.IdleSettings.StopOnIdleEnd) {
        throw 'Installed Context Forge task verification failed.'
    }
}
