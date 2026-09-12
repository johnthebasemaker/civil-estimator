#!/usr/bin/env bash
# Launch Civil Estimator with the project's own Python.
#
# `streamlit` on PATH is the system framework Python, which does not have
# PyMuPDF — the Extract page dies with "No module named 'fitz'" on import.
# The project's dependencies live in ./venv, so the app must be started from
# there. Use this script rather than a bare `streamlit run Home.py`.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x venv/bin/streamlit ]; then
  echo "venv/bin/streamlit not found. Create the environment first:" >&2
  echo "  python3 -m venv venv && venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

if ! curl -s -m 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  echo "note: Ollama is not responding on :11434 — drawing extraction will be"
  echo "      unavailable until you start it with:  ollama serve"
  echo
fi

exec venv/bin/streamlit run Home.py "$@"
