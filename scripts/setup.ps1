# Windows setup: creates .venv, installs dependencies, initialises the database.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 [-WithLLM]
param([switch]$WithLLM)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path ".venv")) { py -3.11 -m venv .venv }
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -r requirements-dev.txt
if ($WithLLM) {
    & $py -m pip install -r requirements-llm.txt --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
}
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
& $py -m aplos init-db
& $py -m aplos check-model
Write-Host "`nSetup complete. Start the dashboard with:  .\.venv\Scripts\streamlit run app.py"
