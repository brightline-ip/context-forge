<#
.SYNOPSIS
  Removes this Context Forge installation: what Setup created, nothing else.
.DESCRIPTION
  Stops this installation's service, removes its shortcuts and optional scheduled task, and deletes its
  program files. Your data (data\, output\, security\) is kept unless you choose to delete it.
  Python, Node.js and Tesseract stay installed. MCP client entries you added are not edited.
.PARAMETER RemoveData
  Also permanently delete your data.
.PARAMETER KeepData
  Keep your data without asking.
.PARAMETER Yes
  Do not ask for confirmation (requires -RemoveData or -KeepData).
#>
[CmdletBinding()]
param([switch]$RemoveData, [switch]$KeepData, [switch]$Yes, [string]$TaskName = 'Context Forge Service')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$paths = Get-CfPaths $root

function Remove-Links([string]$Path) {
    # Walked by hand: Windows PowerShell's -Recurse can follow a junction into its target.
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer) { return }
    foreach ($child in @(Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue)) {
        if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            if ($child.PSIsContainer) { [IO.Directory]::Delete($child.FullName, $false) } else { [IO.File]::Delete($child.FullName) }
        } elseif ($child.PSIsContainer) { Remove-Links $child.FullName }
    }
}

function Remove-Tree([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    # Unlink junctions and symbolic links first, without following them, so a linked folder's target survives.
    Remove-Links $Path
    if ((Get-Item -LiteralPath $Path -Force).PSIsContainer) {
        try { Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop } catch {}
        if (Test-Path -LiteralPath $Path) {
            # Long paths (deep node_modules folders): empty the folder with robocopy, then remove it.
            $empty = Join-Path ([IO.Path]::GetTempPath()) ('cf-empty-' + [Guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Path $empty | Out-Null
            $null = & robocopy.exe $empty $Path /MIR /XJ /NFL /NDL /NJH /NJS /NP
            Remove-Item -LiteralPath $empty -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue
        }
    } else { Remove-Item -LiteralPath $Path -Force }
    if (Test-Path -LiteralPath $Path) { throw "Could not remove $Path. Close any program using files there and try again." }
}

$exit = 0
try {
    if ($RemoveData -and $KeepData) { throw 'Use either -RemoveData or -KeepData, not both.' }
    if ($Yes -and -not ($RemoveData -or $KeepData)) { throw '-Yes needs -RemoveData or -KeepData, so your data is never deleted without an explicit choice.' }
    if (-not (Test-Path -LiteralPath $paths.Marker -PathType Leaf)) { throw "No Context Forge installation was found in $root (the marker file is missing). Nothing was changed." }
    if ($root.TrimEnd('\') -eq ([IO.Path]::GetPathRoot($root)).TrimEnd('\') -or $root -ieq $env:USERPROFILE -or $root -ieq $env:LOCALAPPDATA) { throw "Refusing to remove $root." }
    $config = $null
    try { $config = Read-CfConfig $root } catch { Write-Warn2 'The installation settings are missing; removing files only.' }

    Write-Host 'Uninstall Context Forge' -ForegroundColor White
    Write-Info "Install folder: $root"
    Write-Info 'Python, Node.js and Tesseract stay installed.'
    if (-not ($RemoveData -or $KeepData)) {
        Write-Host ''
        Write-Info 'Your data is your archived originals, extracted text, job history and access token.'
        if (Read-YesNo 'Permanently DELETE your data as well?' $false) { $RemoveData = $true } else { $KeepData = $true }
    }
    if (-not $Yes -and -not (Read-YesNo 'Remove Context Forge now?' $false)) { Write-Info 'Nothing was changed.'; exit 0 }

    Set-Location -LiteralPath ([IO.Path]::GetTempPath())
    if (Stop-CfService $root) { Write-Good 'Service stopped'; Start-Sleep -Seconds 2 }

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        if ($task.Actions.Count -eq 1 -and $task.Actions[0].WorkingDirectory -ieq $root) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false; Write-Good 'Scheduled task removed' }
        else { Write-Warn2 "Scheduled task '$TaskName' does not point at this installation; left unchanged." }
    }

    if ($config) {
        foreach ($lnk in @($config.shortcuts)) {
            if ($lnk -and [string]$lnk -like '*.lnk' -and (Test-Path -LiteralPath $lnk)) { Remove-Item -LiteralPath $lnk -Force }
        }
        $folder = [string]$config.shortcut_folder
        if ($folder -and (Test-Path -LiteralPath $folder) -and -not (Get-ChildItem -LiteralPath $folder -Force | Select-Object -First 1)) { Remove-Item -LiteralPath $folder -Force }
        Write-Good 'Shortcuts removed'
    }

    if ($RemoveData) {
        Remove-Tree $root
        Write-Good 'Program files and data removed'
    } else {
        $keep = @('data', 'output', 'security', $script:CfMarker)
        foreach ($child in @(Get-ChildItem -LiteralPath $root -Force)) {
            if ($child.Name -notin $keep) { Remove-Tree $child.FullName }
        }
        Write-Good "Program files removed. Your data was kept in $root"
    }
    Write-Info 'Remove any MCP client entry named context-forge yourself.'
} catch {
    Write-Bad $_.Exception.Message
    $exit = 1
}
if (-not $Yes) { Read-Host 'Press Enter to close' | Out-Null }
exit $exit
