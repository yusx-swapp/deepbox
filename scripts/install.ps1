<#
.SYNOPSIS
  Install the deepbox local command (Windows / PowerShell).

.DESCRIPTION
  Downloads the deepbox connector, creates an isolated virtual environment,
  installs its dependencies, and adds a stable `deepbox` command to the user
  PATH. Installation and upgrades refresh %USERPROFILE%\.deepbox\app; daily
  `deepbox connect` calls do not download or replace installed files.

  Run once:

      irm https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.ps1 | iex

  Then set DEEPBOX_SERVER_URL and DEEPBOX_TOKEN and run:

      deepbox connect

  Upgrade explicitly with `deepbox upgrade`. The installer never stores your
  token on disk; the connector reads it from the process environment.

.NOTES
  Requires Python 3.10+ (https://www.python.org/downloads/ or `winget install
  Python.Python.3.12`). The connector runs your local Claude Code / Copilot CLI
  / Codex agents; those tools are NOT installed by this script.
#>

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "[deepbox] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[deepbox] $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "[deepbox] $msg" -ForegroundColor Yellow }

# Match only connector processes launched by this installation's virtualenv.
# Command lines are inspected but never printed because they may contain tokens.
function Test-DeepboxConnectorProcess {
    param(
        [Parameter(Mandatory=$true)] $Process,
        [Parameter(Mandatory=$true)] [string] $VenvPython
    )

    $commandLine = [string]$Process.CommandLine
    if (-not $commandLine -or
        $commandLine -notmatch '(?i)(?:^|\s)-m\s+connector(?:\.cli)?(?:\s|$)') {
        return $false
    }

    try { $target = [IO.Path]::GetFullPath($VenvPython) }
    catch { return $false }

    $exeMatches = $false
    if ($Process.ExecutablePath) {
        try {
            $actual = [IO.Path]::GetFullPath([string]$Process.ExecutablePath)
            $exeMatches = [string]::Equals(
                $actual, $target, [StringComparison]::OrdinalIgnoreCase)
        } catch {}
    }

    $targetPattern = [regex]::Escape($target)
    $commandUsesTarget = $commandLine -match (
        '(?i)^\s*"?' + $targetPattern + '"?(?:\s|$)')
    return ($exeMatches -or $commandUsesTarget)
}

function Get-RunningDeepboxConnectors {
    param([Parameter(Mandatory=$true)] [string] $VenvPython)

    try {
        $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    } catch {
        return @()
    }
    return @($processes | Where-Object {
        Test-DeepboxConnectorProcess -Process $_ -VenvPython $VenvPython
    })
}

function Get-DeepboxProcessTreeIds {
    param(
        [Parameter(Mandatory=$true)] [object[]] $Processes,
        [Parameter(Mandatory=$true)] [int[]] $RootProcessIds
    )

    $ids = @{}
    foreach ($rootId in $RootProcessIds) { $ids[[int]$rootId] = $true }
    do {
        $added = $false
        foreach ($item in $Processes) {
            $parentId = [int]$item.ParentProcessId
            $childId = [int]$item.ProcessId
            if ($ids.ContainsKey($parentId) -and -not $ids.ContainsKey($childId)) {
                $ids[$childId] = $true
                $added = $true
            }
        }
    } while ($added)
    return @($ids.Keys | ForEach-Object { [int]$_ })
}

function Stop-RunningDeepboxConnectors {
    param([Parameter(Mandatory=$true)] [string] $VenvPython)

    if (-not (Test-Path -LiteralPath $VenvPython)) { return }
    $running = @(Get-RunningDeepboxConnectors -VenvPython $VenvPython)
    if ($running.Count -eq 0) { return }

    try { $snapshot = @(Get-CimInstance Win32_Process -ErrorAction Stop) }
    catch { $snapshot = $running }
    $rootIds = @($running | ForEach-Object { [int]$_.ProcessId })
    $processIds = @(Get-DeepboxProcessTreeIds `
        -Processes $snapshot -RootProcessIds $rootIds)

    Write-Step "Stopping the existing connector and its child processes for upgrade ..."
    foreach ($processId in $processIds) {
        # A venv launcher and its base-Python child can both match. Stopping
        # either may make the other disappear, so decide success only after
        # checking that the complete snapshotted process tree has exited.
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(8)
    do {
        $alive = @($processIds | Where-Object {
            Get-Process -Id $_ -ErrorAction SilentlyContinue
        })
        if ($alive.Count -eq 0) { break }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)

    if ($alive.Count -ne 0) {
        throw "The existing deepbox connector did not stop. Stop it with Ctrl+C, then re-run the installer."
    }
    # Let its parent launcher unwind and release app as its working directory.
    Start-Sleep -Milliseconds 500
    Write-Ok "Existing connector stopped."
}

function Remove-DirectoryWithRetry {
    param(
        [Parameter(Mandatory=$true)] [string] $Path,
        [int] $Attempts = 12,
        [int] $DelayMilliseconds = 500
    )

    if (-not (Test-Path -LiteralPath $Path)) { return }
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            return
        } catch {
            if ($attempt -eq $Attempts) {
                throw "Could not refresh '$Path' because another process is using it. Stop any connector or shell whose working directory is there, then re-run the installer."
            }
            Start-Sleep -Milliseconds $DelayMilliseconds
        }
    }
}

function Add-UserPathEntry {
    param([Parameter(Mandatory=$true)] [string] $Path)

    $target = [Environment]::ExpandEnvironmentVariables($Path).TrimEnd('\')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $userEntries = @($userPath -split ';' | Where-Object { $_ })
    $hasUserEntry = @($userEntries | Where-Object {
        [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\') -ieq $target
    }).Count -gt 0
    if (-not $hasUserEntry) {
        $newUserPath = if ([string]::IsNullOrWhiteSpace($userPath)) {
            $Path
        } else {
            $userPath.TrimEnd(';') + ';' + $Path
        }
        try {
            [Environment]::SetEnvironmentVariable('Path', $newUserPath, 'User')
        } catch {
            Write-Warn2 "Could not persist $Path on the user PATH. Add it manually."
        }
    }

    $processEntries = @($env:Path -split ';' | Where-Object { $_ })
    $hasProcessEntry = @($processEntries | Where-Object {
        [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\') -ieq $target
    }).Count -gt 0
    if (-not $hasProcessEntry) { $env:Path = $Path + ';' + $env:Path }
}

# --- Config ----------------------------------------------------------------
# Public source of the connector code (anonymous download; no repo access
# needed). Override with $env:DEEPBOX_SOURCE_ZIP to pin a fork/branch.
$SourceZip = if ($env:DEEPBOX_SOURCE_ZIP) { $env:DEEPBOX_SOURCE_ZIP }
            else { 'https://github.com/yusx-swapp/deepbox/archive/refs/heads/main.zip' }
$Home2   = if ($env:USERPROFILE) { $env:USERPROFILE } else { [Environment]::GetFolderPath('UserProfile') }
$Root    = if ($env:DEEPBOX_HOME) { $env:DEEPBOX_HOME } else { Join-Path $Home2 '.deepbox' }
$Src     = Join-Path $Root 'app'          # extracted connector source
$Venv    = Join-Path $Root 'venv'
$VenvPy  = Join-Path $Venv 'Scripts\python.exe'
$Bin     = Join-Path $Root 'bin'
$Command = Join-Path $Bin 'deepbox.cmd'
$Launcher = Join-Path $Root 'deepbox-connect.cmd'  # legacy compatibility
$InstallScriptUrl = 'https://raw.githubusercontent.com/yusx-microsoft/deepbox/main/scripts/install.ps1'

Write-Step "Installing into $Root"
New-Item -ItemType Directory -Force -Path $Root | Out-Null

# --- 1. Locate Python 3.10+ -------------------------------------------------
# Returns @(exe, @(prefixArgs...)) for the first interpreter that is >= 3.10,
# or $null. Handles the Windows `py` launcher (needs a `-3` prefix arg) as
# well as plain `python` / `python3`.
function Find-Python {
    $verCheck = 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)'
    $candidates = @(
        @('py',      @('-3')),
        @('python',  @()),
        @('python3', @())
    )
    foreach ($c in $candidates) {
        $exe  = $c[0]
        $pre  = $c[1]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            & $exe @pre '-c' $verCheck 2>$null
            if ($LASTEXITCODE -eq 0) { return ,@($exe, $pre) }
        } catch {}
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Warn2 "Python 3.10+ was not found on PATH."
    Write-Host  "  Install it, then re-run this installer:"
    Write-Host  "    winget install Python.Python.3.12"
    Write-Host  "    (or download from https://www.python.org/downloads/)"
    throw "Python 3.10+ required."
}
$PyExe  = $py[0]
$PyArgs = $py[1]
Write-Ok "Using Python: $PyExe $($PyArgs -join ' ')"

# --- 2. Download + extract connector source --------------------------------
Write-Step "Downloading connector source ..."
$tmpZip = Join-Path $env:TEMP ("deepbox-" + [guid]::NewGuid().ToString('N') + '.zip')
Invoke-WebRequest -Uri $SourceZip -OutFile $tmpZip -UseBasicParsing

$tmpExtract = Join-Path $env:TEMP ("deepbox-x-" + [guid]::NewGuid().ToString('N'))
if (Test-Path $tmpExtract) { Remove-Item -Recurse -Force $tmpExtract }
Expand-Archive -Path $tmpZip -DestinationPath $tmpExtract -Force
Remove-Item -Force $tmpZip

# The GitHub zip nests everything under a single <repo>-<branch> folder.
$inner = Get-ChildItem -Path $tmpExtract -Directory | Select-Object -First 1
if (-not $inner) { throw "Unexpected archive layout (no inner folder)." }

# Refresh the app folder with only what the connector needs. Legacy launchers
# used app as their cwd, so stop only processes launched by this installation's
# virtualenv and retry while those wrappers unwind.
Stop-RunningDeepboxConnectors -VenvPython $VenvPy
Remove-DirectoryWithRetry -Path $Src
New-Item -ItemType Directory -Force -Path $Src | Out-Null
Copy-Item -Recurse -Force (Join-Path $inner.FullName 'connector') (Join-Path $Src 'connector')
foreach ($f in @('requirements-connector.txt', 'requirements.txt')) {
    $p = Join-Path $inner.FullName $f
    if (Test-Path $p) { Copy-Item -Force $p (Join-Path $Src $f) }
}
Remove-Item -Recurse -Force $tmpExtract
Write-Ok "Connector source ready."

# --- 3. Virtual environment + dependencies ---------------------------------
if (-not (Test-Path $VenvPy)) {
    Write-Step "Creating virtual environment ..."
    & $PyExe @PyArgs -m venv $Venv
}
Write-Step "Installing connector dependencies ..."
& $VenvPy -m pip install --quiet --upgrade pip | Out-Null
$req = Join-Path $Src 'requirements-connector.txt'
if (Test-Path $req) {
    & $VenvPy -m pip install --quiet -r $req
} else {
    & $VenvPy -m pip install --quiet 'httpx>=0.27' 'websockets>=12.0' 'PyYAML>=6.0' 'pywinpty>=2.0'
}
$sitePackages = (& $VenvPy -c "import site; print(site.getsitepackages()[0])").Trim()
if (-not $sitePackages) { throw "Could not locate the connector virtualenv site-packages directory." }
$pthLine = "import sys; from pathlib import Path; sys.path.insert(0, str(Path(sys.prefix).parent / 'app'))`n"
[System.IO.File]::WriteAllText((Join-Path $sitePackages 'deepbox-app.pth'), $pthLine, [System.Text.Encoding]::ASCII)
Write-Ok "Dependencies installed."

# --- 4. Install stable command + legacy launcher ----------------------------
# The PATH shim is intentionally stable: `deepbox upgrade` can refresh app and
# venv without rewriting the batch file that is currently invoking the upgrade.
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
$commandBody = @"
@echo off
rem deepbox-stable-shim-v1
setlocal
set "DEEPBOX_ROOT=%DEEPBOX_HOME%"
if not defined DEEPBOX_ROOT for %%I in ("%~dp0..") do set "DEEPBOX_ROOT=%%~fI"
set "DEEPBOX_HOME=%DEEPBOX_ROOT%"
if /I "%~1"=="upgrade" goto upgrade
set "DEEPBOX_PYTHON=%DEEPBOX_ROOT%\venv\Scripts\python.exe"
if not exist "%DEEPBOX_PYTHON%" (echo [deepbox] installation is incomplete; run deepbox upgrade & exit /b 1)
"%DEEPBOX_PYTHON%" -I -u -m connector.cli %*
exit /b %ERRORLEVEL%
:upgrade
set "DEEPBOX_INSTALL_ONLY=1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "irm '$InstallScriptUrl' | iex"
exit /b %ERRORLEVEL%
"@
if (-not (Test-Path -LiteralPath $Command)) {
    Set-Content -Path $Command -Value $commandBody -Encoding ASCII
} elseif (-not ([IO.File]::ReadAllText($Command).Contains('deepbox-stable-shim-v1'))) {
    throw "Refusing to replace an unrecognized command at '$Command'. Move it, then re-run the installer."
}
Add-UserPathEntry -Path $Bin
Write-Ok "Command installed: $Command"

$launcherBody = @"
@echo off
rem Legacy compatibility; prefer: deepbox connect
setlocal
for %%I in ("%~dp0.") do set "DEEPBOX_ROOT=%%~fI"
set "DEEPBOX_HOME=%DEEPBOX_ROOT%"
call "%DEEPBOX_ROOT%\bin\deepbox.cmd" connect %*
exit /b %ERRORLEVEL%
"@
Set-Content -Path $Launcher -Value $launcherBody -Encoding ASCII

# --- 5. Finish, or honor commands generated by the previous web UI ---------
$server = $env:DEEPBOX_SERVER_URL
$token  = $env:DEEPBOX_TOKEN
$installOnly = $env:DEEPBOX_INSTALL_ONLY -eq '1'

if (-not $installOnly -and $server -and $token) {
    Write-Ok "Setup complete. Connecting ..."
    Write-Host ""
    Write-Host "  Reconnect without reinstalling:" -ForegroundColor DarkGray
    Write-Host "      deepbox connect" -ForegroundColor DarkGray
    Write-Host ""
    & $Command doctor
    & $Command connect
} else {
    if (($server -and -not $token) -or ($token -and -not $server)) {
        Write-Warn2 "Both DEEPBOX_SERVER_URL and DEEPBOX_TOKEN are required to connect."
    }
    Write-Ok "Setup complete."
    Write-Host "  Open a new terminal if needed, then run:"
    Write-Host "      deepbox connect"
    Write-Host "  Upgrade later with:"
    Write-Host "      deepbox upgrade"
}
