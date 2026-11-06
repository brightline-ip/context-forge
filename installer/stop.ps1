# Stops this installation's Context Forge service and nothing else.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
[void](Stop-CfService $root)
exit 0
