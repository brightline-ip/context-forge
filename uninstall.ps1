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
    [string]$TaskName = 'Context Forge Service'
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

foreach ($listener in @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)) {
    $proc = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    if ($proc -and $proc.Path -and ([IO.Path]::GetFullPath($proc.Path) -ieq $venvPython)) {
        if ($PSCmdlet.ShouldProcess("process $($proc.Id)", 'Stop Context Forge service')) { Stop-Process -Id $proc.Id }
    } else {
        Write-Warning 'Something else is listening on port 8765; it was not touched.'
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
