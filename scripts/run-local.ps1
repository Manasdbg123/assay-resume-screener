# Run the app locally on Windows, without Docker.
#
#   .\scripts\run-local.ps1              # start the app on http://localhost:5000
#   .\scripts\run-local.ps1 -Setup       # create the venv and install dependencies first
#   .\scripts\run-local.ps1 -Eval        # run the baseline-vs-Gemini evaluation
#   .\scripts\run-local.ps1 -Test        # run the test suite
#
# If PowerShell blocks the script, allow local scripts for this session:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

param(
    [switch]$Setup,
    [switch]$Eval,
    [switch]$Test,
    [int]$Port = 5000
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venvPython = Join-Path $root ".venv\Scripts\python.exe"

if ($Setup -or -not (Test-Path $venvPython)) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    python -m venv .venv
    & $venvPython -m pip install --upgrade pip
    Write-Host "Installing dependencies..." -ForegroundColor Cyan
    & $venvPython -m pip install -r requirements-dev.txt
}

if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Host "No .env found. Copying .env.example - add your GEMINI_API_KEY to it." -ForegroundColor Yellow
    Copy-Item .env.example .env
}

if ($Test) {
    & $venvPython -m pytest
    & $venvPython -m ruff check .
    exit $LASTEXITCODE
}

if ($Eval) {
    & $venvPython -m eval.evaluate --engine both --json eval/results/run.json
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Starting ResumeAI on http://localhost:$Port" -ForegroundColor Green
Write-Host "Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

$env:FLASK_PORT = $Port
& $venvPython wsgi.py
