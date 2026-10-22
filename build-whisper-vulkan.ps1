<#
.SYNOPSIS
  Builds whisper.cpp with Vulkan GPU support, so transcription can run on AMD / Intel / NVIDIA graphics
  (including integrated graphics such as the Radeon 680M). Puts whisper-cli.exe (used by this PC) and
  whisper-server.exe (for transcribing for another PC) in .\whisper.cpp\ where Podcast Ad Cleaner finds them.
  Then pick  Engine: whisper.cpp  in the Transcription tab.

  The build itself happens in a folder on this PC's own drive (%LOCALAPPDATA%), not next to the app,
  because building on a network drive is slow and antivirus / file locks there often break it.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File build-whisper-vulkan.ps1
  powershell -ExecutionPolicy Bypass -File build-whisper-vulkan.ps1 -Tag v1.9.5

  Installs (with winget, if missing): Git, CMake, Visual Studio 2022 Build Tools (C++), Vulkan SDK.
  The first run downloads a few GB and takes 15-30 minutes. Run it from an ADMINISTRATOR PowerShell
  so the installers can run; afterwards it needs nothing else.
#>
param(
    [string]$Tag = "v1.9.5",
    [switch]$SkipInstall
)
$ErrorActionPreference = "Stop"
$App = Split-Path -Parent $MyInvocation.MyCommand.Path
$Src = Join-Path $env:LOCALAPPDATA "PodcastAdCleaner\whisper.cpp-src"
$Out = Join-Path $App "whisper.cpp"

function Remove-Folder($path) {
    # A file can stay locked for a while (antivirus scanning a freshly built .exe, or a build process still
    # running). Stop leftover build processes, retry, and as a last resort move the folder aside.
    if (-not (Test-Path $path)) { return }
    Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "cmTC_*" -or
        ($_.Path -and $_.Path.StartsWith($path, [StringComparison]::OrdinalIgnoreCase)) } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    for ($i = 1; $i -le 5; $i++) {
        try { Remove-Item -Recurse -Force $path -ErrorAction Stop; return }
        catch { Start-Sleep -Seconds 2 }
    }
    $aside = "$path.old-" + (Get-Date -Format "yyyyMMdd-HHmmss")
    try {
        Rename-Item $path $aside -ErrorAction Stop
        Write-Host "Couldn't delete the old build folder (a file in it is locked) - moved it to $aside; delete it later." -ForegroundColor Yellow
    } catch {
        throw "Couldn't delete or move $path - a file in it is locked. Restart the PC (or close the program using it) and run this again."
    }
}

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Refresh-Path {
    $env:PATH = [Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("PATH", "User")
    $sdk = [Environment]::GetEnvironmentVariable("VULKAN_SDK", "Machine")
    if ($sdk) { $env:VULKAN_SDK = $sdk; $env:PATH = "$sdk\Bin;" + $env:PATH }
}
function Winget-Install($id, $extra = @()) {
    Write-Host "Installing $id ..."
    & winget install -e --id $id --accept-package-agreements --accept-source-agreements @extra
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {   # -1978335189 = already installed
        throw "winget couldn't install $id (exit $LASTEXITCODE). Install it by hand and run this again with -SkipInstall."
    }
}

# ---------- 1. tools ----------
if (-not $SkipInstall) {
    Step "Checking build tools"
    if (-not (Have winget)) { throw "winget isn't available. Install 'App Installer' from the Microsoft Store, or install Git, CMake, VS Build Tools and the Vulkan SDK by hand and use -SkipInstall." }
    Refresh-Path
    if (-not (Have git))   { Winget-Install "Git.Git" }
    if (-not (Have cmake)) { Winget-Install "Kitware.CMake" }
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    $hasCpp = (Test-Path $vswhere) -and (& $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath)
    if (-not $hasCpp) {
        Winget-Install "Microsoft.VisualStudio.2022.BuildTools" @("--override",
            "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --passive --wait")
    }
    Refresh-Path
    if (-not $env:VULKAN_SDK) { Winget-Install "KhronosGroup.VulkanSDK" }
    Refresh-Path
}
Refresh-Path
if (-not $env:VULKAN_SDK) { throw "The Vulkan SDK isn't installed (VULKAN_SDK isn't set). Install it from https://vulkan.lunarg.com/ and run this again - or open a NEW PowerShell window if you just installed it." }
foreach ($t in "git", "cmake") { if (-not (Have $t)) { throw "$t isn't on PATH - open a new PowerShell window and run this again." } }
Write-Host "Vulkan SDK: $env:VULKAN_SDK"

# ---------- 2. source ----------
Step "Getting whisper.cpp $Tag"
New-Item -ItemType Directory -Force -Path (Split-Path $Src) | Out-Null
Remove-Folder $Src
# an old build folder next to the app (earlier versions of this script built there)
try { Remove-Folder (Join-Path $App "whisper.cpp-src") }
catch { Write-Host "(The old folder $(Join-Path $App 'whisper.cpp-src') is locked - it isn't used any more; delete it after a restart.)" -ForegroundColor Yellow }
& git clone --depth 1 --branch $Tag https://github.com/ggml-org/whisper.cpp $Src
if ($LASTEXITCODE -ne 0) { throw "git clone failed" }

# ---------- 3. build ----------
Step "Building with Vulkan (several minutes)"
& cmake -S $Src -B "$Src\build" -DGGML_VULKAN=ON -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_EXAMPLES=ON -DCMAKE_BUILD_TYPE=Release
if ($LASTEXITCODE -ne 0) { throw "cmake configure failed - see the messages above" }
& cmake --build "$Src\build" --config Release -j $env:NUMBER_OF_PROCESSORS --target whisper-cli whisper-server
if ($LASTEXITCODE -ne 0) { throw "build failed - see the messages above" }

# ---------- 4. install next to the app ----------
Step "Copying to $Out"
New-Item -ItemType Directory -Force -Path $Out | Out-Null
$bin = @("$Src\build\bin\Release", "$Src\build\bin") | Where-Object { Test-Path "$_\whisper-cli.exe" } | Select-Object -First 1
if (-not $bin) { throw "whisper-cli.exe wasn't produced" }
Copy-Item "$bin\*.exe", "$bin\*.dll" -Destination $Out -Force -ErrorAction SilentlyContinue
# quick check that it starts. whisper-cli prints its help to stderr, which Windows PowerShell 5.1 would treat
# as an error with $ErrorActionPreference = Stop - so run it through cmd and only look at the exit code.
$ErrorActionPreference = "Continue"
& cmd /c "`"$Out\whisper-cli.exe`" --help >nul 2>&1"
$ok = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"
if (-not $ok) {
    Write-Host "whisper-cli.exe was built but didn't start (exit $LASTEXITCODE) - a DLL may be missing; see above." -ForegroundColor Yellow
}
Write-Host "`nDone: $Out\whisper-cli.exe and whisper-server.exe" -ForegroundColor Green
Write-Host "To use it on this PC: Transcription tab > Engine: whisper.cpp, then Save."
Write-Host "To let this PC transcribe for another one:"
Write-Host "  `"$Out\whisper-server.exe`" -m <model .bin> --host 0.0.0.0 --port 8080"
Write-Host "  then on the main PC: Transcription tab > Add PC > whisper.cpp server."
Write-Host "The first episode downloads the speech model; the Log shows 'whisper.cpp ... using Vulkan0 backend' when the GPU is used."
try { Remove-Folder $Src } catch { Write-Host "(The build folder $Src can be deleted later.)" }
