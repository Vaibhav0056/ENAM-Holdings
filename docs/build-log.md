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

## Step 7 — Seed, test, deploy
- What: ran ingest + push + alerts for real (239 filings, 30 alerts, 0 failures) and the full refresh endpoint end to end. Pre-ran research for POLYCAB (portfolio), KPITTECH and BEL (both unseen, triggered through POST /research/{ticker}). BEL shows 4 delivered and 1 missed (FY26 capex ₹890 cr vs ₹1,000 cr guided), each with quote + page. tests/test_core.py has 50 passing tests. Dockerfile (python:3.11-slim) + render.yaml (one web service, 1 GB disk) + scripts/start.sh, which seeds an empty disk from the committed data/seed.db (1.1 MB). Web Push was checked against Google's real push service: VAPID-signed, encrypted, rejected for a fake token, and the subscription pruned (404/410 path).
- Decision: ship a seed snapshot rather than seeding on start, because a cold start would spend the free-tier LLM quota and take minutes. A fresh-boot simulation (empty FINN_DB on another port via start.sh) served the ranked feed and BEL research instantly. Matching fixes found while pre-running: "FY2025-26" periods now parse as FY26, and amount/investment words are generic, so "CAPEX" matches "Capex Investment Amount".
- Issue: Docker isn't installed on this machine, so the image build was not executed, only the start script. The uvicorn reloader missed some edits mid-session and served stale matching code; the server was restarted and all three reports regenerated (extractions from cache, zero re-reads). KPITTECH's guidance is FY27 or qualitative, so it is honestly all pending/unverifiable.

## Step 8 — Logic hardening
- What: replayed the scoring, alert and matching logic against the real local data (239 filings, 4 cached research reports) and replaced the rules that were wrong on it. Novelty: same-event window is 12 hours outside results (was ±3 days for every type). Magnitude: text cues for non-events plus a market-reaction boost, with recent filings re-scored each run. Alerts: median volume baseline with an escalation-only cooldown, and a volatility-relative, market-adjusted price trigger beside the fixed %. Research: period length and unit are part of the match, repeated promises are collapsed, direction is right for savings/reductions, figures must be printed in their quote, and a Python hit rate is shown.
- Decision: every change had to fix a row in the data. Text similarity was rejected for duplicates: CRISIL and ICRA rating filings are near-identical text and different events, while true duplicates are always filed within minutes. Volume alone does not earn a reaction boost (2× median happens too often to attribute to a filing), and on a results day the reaction belongs to the results filing.
- Issue: on the 239 filings, 46 scores changed: 7 distinct events wrongly halved as duplicates are back (L&T order win, Polycab NCLAT order, Persistent ICRA rating), 4 non-events left Attention (two shell-subsidiary incorporations, a rating re-affirmation, a re-appointment). Alerts went 30 → 31: five repeat volume alerts inside one burst dropped, TITAN +4.2% (3.2σ) and TATAPOWER −3.3% (4.0σ) added. BEL's table went from 24 rows to 14 distinct promises (75% hit rate on 4 checkable). Targets stated only in words ("three million vehicles") now show as unverifiable. The committed seed.db still carries scores from the old logic until it is re-exported.

---

# FINN v2: ENAM AI Engineer Build Challenge

The challenge spec supersedes Steps 1-8. Those built a working SQLite MVP; v2 rebuilds it on Postgres (Neon) phase by phase, porting the logic that already proved itself (scoring, results parsing, grounding, guidance matching) rather than rewriting it.

## Phase 0: Setup + probes

**Plan.** What: repo layout, `.env.example`, `tasks.py`, SQLAlchemy models + Alembic on a Neon dev branch, `finn/fiscal.py` with tests, a probe per source, validation of the 25 tickers, `/health` without a DB call. Why: every later phase stands on the schema, the fiscal calendar and verified source shapes. Risks: Neon latency from India against the < 300 ms feed target; Gemini free-tier quotas and no Pro model; BSE listing gaps for NSE-only stocks.

**Built.**
- Neon: project green-breeze-48350921 (aws-ap-southeast-1, Singapore, Postgres 18.6). A `dev` branch was created with `neon branches create` rather than `neon checkout --create`, because the `neon.ts` policy that `neon init` wrote gives new branches a 7-day TTL and the dev data would vanish. `.env` points at the dev branch; Alembic uses the unpooled endpoint for DDL, the app the pooled one.
- Schema (Alembic `1396ff35f08f`): the 26 tables of spec §4.1 plus `calendar_events` (catalysts), `research_jobs`, `llm_quota` and `ingest_runs`. pgvector `vector(768)` on `chunks` with an HNSW cosine index; generated `tsvector` columns + GIN on `pages`, `chunks` and `filings`. JSONB for payloads. SQLite fallback for unit tests maps JSONB to JSON and vectors to JSON lists.
- `finn/fiscal.py`: Indian FY periods (quarter/half/9M/year, ordered by end date), a parser for the forms filings use ("Q2 FY26", "2QFY26", "FY2025-26", "quarter ended 30 September 2025", "H1 FY26"…), IST↔UTC, ₹ lakh/million/billion/'000 → crore, NSE trading days and "first session that could react to a filing" (15:30 IST close). 46 tests.
- `finn/llm/router.py`: `call(task, prompt_id, inputs, schema, tier)`. Versioned prompt files, sha256 cache key over model + prompt@version + canonical inputs + schema, Pydantic validation with one corrective retry, 30 s timeout with SDK retries off, exponential backoff on transient errors, daily-quota rotation inside a tier, no cheap→strong escalation (raises), per-report budget guard, every call logged with tokens/cost/latency. `embed()` for 768-dim vectors, cached per text. 11 tests with a fake client. The Step 1-8 gateway moved to `finn/llm/legacy.py` and stays only until each old module is ported.
- `tasks.py` (setup, migrate, seed, validate, probes, ingest, pipeline, serve, test, evals); `finn/seed.py` (idempotent: second run adds 0); `/health` returns without touching the DB (tested by making any DB access raise).

**Probes (raw output in `docs/probes/`).**
| Source | Real shape and finding |
|---|---|
| Neon | Postgres 18.6, pgvector 0.8.6, pg_trgm available. First connect 1.5-1.9 s (compute wake), warm round trip 0.30-0.38 s from this machine |
| BSE (`bse` 3.3.4) | `announcements(scripcode, from_date, to_date)` → `{"Table": rows, "Table1": [{"ROWCNT"}]}`; 28 fields incl. NEWSID, DissemDT (IST, naive), CATEGORYNAME, SUBCATNAME, NEWSSUB, HEADLINE, MORE, ATTACHMENTNAME, XML_NAME (set on all 43 TCS rows). One page held all 43 TCS filings for 90 days (0.5 s). `resultCalendar()` → scrip_Code, short_name, meeting_date ("11 Nov 2026"); `actions()` → Purpose, Ex_date, RD_Date, BCRD_FROM/TO; `equityMetaInfo()` → ISIN, Sector, Industry, ISubGroup |
| yfinance 1.8.0 | `download(period="90d")` → 90 rows, MultiIndex columns (Adj Close, Close, High, Low, Open, Volume), naive dates. ^NSEI works (market leg for relative moves). Dividends/splits available for ex-date suppression. 10 Nov 2026 is missing from both series, matching the NSE Diwali holiday |
| screener.in | `section#documents`: `.concalls li` with a "Transcript" link (18-26 per company), `.annual-reports li a` (4-15). Links are mostly BSE `AnnPdfOpen.aspx?Pname=` or `AttachHis/`, some on company sites (divislabs.com) |
| Gemini | 25 generate models. gemini-2.5-* → 404 for new users. gemini-4-carbon / gemini-4-argon (the only Pro-class options) → 429 with zero quota on this key. gemini-3.5-flash structured output OK in 3.3 s; gemini-3.8-flash answered once in 295 s and then hit 504 at a 45 s deadline; gemini-3.7-flash 503 (high demand). gemini-embedding-2 and -001 honour `output_dimensionality=768` |
| Tavily | No `TAVILY_API_KEY`: skipped. Management bios fall back to the profile in the filing itself, cited to the filing |

**Tickers.** 25 checked, 25 pass after one swap. CDSL → **CAMS**: CDSL is listed on NSE only (BSE is one of its promoters), so it has no BSE scrip code and no BSE filings. CAMS is the closest BSE-listed peer: asset-light capital-market infrastructure with annuity-like fees. No holding or peer falls in ENAM's excluded industries (checked against BSE industry). A BSE timeout once failed DEEPAKNTR; the validator now retries timeouts, since a timeout is no evidence a scrip doesn't exist.

**Decisions and rejected alternatives.**
- Strong tier = gemini-3.8-flash with gemini-3.7-flash / 3.5-flash as same-tier fallbacks. No Pro model is usable on this key, so "strong" means "the best Flash available", stated in every report receipt. Rejected: silently using the cheap model for synthesis.
- Kept Python 3.14 locally (3.11 isn't installed); the Dockerfile will pin 3.11 and CI runs there.
- Kept the Step 1-8 app serving from SQLite during the port, so there is always a working build. Rejected: a big-bang rewrite.
- Cost is estimated at list prices (flash-lite $0.10/$0.40, flash $0.30/$2.50 per 1M tokens in/out). The free tier bills $0, but receipts and the budget guard should reflect what a paid key would cost.

**Problems.** gemini-3.8-flash latency is the biggest risk for the < 90 s cold-research target; the router's 30 s timeout plus fallback contains it. A Neon round trip of ~0.3 s means the feed must be one query (or cached in-process) to hit p95 < 300 ms. The bash heredoc in this environment mangles long Python files, so code is written with the editor tool.

**Checkpoint.** 25/25 tickers resolve (1 swap, logged). Every probe is recorded above with its real response shape. Tests: 120 passed (46 fiscal, 11 router, 1 health, 62 carried over). ✅
