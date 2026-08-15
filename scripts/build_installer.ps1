[CmdletBinding()]
param(
    [string]$Python = "D:\shirley_space\software\anaconda3\envs\real-time-voice\python.exe",
    [string]$InnoSetup = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

& (Join-Path $PSScriptRoot "build_windows.ps1") -Python $Python
if ($LASTEXITCODE -ne 0) {
    throw "Application bundle build failed"
}

if (-not $InnoSetup) {
    $candidates = @(
        "D:\shirley_space\software\InnoSetup 6\ISCC.exe",
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    $InnoSetup = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $InnoSetup -or -not (Test-Path $InnoSetup)) {
    throw "Inno Setup 6 was not found. Install it or pass -InnoSetup <path-to-ISCC.exe>."
}

& $InnoSetup (Join-Path $ProjectRoot "installer\ai-voice-assistant.iss")
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE"
}

Write-Host "Installer created under: $(Join-Path $ProjectRoot 'release')"
