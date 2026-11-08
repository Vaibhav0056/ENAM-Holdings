# FINN build log

## Step 1 — Setup + portfolio
- What: repo layout, venv, deps, probe scripts (scripts/probe_sources.py, scripts/probe_bse_pdf.py), a validated 16-stock portfolio (scripts/build_portfolio.py), the SQLite schema (finn/db.py) and the single LLM gateway (finn/llm.py).
- Decision: only Python 3.14 is installed locally, so dev runs on 3.14 while the Dockerfile pins 3.11. BSE PDFs live at xml-data/corpfiling/AttachLive/<ATTACHMENTNAME> (probed: 200, application/pdf). All 16 suggested stocks validated, so no replacements were needed.
- Issue: BSE.lookup() crashes in its HTML parser on 3.14 (IndexError), but getScripCode() works, so we use that. No GEMINI_API_KEY in the environment, so every LLM caller must degrade gracefully.

## Step 2 — Ingest
- What: finn/ingest.py pulls 90 days of auto-adjusted prices and 30 days of BSE announcements per scrip, storing category/subcategory/subject, IST filing time, PDF URL and a content hash. POST /api/refresh runs it in a background thread.
- Decision: the content hash is sha256(scrip|NEWSID|subject|attachment), and the insert is skipped when it already exists, so reruns are idempotent (second run: 0 new). yfinance returns MultiIndex columns, so they are flattened to the field level.
- Issue: none blocking. First run gave 1456 price rows and 239 announcements, with 0 per-ticker failures.

## Step 3 — Push pipeline
- What: finn/push.py classifies (ordered keyword rules, then ONE batched CHEAP call for the unmatched), enriches results (finn/results.py) and management changes, scores type_weight × magnitude × novelty into attention / worth_a_look / routine, and writes batched one-liners for worth_a_look items. Result: 239 filings, 0 failures; 10 of 12 quarterly results produced a QoQ/YoY table in ₹ crore.
- Decision: the LLM reads the P&L table and a deterministic line parser cross-checks it (same basis only); disagreement or a >±300% move sets confidence=low. The LLM was right where the parser wasn't: ITC prints columns as current / last-year / previous quarter, which only header-reading catches. Novelty groups related filings (±3 days, same type) so a "Result" + "Outcome of board meeting" pair yields one attention card; the other drops to routine.
- Issue: gemini-2.5-* is retired for new keys, and the free tier allows only 20 requests per model per day. Fix: CHEAP=gemini-3.5-flash, STRONG=gemini-3.8-flash, a per-tier model pool that rotates on daily-quota 429s (recorded in llm_quota), and batching. Scanned PDFs (Dixon) and bank P&L layouts (HDFC Bank) show "Figures not extracted" rather than a guess. The key the user put in .env.example was moved to the gitignored .env.

## Step 4 — Alerts
- What: finn/alerts.py seeds alert_rules (volume ≥ 2.0× 20-day average, |move| ≥ 5%, types results/mgmt_change/m_and_a), evaluates the last 10 days after each run, and dedupes on UNIQUE(rule, ticker, date). finn/notify.py pushes new alerts with pywebpush and deletes 404/410 subscriptions; scripts/gen_vapid.py makes the keys.
- Decision: the 20-day volume average excludes the alert day itself. Type alerts skip routine-bucket duplicates so one event triggers one alert. At most 5 pushes per run plus one summary, so a first run can't spam the phone.
- Issue: notify never raises. If VAPID keys are missing it reports "skipped" and the Alerts tab still works as the source of truth.
