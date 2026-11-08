# FINN build log

## Step 1 — Setup + portfolio
- What: repo layout, venv, deps, probe scripts (scripts/probe_sources.py, scripts/probe_bse_pdf.py), a validated 16-stock portfolio (scripts/build_portfolio.py), the SQLite schema (finn/db.py) and the single LLM gateway (finn/llm.py).
- Decision: only Python 3.14 is installed locally, so dev runs on 3.14 while the Dockerfile pins 3.11. BSE PDFs live at xml-data/corpfiling/AttachLive/<ATTACHMENTNAME> (probed: 200, application/pdf). All 16 suggested stocks validated, so no replacements were needed.
- Issue: BSE.lookup() crashes in its HTML parser on 3.14 (IndexError), but getScripCode() works, so we use that. No GEMINI_API_KEY in the environment, so every LLM caller must degrade gracefully.

## Step 2 — Ingest
- What: finn/ingest.py pulls 90 days of auto-adjusted prices and 30 days of BSE announcements per scrip, storing category/subcategory/subject, IST filing time, PDF URL and a content hash. POST /api/refresh runs it in a background thread.
- Decision: the content hash is sha256(scrip|NEWSID|subject|attachment), and the insert is skipped when it already exists, so reruns are idempotent (second run: 0 new). yfinance returns MultiIndex columns, so they are flattened to the field level.
- Issue: none blocking. First run gave 1456 price rows and 239 announcements, with 0 per-ticker failures.
