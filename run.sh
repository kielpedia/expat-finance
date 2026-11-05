#!/usr/bin/env bash
# Starts Household Finance on this computer only (http://localhost:8501).
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/python -c "import streamlit, pandas, plotly, requests, openpyxl, pypdf, keyring, defusedxml" 2>/dev/null || .venv/bin/pip install -q -r requirements.txt
exec .venv/bin/streamlit run app.py
