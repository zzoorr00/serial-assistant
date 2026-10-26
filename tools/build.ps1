$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw 'Create .venv and install requirements-build.txt first. See README.md.'
}
Push-Location -LiteralPath $projectDir
try {
    & $pythonExe tools\freeze.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
    $exePath = Join-Path $projectDir 'dist\串口助手.exe'
    $hash = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash
    Write-Output "Built: $exePath"
    Write-Output "SHA256: $hash"
} finally {
    Pop-Location
}
