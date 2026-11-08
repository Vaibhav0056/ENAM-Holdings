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

## Step 5 — Pull research
- What: finn/pull.py scrapes screener.in Documents (consolidated URL, then standalone) for the last 4 concall transcripts and the latest annual report, caches PDFs by sha256, splits transcripts into speaker turns, keeps management turns only, extracts GuidanceItem / ReportedActual / snapshot (CHEAP, cached per document), verifies every quote on its page, matches in Python, then runs ONE STRONG synthesis on verified JSON only. POST /research/{ticker} plus GET /research/job/{id} expose the stages; reports are cached 24h.
- Decision: management speakers come from the transcript's own MANAGEMENT roster (fuzzy name match), with opening-remarks speakers as fallback. Metric matching refuses pairs that differ by a segment word, so "FMEG EBITDA margin" never matches company-wide "EBITDA margin". For open horizons ("medium term", "FY30") the verdict stays pending, and the latest reported figure is attached with an on-track/behind flag, but only for ratio-type targets (%, bps, x, days), never for amounts like capex. Synthesis prose is scrubbed: sentences with numbers not in the evidence JSON are dropped, and bullets must cite known IDs.
- Issue: POLYCAB guides almost entirely on multi-year horizons, so its table is honestly all pending/unverifiable (with 9 rows tracking). Screener's transcript links are AnnPdfOpen.aspx?Pname=…, rewritten to the direct AttachHis PDF (probed: identical bytes).

## Step 6 — Frontend
- What: one Alpine.js + Tailwind (CDN) page with no build step: Today (window picker, score-ranked results / mgmt / generic cards, swipe or tap "Mark reviewed", collapsed routine row), Alerts (unread dots, tab badge, tap opens the card via #card/{id}), Research (stage progress, Promised vs Delivered rows that expand to quote + doc + page, snapshot, credibility, bull/bear) and Settings (thresholds, type toggles, push enable/test, iPhone home-screen hint, portfolio, refresh). PWA manifest, PNG icons rendered with PyMuPDF, and sw.js handling push and notificationclick.
- Decision: dark, one accent (#8ab4ff), green/red only for deltas and delivered/missed, tabular numbers, two-line clamps. Checked at a true 375px width by screenshotting the app inside a 375px iframe, because headless Chrome won't size a window below about 500px. The quiet-window fallback steps back day by day until at least 3 material filings show; the header then says "Since <date>".
- Issue: Titan's P&L has no revenue total, only two sub-lines, so the LLM (correctly) returned nothing. The parser now sums sub-items column-wise in Python, and the OCR'd unit "fin Crores" is recognised. 11 of 14 primary results filings now show a table.
