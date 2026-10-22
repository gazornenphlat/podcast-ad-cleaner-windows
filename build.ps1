<#
.SYNOPSIS
  Builds PodcastAdCleaner.exe with PyInstaller (no Python needed on the target PC).
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File build.ps1                  # single .exe
  powershell -ExecutionPolicy Bypass -File build.ps1 -BundleFfmpeg    # also copy ffmpeg/ffprobe alongside
  powershell -ExecutionPolicy Bypass -File build.ps1 -Folder          # folder build: starts faster
.NOTES
  Run install.ps1 first (it creates .venv). Output: dist\PodcastAdCleaner\ and a .zip of it.
  The .exe bundles faster-whisper for CPU. For GPU, put the NVIDIA cuBLAS/cuDNN DLLs
  next to the exe or on PATH (or use the Python install with install.ps1 -Cuda).
#>
param([switch]$Folder, [switch]$BundleFfmpeg)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root
$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "Run install.ps1 first to create .venv" }

Write-Host "==> Installing PyInstaller" -ForegroundColor Cyan
& $py -m pip install --upgrade pyinstaller

Write-Host "==> Creating icon" -ForegroundColor Cyan
New-Item -ItemType Directory -Force build | Out-Null
& $py -c "from podcast_cleaner.app import make_icon; make_icon('idle', 256).save(r'build\icon.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(256,256)])"

$mode = if ($Folder) { "--onedir" } else { "--onefile" }
$out = Join-Path $Root "dist\PodcastAdCleaner"
if (Test-Path $out) { Remove-Item -Recurse -Force $out }

Write-Host "==> Building ($mode)" -ForegroundColor Cyan
& $py -m PyInstaller --noconfirm --clean $mode --windowed `
    --name PodcastAdCleaner `
    --icon build\icon.ico `
    --distpath $(if ($Folder) { "dist" } else { $out }) `
    --add-data "config.example.yaml;." `
    --add-data "podcast_cleaner/fonts;podcast_cleaner/fonts" `
    --add-data "podcast_cleaner/web;podcast_cleaner/web" `
    --collect-all faster_whisper `
    --collect-all ctranslate2 `
    --collect-all onnxruntime `
    --collect-data tokenizers `
    --collect-submodules ruamel.yaml `
    --hidden-import pystray._win32 `
    --collect-all webview `
    --hidden-import clr `
    --exclude-module torch `
    --exclude-module whisper `
    --exclude-module matplotlib `
    --exclude-module pandas `
    --exclude-module scipy `
    --exclude-module IPython `
    --exclude-module sympy `
    --exclude-module onnxruntime.tools `
    --exclude-module onnxruntime.transformers `
    --exclude-module onnxruntime.quantization `
    launcher.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Copy-Item config.example.yaml, README.md, LICENSE $out
if ($BundleFfmpeg) {
    foreach ($tool in "ffmpeg", "ffprobe") {
        $src = (Get-Command $tool -ErrorAction SilentlyContinue).Source
        if ($src) { Copy-Item $src $out; Write-Host "Bundled $src" }
        else { Write-Warning "$tool not found on PATH - not bundled" }
    }
}

$zip = Join-Path $Root "dist\PodcastAdCleaner.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "$out\*" -DestinationPath $zip
Write-Host "`nBuilt: $out" -ForegroundColor Green
Write-Host "Zip:   $zip"
Write-Host "Copy the folder anywhere and run PodcastAdCleaner.exe. config.yaml is created next to it on first run."
