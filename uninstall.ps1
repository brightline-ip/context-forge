<#
.SYNOPSIS
  Remove what install.ps1 created, with optional removal of your data.
.DESCRIPTION
  Stops the Context Forge listener only if it is this installation's venv Python, removes the
  optional scheduled task if present, and deletes .venv and integrations\mcp\node_modules.
  Your data (data\, output\, security\) is kept unless you pass -RemoveData, which asks to confirm.
  MCP client configuration entries you added by hand are not edited; remove them yourself.
  The install directory itself is not deleted.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [switch]$RemoveData,
    [string]$TaskName = 'Context Forge Service',
    [int]$Port = 8765
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($PSScriptRoot)
$venvPython = Join-Path $root '.venv\Scripts\python.exe'

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    if ($task.Actions.Count -eq 1 -and $task.Actions[0].Arguments -like "*$root*") {
        if ($PSCmdlet.ShouldProcess($TaskName, 'Unregister scheduled task')) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
    } else {
        Write-Warning "Scheduled task '$TaskName' does not point at this installation; left unchanged."
    }
}

foreach ($listener in @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
    # A venv python.exe starts the real interpreter as a child, so the listener's parent may be the venv Python.
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction SilentlyContinue
    $parent = if ($proc) { Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.ParentProcessId)" -ErrorAction SilentlyContinue } else { $null }
    $owner = if ($proc -and $proc.ExecutablePath -ieq $venvPython) { $proc } elseif ($parent -and $parent.ExecutablePath -ieq $venvPython) { $parent } else { $null }
    if ($owner) {
        if ($PSCmdlet.ShouldProcess("process $($owner.ProcessId)", 'Stop Context Forge service')) { $null = & taskkill.exe /PID $owner.ProcessId /T /F 2>&1 }
    } else {
        Write-Warning "Something else is listening on port $Port; it was not touched."
    }
}

foreach ($relative in @('.venv', 'integrations\mcp\node_modules')) {
    $target = [IO.Path]::GetFullPath((Join-Path $root $relative))
    if ((Test-Path -LiteralPath $target) -and $target.StartsWith($root + '\')) {
        if ($PSCmdlet.ShouldProcess($target, 'Remove directory')) { Remove-Item -LiteralPath $target -Recurse -Force }
    }
}

if ($RemoveData) {
    foreach ($relative in @('data', 'output', 'security')) {
        $target = [IO.Path]::GetFullPath((Join-Path $root $relative))
        if ((Test-Path -LiteralPath $target) -and $target.StartsWith($root + '\')) {
            if ($PSCmdlet.ShouldProcess($target, 'Permanently delete your Context Forge data')) { Remove-Item -LiteralPath $target -Recurse -Force }
        }
    }
} else {
    Write-Host 'Kept data\, output\ and security\ (use -RemoveData to delete them).'
}
Write-Host 'Uninstall finished. Remove any MCP client entry named context-forge yourself.'
