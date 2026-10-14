#!/usr/bin/env sh
# Linux/macOS setup: creates .venv, installs dependencies, initialises the database.
# Usage:  sh scripts/setup.sh [--with-llm]
set -e
cd "$(dirname "$0")/.."
[ -d .venv ] || python3 -m venv .venv
PY=.venv/bin/python
$PY -m pip install --upgrade pip
$PY -m pip install -r requirements-dev.txt
if [ "$1" = "--with-llm" ]; then
  $PY -m pip install -r requirements-llm.txt --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
fi
[ -f .env ] || cp .env.example .env
$PY -m aplos init-db
$PY -m aplos check-model || true
echo
echo "Setup complete. Start the dashboard with:  .venv/bin/streamlit run app.py"
