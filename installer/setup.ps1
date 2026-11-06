<#
.SYNOPSIS
  Installs Context Forge for the current Windows user. Started by Setup.cmd.

.DESCRIPTION
  Copies Context Forge into a per-user folder, creates its Python environment and service token, adds Start
  Menu shortcuts and starts the local service. Missing programs are explained and offered through winget;
  nothing is installed without your "yes". Re-running Setup repairs or updates an installation and keeps your
  data and token. No administrator rights are needed for Context Forge itself.

.PARAMETER InstallRoot
  Install folder. Default: %LOCALAPPDATA%\Brightline\ContextForge
.PARAMETER Port
  Local port. Default 8765. With another port, MCP clients need CONTEXT_FORGE_URL (Setup writes it for you).
.PARAMETER InputRoots
  Folders the service may read. Default: your Documents folder.
.PARAMETER ShortcutFolder
  Where shortcuts go. Default: Start Menu, folder "Context Forge".
.PARAMETER DryRun
  Check this computer and show what Setup would do, without installing, writing or starting anything.
.PARAMETER SimulateMissing
  With -DryRun only: pretend these programs are missing (Python, Node, Tesseract).
.PARAMETER NoOptional
  Do not offer the optional programs (Node.js, Tesseract). Never approves installing software.
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Brightline\ContextForge'),
    [ValidateRange(1024, 65535)][int]$Port = 8765,
    [string[]]$InputRoots,
    [string]$ShortcutFolder = (Join-Path ([Environment]::GetFolderPath('Programs')) 'Context Forge'),
    [string]$PythonExe,
    [switch]$DryRun,
    [string[]]$SimulateMissing = @(),
    [switch]$NoOptional,
    [switch]$NoStart
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$bundle = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
$paths = Get-CfPaths $InstallRoot
$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$SimulateMissing = @($SimulateMissing | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($SimulateMissing.Count -and -not $DryRun) { throw '-SimulateMissing can only be used together with -DryRun.' }

function Get-WingetArgs([string]$Id) {
    return @('install', '--id', $Id, '--exact', '--source', 'winget', '--accept-source-agreements', '--accept-package-agreements')
}

# Explain a missing program and, after an explicit yes, install it with winget.
# Returns 'installed', 'declined', 'failed' or 'dryrun'.
function Request-Program([string]$Name, [string]$Id, [string[]]$Why, [string]$ManualUrl) {
    Write-Warn2 "$Name was not found."
    foreach ($line in $Why) { Write-Info $line }
    Write-Info 'Setup can install it with Windows Package Manager:'
    Write-Info ('    winget ' + ((Get-WingetArgs $Id) -join ' '))
    Write-Info "Or install it yourself from $ManualUrl and run Setup again."
    if ($DryRun) { Write-Info "[dry run] Setup would ask: Install $Name now?"; return 'dryrun' }
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Bad 'Windows Package Manager (winget) is not available. Install "App Installer" from the Microsoft Store, or use the link above.'
        return 'failed'
    }
    if (-not (Read-YesNo "Install $Name now?" $false)) { return 'declined' }
    Write-Info "Installing $Name. Windows may ask for permission; choose Yes."
    & $winget.Source @(Get-WingetArgs $Id)
    $code = $LASTEXITCODE
    Update-SessionPath
    # -1978335189 (0x8A15002B): already installed, no applicable upgrade.
    if ($code -eq 0 -or $code -eq -1978335189) { return 'installed' }
    Write-Bad "winget reported an error ($code) while installing $Name."
    return 'failed'
}

function New-CfShortcut([string]$Name, [string]$Script, [string]$Description, [string]$Icon, [switch]$Hidden) {
    New-Item -ItemType Directory -Force -Path $ShortcutFolder | Out-Null
    $lnk = Join-Path $ShortcutFolder ($Name + '.lnk')
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    $s.TargetPath = $PowerShellExe
    $window = if ($Hidden) { '-WindowStyle Hidden ' } else { '' }
    $s.Arguments = '-NoProfile -ExecutionPolicy Bypass -STA ' + $window + '-File "' + (Join-Path $InstallRoot "installer\$Script") + '"'
    $s.WorkingDirectory = $InstallRoot
    $s.Description = $Description
    $s.IconLocation = "$env:SystemRoot\System32\$Icon"
    $s.Save()
    return $lnk
}

$exit = 0
try {
    Write-Host ''
    Write-Host 'Context Forge - Setup (public preview 0.1.1)' -ForegroundColor White
    Write-Info 'Turns PDFs, Word, PowerPoint, Excel and other files into clean, traceable text on your own computer.'
    Write-Info "Install folder: $InstallRoot"

    # ---- 1. This computer --------------------------------------------------------------------
    Write-Step 'Step 1 of 5: Checking this computer'
    if (-not [Environment]::Is64BitOperatingSystem -or [Environment]::OSVersion.Version.Major -lt 10) { throw 'Context Forge needs 64-bit Windows 10 or 11.' }
    if (-not (Test-Path -LiteralPath (Join-Path $bundle 'context_forge\server.py'))) {
        throw 'The download is incomplete. Extract the whole zip file (right-click, Extract All) and run Setup.cmd from the extracted folder.'
    }
    if ($InstallRoot.IndexOfAny([char[]]'"%!') -ge 0) { throw 'The install folder path cannot contain ", % or !. Choose another with -InstallRoot.' }
    $drive = [IO.Path]::GetPathRoot($InstallRoot)
    $info = New-Object IO.DriveInfo($drive)
    # The service token and your documents rely on NTFS permissions.
    if ($info.DriveFormat -notin @('NTFS', 'ReFS')) { throw "The install folder is on a $($info.DriveFormat) drive, which cannot restrict access to your documents and token. Choose an NTFS drive with -InstallRoot." }
    if ($InstallRoot.Length -gt 80) { throw 'The install folder path is too long (OCR working folders need the room). Choose a shorter path with -InstallRoot.' }
    if ($info.AvailableFreeSpace -lt 2GB) { Write-Warn2 ("Only {0:N1} GB free on {1}. Allow at least 2 GB plus room for your documents." -f ($info.AvailableFreeSpace / 1GB), $drive) }
    Write-Good ('Windows ' + [Environment]::OSVersion.Version.ToString() + ', 64-bit')

    # ---- 2. Programs --------------------------------------------------------------------------
    Write-Step 'Step 2 of 5: Checking required programs'
    $python = if ('Python' -in $SimulateMissing) { $null } else { Find-CfPython $PythonExe }
    if (-not $python) {
        $r = Request-Program 'Python 3.12 or later (required)' 'Python.Python.3.12' @('Context Forge is written in Python.') 'https://www.python.org/downloads/windows/'
        if ($r -eq 'installed') { $python = Find-CfPython $PythonExe }
        if (-not $python -and $r -ne 'dryrun') { throw 'Python 3.12 or later is required. Install it and run Setup again. If you just installed it, close this window and run Setup again.' }
    }
    if ($python) {
        Write-Good "Python $($python.Version) ($($python.Exe))"
        if ($python.Version.Minor -ne 12) { Write-Info 'Note: the tests run on Python 3.12; newer versions are expected to work.' }
    }

    $node = if ('Node' -in $SimulateMissing) { $null } else { Find-CfNode }
    if (-not $node -and -not $NoOptional) {
        $r = Request-Program 'Node.js 20 or later (recommended)' 'OpenJS.NodeJS.LTS' @('Needed to use Context Forge from AI assistants (MCP) and from the command line.', 'The local web page works without it.') 'https://nodejs.org/'
        if ($r -eq 'installed') { $node = Find-CfNode }
    }
    if ($node) { Write-Good "Node.js $($node.Version)" } else { Write-Info 'Continuing without Node.js: the web page works; MCP and the command line do not. Run Setup again after installing it.' }

    $tesseractFound = (-not ('Tesseract' -in $SimulateMissing)) -and (Test-Path -LiteralPath 'C:\Program Files\Tesseract-OCR\tesseract.exe')
    if (-not $tesseractFound -and -not $NoOptional) {
        $r = Request-Program 'Tesseract OCR (optional)' 'UB-Mannheim.TesseractOCR' @('A second OCR engine. It re-reads scanned pages and flags dates and numbers the two engines read differently.', 'Everything else works without it.') 'https://github.com/UB-Mannheim/tesseract/wiki'
        if ($r -eq 'installed') { $tesseractFound = Test-Path -LiteralPath 'C:\Program Files\Tesseract-OCR\tesseract.exe' }
    }
    if ($tesseractFound) { Write-Good 'Tesseract OCR (second-engine number check on)' } else { Write-Info 'Second-engine number check is off.' }

    $ocr = Test-EnglishOcr
    if ($ocr -eq $true) { Write-Good 'Windows English OCR language pack' }
    elseif ($ocr -eq $false) {
        Write-Warn2 'The Windows English OCR language pack was not found, so scanned PDF pages will fail. Other files work.'
        Write-Info 'Add it in Settings, Time and language, Language and region, English, Language options, Optical character recognition.'
    } else { Write-Info 'Could not check for the Windows OCR language pack.' }

    # ---- 3. Port ------------------------------------------------------------------------------
    Write-Step 'Step 3 of 5: Checking the local port'
    $hadInstall = Test-Path -LiteralPath $paths.Marker -PathType Leaf
    if (-not $hadInstall -and (Test-Path -LiteralPath $InstallRoot) -and (Get-ChildItem -LiteralPath $InstallRoot -Force | Select-Object -First 1)) {
        throw "$InstallRoot already has files that Setup did not put there. Choose an empty folder with -InstallRoot."
    }
    $ownRunning = $hadInstall -and @(Get-CfServiceProcesses $InstallRoot).Count -gt 0
    if ($hadInstall -and -not $DryRun) {
        if (Stop-CfService $InstallRoot) { Write-Info 'Stopped the earlier installation so it can be updated (your data is kept).'; Start-Sleep -Seconds 2 }
    }
    if (-not ($DryRun -and $ownRunning) -and -not (Test-PortFree $Port)) {
        throw "Port $Port is already used by another program. Stop that program, or run Setup again with another port, for example: Setup.cmd -Port 18765"
    }
    Write-Good "Port $Port is free (this computer only)"

    if ($DryRun) {
        Write-Host ''
        Write-Good 'Dry run finished. Nothing was changed.'
        Write-Info "Setup would copy the program to $InstallRoot, run install.ps1, add shortcuts in $ShortcutFolder and start the service."
        exit 0
    }

    # ---- 4. Install ---------------------------------------------------------------------------
    Write-Step 'Step 4 of 5: Installing (downloads Python and Node packages; allow a few minutes)'
    New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $paths.Logs | Out-Null
    if (-not $hadInstall) { Write-Utf8NoBom $paths.Marker "Context Forge install root. The uninstaller removes only folders that contain this file.`r`n" }
    if ($bundle -ine $InstallRoot) {
        $null = & robocopy.exe $bundle $InstallRoot /MIR /XD .git .venv node_modules data output security logs __pycache__ /XF installer.json MCP-setup.json $script:CfMarker /NFL /NDL /NJH /NJS /NP
        if ($LASTEXITCODE -ge 8) { throw "Copying the program files failed (robocopy code $LASTEXITCODE)." }
    }
    # Clear the "downloaded from the Internet" mark on the copies this installation runs.
    Get-ChildItem -LiteralPath $InstallRoot -Recurse -File -Include *.ps1, *.cmd, *.py, *.mjs, *.js | Unblock-File -ErrorAction SilentlyContinue
    Write-Good 'Program files copied'

    $log = Join-Path $paths.Logs 'install.log'
    $installArgs = @('-Python', $python.Exe)
    if (-not $node) { $installArgs += '-SkipNode' }
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    & $PowerShellExe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $InstallRoot 'install.ps1') @installArgs *> $log
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    if ($code -ne 0) { throw "Installation failed. Details: $log" }
    if (-not (Test-Path -LiteralPath $paths.Token -PathType Leaf)) { throw "The service token was not created. Details: $log" }
    Write-Good 'Python environment and service token ready (the token is readable only by your account, SYSTEM and Administrators)'

    # ---- 5. Shortcuts and first start ---------------------------------------------------------
    Write-Step 'Step 5 of 5: Shortcuts and first start'
    $shortcuts = @(
        (New-CfShortcut 'Open Context Forge' 'open.ps1' 'Start Context Forge and open its page' 'shell32.dll,13'),
        (New-CfShortcut 'Stop Context Forge' 'stop.ps1' 'Stop the Context Forge service' 'shell32.dll,131' -Hidden),
        (New-CfShortcut 'Uninstall Context Forge' 'remove.ps1' 'Remove Context Forge' 'shell32.dll,31')
    )
    Write-Good "Shortcuts created in $ShortcutFolder"

    $config = [ordered]@{ version = '0.1.1'; port = $Port; shortcuts = @($shortcuts); shortcut_folder = $ShortcutFolder }
    Write-Utf8NoBom $paths.Config ($config | ConvertTo-Json -Depth 4)

    $entry = [ordered]@{ command = $(if ($node) { $node.Exe } else { 'node' }); args = @((Join-Path $InstallRoot 'integrations\mcp\server.mjs')) }
    if ($Port -ne 8765) { $entry['env'] = [ordered]@{ CONTEXT_FORGE_URL = "http://127.0.0.1:$Port" } }
    $mcp = [ordered]@{ mcpServers = [ordered]@{ 'context-forge' = $entry } } | ConvertTo-Json -Depth 5
    Write-Utf8NoBom (Join-Path $InstallRoot 'MCP-setup.json') $mcp

    # Saved in settings.json so a service started on demand by an AI assistant reads the same folders.
    if ($InputRoots) {
        $settingsPath = Join-Path $InstallRoot 'data\settings.json'
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $settingsPath) | Out-Null
        $settings = if (Test-Path -LiteralPath $settingsPath) { Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json } else { New-Object PSObject }
        $settings | Add-Member -NotePropertyName allowed_input_roots -NotePropertyValue @($InputRoots) -Force
        Write-Utf8NoBom $settingsPath ($settings | ConvertTo-Json -Depth 4)
    }

    if (-not $NoStart) {
        if (-not (Start-CfService $InstallRoot $Port)) { throw "Context Forge did not start. Details: $(Join-Path $InstallRoot 'data\service.log')" }
        Write-Good "Context Forge is running at http://127.0.0.1:$Port"
    }

    Write-Host ''
    Write-Good 'Setup finished.'
    Write-Info 'Use it from the Start Menu: "Open Context Forge". The token it asks for is copied to your clipboard for you.'
    if ($node) { Write-Info "To connect an AI assistant, merge $(Join-Path $InstallRoot 'MCP-setup.json') into its MCP configuration." }
    Write-Info 'It reads files only from your Documents folder by default. Results are kept in the install folder.'
    Write-Info 'After restarting Windows, "Open Context Forge" starts it again. AI assistants start it on demand.'
    Write-Info 'Read DISCLAIMER.md: OCR and extraction can be wrong, and this is not legal advice.'
} catch {
    Write-Host ''
    Write-Bad $_.Exception.Message
    Write-Info 'Nothing outside the install folder and shortcuts was changed. You can fix the problem and run Setup again.'
    $exit = 1
}
exit $exit
