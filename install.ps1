<#
.SYNOPSIS
  Installs Podcast Ad Cleaner on Windows.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1
  powershell -ExecutionPolicy Bypass -File install.ps1 -Cuda -Startup
.PARAMETER Cuda
  Install NVIDIA CUDA libraries so transcription runs on the GPU (much faster).
.PARAMETER Startup
  Start the tray app automatically when you sign in to Windows.
#>
param([switch]$Cuda, [switch]$Startup)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Has($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Find-Python {
    # Returns the full path of a suitable python.exe (3.10-3.13), or $null.
    # Every probe runs a one-line script with -c, so it can never open an interactive >>> prompt,
    # and stdin is closed so nothing can wait for keyboard input.
    $code = "import sys; print(sys.executable if (3,10) <= sys.version_info[:2] <= (3,13) else '')"
    $probes = @()
    if (Has "py") { foreach ($v in "3.12", "3.13", "3.11", "3.10") { $probes += ,@("py", "-$v") } }
    if (Has "python") { $probes += ,@("python") }
    if (Has "python3") { $probes += ,@("python3") }
    foreach ($p in $probes) {
        $exe = $p[0]
        $probeArgs = @($p | Select-Object -Skip 1) + @("-c", $code)
        try {
            $out = "" | & $exe @probeArgs 2>$null | Select-Object -Last 1
        } catch { continue }
        if ($LASTEXITCODE -eq 0 -and $out -and (Test-Path $out.Trim()) -and
            $out -notmatch "WindowsApps") {   # skip the Microsoft Store stub
            return $out.Trim()
        }
    }
    return $null
}

Step "Checking Python"
$pythonExe = Find-Python
if (-not $pythonExe) {
    if (Has "winget") {
        Write-Host "No Python 3.10-3.13 found - installing Python 3.12 with winget..."
        winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
        Refresh-Path
        $pythonExe = Find-Python
    }
    if (-not $pythonExe) {
        throw "Python 3.10-3.13 is required. Install 3.12 from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'), then run this again."
    }
}
$ver = "" | & $pythonExe -c "import sys; print(sys.version.split()[0])"
Write-Host "Using Python $ver at $pythonExe"

Step "Creating virtual environment (.venv)"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & $pythonExe -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment" }
}
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip wheel

Step "Installing Python packages"
& $venvPy -m pip install -r requirements.txt

if ($Cuda) {
    # faster-whisper needs cuBLAS + cuDNN 9 for CUDA 12; the app adds these DLL folders at runtime.
    Step "Installing NVIDIA CUDA libraries for faster-whisper"
    & $venvPy -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
}

Step "Checking ffmpeg"
if (-not (Has "ffmpeg")) {
    if (Has "winget") {
        winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
        Refresh-Path
    } else {
        Write-Warning "Install ffmpeg (https://www.gyan.dev/ffmpeg/builds/) and add it to PATH, or set tools.ffmpeg in config.yaml"
    }
}

Step "Checking Ollama"
if (-not (Has "ollama")) {
    if (Has "winget") {
        winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
        Refresh-Path
    } else {
        Write-Warning "Install Ollama from https://ollama.com/download/windows"
    }
}

Step "Creating config.yaml"
if (-not (Test-Path "config.yaml")) {
    Copy-Item "config.example.yaml" "config.yaml"
    Write-Host "Created config.yaml - edit it to list your podcasts."
} else {
    Write-Host "config.yaml already exists - left unchanged."
}

if (Has "ollama") {
    $model = (Select-String -Path "config.yaml" -Pattern '^\s*model:\s*([^\s#]+:[^\s#]+)' |
              Select-Object -First 1).Matches.Groups[1].Value
    if (-not $model) { $model = "llama3.2:3b" }
    Step "Downloading Ollama model $model"
    ollama pull $model
}

if ($Startup) {
    Step "Adding to Windows startup"
    $lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "Podcast Ad Cleaner.lnk"
    $sh = New-Object -ComObject WScript.Shell
    $s = $sh.CreateShortcut($lnk)
    $s.TargetPath = Join-Path $Root ".venv\Scripts\pythonw.exe"
    $s.Arguments = "-m podcast_cleaner tray --minimized"
    $s.WorkingDirectory = $Root
    $s.Description = "Podcast Ad Cleaner (system tray)"
    $s.Save()
    Write-Host "Shortcut created: $lnk"
}

Step "Allowing the feed server through Windows Firewall (private networks)"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) {
    Remove-NetFirewallRule -DisplayName "Podcast Ad Cleaner feeds" -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName "Podcast Ad Cleaner feeds" -Direction Inbound -Protocol TCP `
        -LocalPort 8585 -Action Allow -Profile Private | Out-Null
    Write-Host "Firewall rule added for TCP 8585 (private networks)."
} else {
    Write-Host "Not running as admin - Windows will ask to allow network access the first time the app starts. Choose 'Private networks'."
}

Step "Checking setup"
& $venvPy -m podcast_cleaner check

Write-Host "`nDone. Edit config.yaml, then double-click 'Start Podcast Cleaner.bat'." -ForegroundColor Green
Write-Host "(Open a NEW terminal first if ffmpeg/Ollama were just installed, so PATH is refreshed.)"
