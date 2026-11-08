# FINN build log

## Step 1 — Setup + portfolio
- What: repo layout, venv, deps, probe scripts (scripts/probe_sources.py, scripts/probe_bse_pdf.py), a validated 16-stock portfolio (scripts/build_portfolio.py), the SQLite schema (finn/db.py) and the single LLM gateway (finn/llm.py).
- Decision: only Python 3.14 is installed locally, so dev runs on 3.14 while the Dockerfile pins 3.11. BSE PDFs live at xml-data/corpfiling/AttachLive/<ATTACHMENTNAME> (probed: 200, application/pdf). All 16 suggested stocks validated, so no replacements were needed.
- Issue: BSE.lookup() crashes in its HTML parser on 3.14 (IndexError), but getScripCode() works, so we use that. No GEMINI_API_KEY in the environment, so every LLM caller must degrade gracefully.
