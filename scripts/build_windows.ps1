[CmdletBinding()]
param(
    [string]$Python = "D:\shirley_space\software\anaconda3\envs\real-time-voice\python.exe"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BundleDir = Join-Path $ProjectRoot "dist\AiVoiceAssistant"
$PythonHome = Split-Path -Parent $Python
$RuntimeBin = Join-Path $PythonHome "Library\bin"

Set-Location $ProjectRoot
if (-not (Test-Path $Python)) {
    throw "Python interpreter not found: $Python"
}

# Conda keeps DLL dependencies outside Python's DLLs directory. PyInstaller
# does not discover these through pure-Python modules such as sounddevice.
$RuntimeDlls = @(
    "ffi.dll",
    "libcrypto-3-x64.dll",
    "libssl-3-x64.dll",
    "liblzma.dll",
    "LIBBZ2.dll",
    "libexpat.dll"
)
$RuntimeBinaryArgs = @()
foreach ($dllName in $RuntimeDlls) {
    $dllPath = Join-Path $RuntimeBin $dllName
    if (-not (Test-Path $dllPath)) {
        throw "Required runtime DLL not found: $dllPath"
    }
    $RuntimeBinaryArgs += "--add-binary"
    $RuntimeBinaryArgs += "$dllPath;."
}

$PyInstallerArgs = @(
    "--noconfirm",
    "--clean",
    "--log-level", "WARN",
    "--onedir",
    "--windowed",
    "--name", "AiVoiceAssistant",
    "--icon", "assets\joya.ico",
    "--paths", $ProjectRoot,
    "--add-data", "src\voice_orb_static;src\voice_orb_static",
    "--collect-all", "aec_audio_processing",
    "--collect-all", "onnxruntime",
    "--collect-all", "_sounddevice_data",
    "--hidden-import", "aec_audio_processing.audio_processing"
) + $RuntimeBinaryArgs + @("main.py")

& $Python -m PyInstaller @PyInstallerArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

# Keep these files outside _internal so users can edit config.yaml after installation.
Copy-Item "config.yaml" (Join-Path $BundleDir "config.yaml") -Force
Copy-Item "models" (Join-Path $BundleDir "models") -Recurse -Force
Copy-Item "assets" (Join-Path $BundleDir "assets") -Recurse -Force

Write-Host "Bundle created: $BundleDir"
