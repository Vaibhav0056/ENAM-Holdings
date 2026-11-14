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

## Phase 1: Ingest

**Plan.** What: 90 days of prices (25 companies + NIFTY 50), 90 days of BSE filings, a catalysts calendar, screener.in document links, and polite PDF fetching cached by sha256. Why: the app must read only the database, so every later phase depends on clean, deduplicated source rows. Risks: BSE timeouts, screener.in rate limits, company-hosted PDFs that block bots, NULL-keyed index rows defeating uniqueness.

**Built.** `finn/sources/`, one adapter per source: `prices.py`, `bse_filings.py`, `calendar.py`, `screener.py`, `pdfstore.py`, plus `common.py` (generic User-Agent, ≥ 1 s per host, retry on 429/5xx, `INSERT … ON CONFLICT` helper that counts true inserts) and `run.py` (`python tasks.py ingest [prices bse calendar docs pdfs]`).
- Filings: content hash over scrip|NEWSID|subject|attachment; IST `DissemDT` stored as UTC; BSE's own summary kept as `body`; revised/corrigendum filings linked to the filing they supersede (same sub-category, ≤ 7 days earlier).
- Calendar: BSE result calendar + corporate actions (dividend titles normalised to "Interim Dividend ₹7/share", record dates kept separately) + meeting dates parsed out of board-meeting intimations.
- Documents: 8 transcripts + 2 annual reports listed per company (period inferred: an Aug 2026 call covers Q1 FY27). PDFs cached at `data/pdfs/<sha256>.pdf`; a PDF averaging < 200 characters per page is `unparseable`, never OCR-guessed. `data/doc_overrides.csv` can add documents screener.in doesn't list.
- Migration `6e5b1dbba28d`: prices are unique on (symbol, date), not (company_id, date). The ^NSEI rows have no company, and NULLs never collide, so re-runs would have duplicated every index bar. Alembic now ignores the hand-made tsvector/HNSW objects so autogenerate can't propose dropping them.

**Counts (first run).** 2,340 price rows (25 × 90 + 90 NIFTY), 855 filings, 30 calendar events (4 in the next 7 days), 242 documents listed, 24 latest transcripts parsed (12-38 pages each, 521 pages stored). Per company, filings over 90 days ranged from 9 (DIVISLAB) to 75 (PERSISTENT). Full table: `docs/probes/ingest_rerun.txt`.

**Decisions and rejected alternatives.**
- Only the newest transcript per company is fetched at ingest; older transcripts and annual reports are fetched on demand by research. Rejected: fetching all ~240 PDFs up front, which means ~1 GB of annual reports and several hundred polite requests for documents most users never open.
- APOLLOHOSP hosts its two newest transcripts on apollohospitals.com, which returns 403 to automated requests. The block is respected: those rows stay `failed` with the reason, and the fetcher falls back to the next listed transcript (Q3 FY26, parsed, 18 pages). Rejected: spoofing a browser to get past it.
- TRENT lists no earnings-call transcripts (it doesn't hold calls). Per §10 its research runs on annual reports only and the UI will say so.
- The NSE holiday list in `finn/fiscal.py` is checked against the data: the weekdays missing from ^NSEI bars in the window are exactly 14 Sep, 2 Oct and 20 Oct 2026, all in the list.

**Problems.** One BSE metadata timeout during validation (now retried). PyMuPDF warns that the `fitz` alias is deprecated, so the code imports `pymupdf`. Failed documents are retried on each run; a permanent 403 costs one request per run, acceptable at this scale.

**Checkpoint.** Second full run: 0 price rows, 0 filings, 0 calendar events and 0 documents added; SQL checks find 0 duplicate filings and 0 duplicate price bars. Tests: 139 passed (19 new for the adapters, including idempotent inserts and revision linking). ✅

## Phase 2: Push core

**Plan.** What: rules-first classification with a cheap-model fallback, a stored score breakdown, buckets, one-liners for "worth a look", `/digest?from&to` with presets and "since I last checked". Why: the feed is the product's front door and has to be right before enrichment makes it rich. Risks: rules overfitting to a few subjects; one event filed several times; a slow feed because Neon is ~85 ms away.

**Built.** `finn/feed/`: `classify.py` (15 types, ~120 ordered patterns, batched enum-constrained fallback), `score.py` (type_weight × magnitude × novelty × thesis_multiplier, every factor and its reason stored), `pipeline.py` (classify → score → one-liners; re-scores the last 5 days each run; `--all`, `--reclassify-llm`), `digest.py` (one query, zero LLM calls, 60 s in-process cache invalidated by writes), `finn/web.py` (`/digest`, `/api/state/checked`, review, `/metrics/latency`), `finn/marketmath.py` (shared price maths, market leg = NIFTY 50). Versioned prompts `classify.v1/v2`, `one_liner.v1`.

**Numbers.** 855 filings: 676 typed by rules, 179 by the model in 5 batched calls; 0 failures. Buckets over 90 days: 102 attention, 177 worth a look, 576 routine. LLM spend for the whole phase: 19 calls, ~52k input / ~20k output tokens, ≈ $0.023 at list price (classification $0.0085, one-liners $0.0145). One-liners only for worth-a-look and un-enriched attention cards; routine gets none.

**Decisions and rejected alternatives.**
- Kept the Step 8 scoring ideas that were each justified by a real row (same-event windows, text cues, market-reaction boost) and added the spec's factors. Rejected: starting the scorer from scratch.
- Results twins drop to 0.25 (fold under the primary card) while the spec's 0.5 applies to follow-ups. A duplicate results filing at 0.5 would sit in "worth a look" as a second copy of the same news.
- Follow-up demotion needs an earlier filing of the same type within 45 days. Without that, "Update on the takeover offer" is the first the investor hears of it. Inside one event, the filing with the strongest wording leads, not the earliest.
- Order size: L&T's own bands (significant/large/major/mega) scale magnitude; a press-release deal with no stated value is 0.8. Order value ÷ trailing revenue arrives with results extraction (Phase 3).
- On a company's results day, a bare "Outcome of Board Meeting" or "Press Release" is part of the results event. Typed `results`, it folds under the results card instead of floating as "other".
- Headlines: BSE subjects are often boilerplate ("Announcement under Regulation 30 (LODR)-Credit Rating"); the headline falls back to the first real sentence of BSE's summary, skipping "This is to inform you that…" and not splitting on "Mr." or initials.
- Feed reads use an AUTOCOMMIT engine without pre-ping: one round trip (~85 ms) instead of four (~300 ms). Rejected: Neon's HTTP driver (not available from Python) and a local read replica (premature).

**Problems found by the eval, and fixes.** First run: 85 % classification, 100 % material recall, only 50 % of attention material. Misses were rule gaps, each fixed generally: promoter reclassification → routine; results-day bare filings → results; prompt v2 (bond calls are not fundraises; client-deal press releases are order wins). A real bug surfaced while labelling: Persistent's takeover of Nagarro never reached attention, because every filing begins "Update on the offer" and the follow-up penalty halved them all, including "Persistent Secures Strong Majority Stake". Now the first update and the majority-stake release are in attention, later updates in worth a look.

**Checkpoint.** `evals/filings_labeled.csv` (40 rows, stratified by type, labels provisional for the user to correct; labelled before any tuning): classification 95 %, material recall in attention 100 % (4/4), attention 80 % material. Blind holdout `evals/filings_holdout.csv` (20 unseen rows, labelled before seeing predictions, never tuned on): classification 100 %, material recall 100 % (2/2). Feed: 85-180 ms for 24h/3d/7d, 0.37-0.86 s for 30d (optimised in Phase 7). Tests: 165 passed. ✅

## Phase 3: Results + management + governance

**Plan.** What: results cards (Revenue, EBITDA, PBT, PAT; QoQ, YoY; basis; ₹ crore; sanity checks; quote + page per figure), management-change cards (who, role, in/out, date, cited profile, web bio when a Tavily key exists), governance red flags, and order size for the magnitude factor. Why: these are the cards the brief names explicitly, and the feed's ranking depends on their numbers. Risks: scanned PDFs, bank P&L layouts, lakh/crore/million units, the free tier's ~20 calls per model per day (the default cheap model was already exhausted today).

**Built.** `finn/enrich/`: `results.py` (parser-first, model second, Python maths), `mgmt.py` (batched people extraction, verified profile quotes, Tavily hook, senior-exit flags), `governance.py` (red-flag rules), `docs.py` (filing PDFs through the shared PDF store as `documents.kind='filing'`), `run.py` (enrichment pass, fail-soft). `finn/sources/bse_snapshot.py` stores BSE's structured results snapshot per company (migration `b0e71db43a6c`). The pipeline now scores, enriches, re-scores, and lets the card that leads an event borrow its twin's extraction. Quote threshold raised from 85 to the spec's 90.

**XBRL probe.** Not usable: a filing's `XML_NAME` names the announcement record, and every XBRL URL pattern returned 404. BSE's `resultsSnapshot` does give its own structured revenue and net profit for the last two quarters and last FY. It has no year-ago quarter, no expense lines and no stated basis (it turned out to be standalone for several companies), so it is a cross-check and the trailing-revenue source for order sizing, never the displayed figure.

**Numbers.** 22 primary results filings: 16 cards with tables (15 consolidated), 6 honest "not extracted" cards: DEEPAKNTR (scanned PDF), DIXON and NAUKRI (no readable statement in the text layer), TRENT (board outcome without the statement), HDFCBANK (bank layout, unverified), MARUTI (rejected, see below). 43 management cards with verified people (5 senior exits, 6 independent-director exits flagged), 23 orders sized, 26 governance flags (9 insolvency/NCLAT, 6 pledge). LLM spend for the phase: 18 results calls + 12 batched management calls ≈ $0.026 at list price.

**Decisions and rejected alternatives.**
- Parser first, model second. When the deterministic parser reads the consolidated statement (or the only statement) and BSE's snapshot agrees within 1 %, the figures are verified by two independent readings and no model call is made. Rejected: model-first for every PDF (cost, and a single reading).
- A figure that is off BSE's own number by an exact power of ten is rejected, not shown. Maruti's scan prints thousands separators as dots ("533-402", "31.658"); the model read ₹533 crore against BSE's ₹53,340 crore. Rejected: rescaling by the factor (that is guessing).
- When the parser disagrees with the model but BSE agrees with the model, BSE breaks the tie and confidence stays high (Apollo, CAMS).
- The model-free shortcut never crowds out a consolidated statement: CAMS and Sun Pharma first matched BSE on standalone figures while a consolidated statement existed.
- PBT − tax vs PAT beyond 5 % lowers confidence one step rather than rejecting: consolidated statements legitimately add associates and exceptional items after tax.
- A management profile is the filing's own words, quoted and verified on its page, never model prose. No Tavily key is set, so web bios are off; the hook is wired and tested.
- Results period falls back to the last completed quarter on the filing date when the PDF text doesn't name it.

**Problems.** All three cheap models returned 503 "high demand" for part of the first run, so Persistent and HDFC Bank failed transiently; Persistent succeeded on the re-run. Older BSE attachments return 404 at `AttachLive`; the fetcher now retries `AttachHis`. OCR mangles labels ("Revenue from operalions"), which hid CAMS's consolidated page until page detection matched the stem.

**Checkpoint.** `evals/results_gold.json`: 10 statements hand-read from the PDF text independently of the extractor (provisional, for the user to spot-check) plus 1 scanned PDF. Exact match 90 % of 90 cells (all 9 misses are Maruti, correctly withheld), unit errors 0, the scanned PDF shows "not extracted". Phase 2 evals unchanged (95 % / 100 %; holdout 100 % / 100 %). Tests: 187 passed. ✅

## Phase 4: Research core

**Plan.** What: R1-R4 for any NSE ticker: fetch up to 8 transcripts + 2 annual reports, specialist extraction in parallel, quote verification, Python matching and verdicts, credibility score, vagueness, guidance drift, business snapshot, bull/bear + devil's advocate, receipt, SSE progress, 24 h cache. Why: the Management Credibility Ledger is the product's second idea and the brief's "promised vs delivered". Risks: model 503s under load, long transcripts, metric wording that defeats matching, the 90 s cold target.

**Built.** `finn/research/`: `transcripts.py` (speaker turns; roles from the MANAGEMENT block or the opening remarks; speaker check; Q&A pairs for later deep dives), `ledger.py` (metric matching with segment guard, money units normalised to ₹ crore, verdicts, dedupe of repeated promises, recency-weighted credibility, vagueness %, per-quarter trend, drift), `engine.py` (jobs, parallel fetch and extraction, verification, synthesis, receipt, normalised promises/actuals/ledger_rows, cache). `finn/guards.py`: number check at the printed precision and an advice-phrasing guard. Prompts: `promise_extract.v1-v3`, `actuals_extract.v1`, `facts_extract.v1`, `synthesis.v1-v2`, `devils_advocate.v1`. Routes: `POST /api/v2/research/{ticker}`, `GET …/job/{id}`, `GET …/job/{id}/events` (SSE), `GET /api/v2/research/{ticker}`.

**Results.** POLYCAB: 10/10 sources read, 63 distinct promises (most target FY30, so honestly pending), drift shows the capex range cut and a dividend-payout target that stopped being repeated; synthesis passed the guards with 0 sentences removed. KEI (unseen, cold): 9/10 sources, credibility 24/100 on 3 measured promises: FY26 EBITDA margin delivered (11.81 % vs 11 %), net sales growth missed (19.27 % vs 25 %+), volume growth missed (6.2 % vs 16-18 %). ASTRAL (unseen, cold): 7/10 sources read, the other 3 failed on model 503s and retry next run.

**Decisions and rejected alternatives.**
- Two specialist calls per transcript (promises, actuals) instead of one. With one call the model spent its output on actuals: v2 returned 6 promises from Polycab's Q4 FY26 call where v1 had returned 9. Rejected: raising output limits (doesn't fix the competition for attention).
- Prompt v2/v3 rules: relative targets ("1.5x to 2x of industry growth") are numeric; a segment target must name the segment; two related targets (annual capex and a five-year total) are two promises; dated milestones are promises with null bounds. These are general rules, but they were found on Polycab, the same company the gold set uses, so the 80 % recall may flatter other companies (KEI and ASTRAL were not hand-labelled).
- The call's quarter comes from the listing date (a May call covers Q4), with the model's reading kept only as a fallback.
- Money is compared in ₹ crore: "₹1,200-1,600 crore" vs a reported "14.8 billion" was unverifiable until units were normalised.
- "Dropped" guidance means repeated on at least two calls and then silent on every later call; a single mention worded differently later is not a drop (the first rule flagged 8 false drops on Polycab).
- When every strong-tier model is out of quota, synthesis runs on the cheap tier and the receipt says so. This is a disclosed downgrade; cheap tasks still never escalate to strong.
- The advice guard matches advice phrasing ("investors should buy", "BUY rating", "looks undervalued", "target price"), not the words, so "the company sells cables" and "promoters hold 50 %" survive.
- Synthesis prompt v2 states that the credibility score is out of 100 and counts only matured promises: v1 produced "a score of 24 out of 70", where 70 was the promise count. It passed the number check (70 is in the evidence) but was misleading, so the number check alone is not enough.

**Problems.** Free-tier 503s: 14-22 failed calls per cold run, each retried with backoff; failures are not cached, so a re-run fills the gaps. Saving the ledger row by row cost ~58 s per report against Neon; bulk inserts with RETURNING cut a cached-extraction report from 85.6 s to 12.9 s. The first cached-report read paid ~2-3 s for a new Neon connection; reads now use the autocommit path plus an in-process memo (≈105 ms warm).

**Checkpoint.** `evals/guidance_gold.json` (Polycab, 4 calls, 30 promises hand-labelled from management's forward-looking sentences before comparing; provisional): promise recall 80 % (24/30), verdict agreement 100 %, unverified citations shown 0, unsupported-number sentences shown 0 (guards remove them before display). Cached report ≈ 105 ms warm (< 1 s ✅). **Cold research: not met.** ASTRAL took 126.6 s (fetch 39.7 s, extraction 61.0 s with 22 failed calls, verdict 9.6 s, plus ~12 s of the row-by-row saving since fixed), KEI 175 s before the fixes; the < 90 s target and "first section < 20 s" are missed while the free tier returns 503s, and the report arrives whole rather than section by section. Tests: 211 passed.

## Phase 5: Thesis Guardian

**Plan.** What: a draft thesis per holding from management's own claims (R6), thesis impact and "why it matters" on every card, the Intact / Watch / At risk engine with a timeline, and the results-day verdict against prior guidance. Why: "has anything happened that changes why I own this?" is the product's one question. Risks: invented theses, KPIs that can't be checked, alarm fatigue.

**Built.** `finn/thesis/kpi.py` (KPI values from verified results cards, breach checks, status rules), `finn/thesis/guardian.py` (draft, impacts, statuses, results_day; `python -m finn.thesis.guardian all`), wired into the pipeline after enrichment. Prompts `thesis_draft.v1-v2`, `thesis_impact.v1-v2`. 20 theses drafted (2 calls each where no research report exists: annual-report facts + draft), 246 cards judged in 25 batched calls.

**Decisions and rejected alternatives.**
- KPIs are limited to what Python can check on a results card (revenue growth YoY, EBITDA margin, PAT growth YoY, PAT margin), must be company-wide, and must cite a management target, never only a reported figure. The model ignored the "no peak bars" prompt rule in 3 of 5 drafts (Persistent "PAT growth ≥ 33%" from last year's actual; an EBIT margin mapped onto EBITDA), so the rule is enforced in code. Result: 0 KPIs survive across the 20 drafts, because annual reports state strategy, not company-wide numeric targets, and Polycab's 11-13 % is a Wires & Cables segment target. Theses are guarded by their breakers until the user sets KPIs (Phase 7 thesis editor); the KPI machinery is tested and ready. Rejected: letting drafts invent thresholds.
- Structural floor on impact: results, CEO/CFO/MD/compliance-head changes, capital raises, high-severity governance flags, completed acquisitions and legal action against the company are at least "watch" for every thesis. The model, reading strategy-statement theses, called all of these "none" (thesis agreement 60 %). Prompt v2 also says a "supports" needs a concrete fact, not a launch or a report.
- The 1.3 multiplier applies to "watch" and "threatens" only. With "supports" included, Needs attention grew from 101 to 128 cards and the material share fell to a third; confirmation is shown on the card without interrupting.
- Status: a senior exit is "watch", not "at risk" (HAL's CMD superannuating and Info Edge's planned CEO succession were first shown as At risk). At risk needs a thesis breaker judged "threatens" or an integrity red flag (auditor resignation, fraud, default, regulatory action, modified opinion).
- Results-day verdict uses two sources: guided ratios for that quarter or year from the ledger, and ledger promises whose latest reported figure is for that quarter (Bajaj Finance: AUM growth 26.5 % vs 20-24 % guided for FY27, on track; ROA 4.6 % vs 4.4-4.6 %, on track).
- Found while checking verdicts: a share of revenue matched a growth rate ("export revenue contribution" vs "export revenue growth" 17 %). Shares and growth rates can no longer match; Polycab's guidance recall stayed at 80 %.

**Results.** Statuses: 11 intact, 7 watch (Polycab NCLAT matter, Apollo hospitals-division CEO exit, Asian Paints promoter encumbrance, HAL CMD, Maruti production director, Info Edge CEO transition, Tata Power independent director), 2 at risk (Sun Pharma quality-lapse clarification judged against its quality breaker; CAMS chief platform officer exit judged against its KMP breaker). Results-day verdicts on 2 holdings with research reports. Needs attention 116 over 90 days (from 101).

**Problems.** The thesis eval was tuned on the same 30 cards (labelled before tuning, then the rules were changed), so 90 % flatters a fresh sample. The material share of attention fell to 44 % (tuning set) and 40 % (holdout) because results, senior changes and capital raises now also earn "watch"; material recall stayed at 100 %.

**Checkpoint.** `evals/thesis_labeled.csv` (30 cards, 15 attention + 15 worth a look, provisional labels): affects-thesis agreement 90 % (target ≥ 80 %). All earlier evals still pass. Tests: 217 passed. ✅

## Phase 6: Alerts + push

**Plan.** What: the alert rules (volume, price, filing types), new-promise and peer read-across alerts, dedupe, the catalysts strip, Web Push with a test endpoint. Why: the brief's triggered alerts, and the inbox needs a way to reach the investor. Risks: noisy alerts, ex-dates looking like crashes, re-runs repeating alerts, research re-runs looking like management changing its story.

**Built.** `finn/alerting/engine.py` (five rules over Postgres, dedupe by the `(rule, company, day)` unique key via `ON CONFLICT DO NOTHING RETURNING`), `finn/alerting/notify.py` (pywebpush, VAPID, 5 pushes + 1 summary per burst, 404/410 subscriptions deleted), `finn/pipeline.py` (`python tasks.py pipeline`: feed → alerts → notify, each stage isolated). Routes: `/api/v2/alerts` (+ read, read_all, evaluate), `/api/v2/rules` (GET, PUT with bounds), `/api/v2/push/key|subscribe|test`, `/api/v2/catalysts`.

**Results (last 30 days).** 85 alerts: 41 volume spikes, 13 price moves, 25 material filings, 6 peer read-across (e.g. Divi's results → Sun Pharma), 0 new-promise. Examples: Titan at 6.7× its median volume; Tata Power −3.3 % on a day the NIFTY rose 2.0 % (5.1σ for the stock); Apollo −2.7 % (3.6σ). Catalysts in the next 7 days: Tata Power, HAL and Kaynes results meetings; CAMS ex-date for ₹3.25. Incremental pipeline run: 36 s.

**Decisions and rejected alternatives.**
- Price moves use adjusted closes and are suppressed on the stock's ex-dates and record dates from the BSE calendar.
- A new-promise alert compares only promises said on a newer call than the previous report's newest call. The first version raised "Polycab: 7 new promises" because I had re-extracted the same calls with a better prompt; re-research must never look like management changing its story.
- Peer read-across shows a research-only peer's material filing against the holding it is a peer of, not as a standalone item.
- Alert counts come from `RETURNING`: the driver's rowcount reported 93 inserts where 86 rows existed.

**Problems.** Real-device push needs a browser to subscribe, which arrives with the frontend (Phase 7). The path was checked against Google's push service: a VAPID-signed, encrypted message to a fake FCM subscription was rejected and the subscription deleted.

**Checkpoint.** A rule fires once (second evaluation adds 0) ✅, alerts appear in `/api/v2/alerts` ✅, rule edits are bounded (pct 50 → 400) ✅. Push arriving in Chrome on localhost: pending the Phase 7 UI. Tests: 221 passed.

## Phase 7: Frontend

**Plan.** What: every screen (Today, Holdings + holding page with thesis editor, Research with live progress and the ledger, Alerts, Settings), the card sheet with layered depth and evidence, the `#gallery` page, every state (loading, waking, stale, error, empty, low confidence), dark and light themes at 375 px. Why: the evaluators see FINN through this. Risks: render-blocking CSS, layout shift from late data, contrast in two themes. `docs/UI_BRIEF.md` was not added, so the §8 principles were followed.

**Built.** `static/index.html` (Alpine templates, inline SVG icon sprite), `static/app.js` (routing, data, swipe-to-review, SSE with polling fallback, push opt-in, theme), `static/app.css` (design tokens for light and dark), `static/sw.js` (app shell stale-while-revalidate, API never cached, push + click-through), `static/vendor/alpine-3.14.8.min.js`. New routes in `finn/web_ui.py`: holdings with status, holding page, thesis editor (PUT, validated, re-runs the status engine), card detail, evidence page text, user state. Company names filled from BSE announcements (the BSE lookup had returned symbols as names).

**Layering.** Card: headline → summary (results mini-table, people IN/OUT, governance flags, results-day verdict, thesis "why it matters") → sheet ("Go deeper": full table with previous quarter and year ago, notes, confidence, against-guidance, people with verified profile quotes, flags, "Why am I seeing this?" score breakdown) → evidence (the stored page text with the quote highlighted, or the PDF). Research ledger rows expand to the quote, speaker, document and page.

**Decisions and rejected alternatives.**
- Plain CSS with design tokens instead of the Tailwind CDN. The Tailwind play CDN compiles CSS in the browser and blocks rendering; the Lighthouse ≥ 90 target was the reason. No build step either way.
- Alpine served from FINN itself (same pinned version, 44 KB): one less cross-origin connection; Research went from 77 to 91 after this plus the layout fixes.
- Network notices (waking, stale, error) float over the content: inline, they appeared and vanished above the feed and caused most of the layout shift.
- The route is read from the URL when the component is constructed: starting on Today and switching after init moved the whole page (CLS 0.23 on Research).
- Rolling windows are precomputed in-process every 50 s and cached under one key each; the first version keyed the cache by the minute and missed constantly.
- The gallery's invented examples use a fictional "SAMPLECO": its first draft paired real companies (Polycab, TCS, Info Edge) with made-up events.
- Found while reviewing the research screen: a JSON `null` leaked into prose ("a credibility score of null") and a government forecast ("17,000 circuit km") was extracted as a Polycab promise. A placeholder guard now removes such sentences, and promise prompt v4 excludes market, industry and government forecasts. Polycab recall rose to 83 %, verdict agreement 96 %.
- A status timeline reset: entries created by the senior-exit rule fixed in Phase 5 were artefacts of my own fix, not history, so the timeline was recomputed.

**Checkpoint.**
| Screen | Performance | Accessibility | CLS |
|---|---|---|---|
| Today | 92 | 100 | 0.107 |
| Research (POLYCAB) | 91 | 100 | 0 |
| Holdings | 95 | 100 | 0.061 |
| Alerts | 95 | 100 | 0.008 |
Lighthouse 12.8.2, mobile, headless Chrome 157, local server. Feed `/digest`: p50 3.8 ms, p95 20.2 ms over 80 requests (target < 300 ms). App shell 0.22 s from the server, served from the service-worker cache on repeat visits. Every screen checked at 375 px in dark and light (screenshots in the session scratchpad). Real-device push still needs a person to tap "Enable notifications" in Chrome; the subscribe → sign → send → prune path is verified. Tests: 222 passed. ✅

## Phase 8: Deep dives + embeddings

**Plan.** What: hybrid retrieval (full-text + pgvector, Reciprocal Rank Fusion) and the shared recipe engine with eight deep dives in the spec's order. Why: the depth layer the investor taps into from a report. Risks: embedding batches, annual-report tables, recipes that look numeric but rest on unverified lines.

**Built.** `finn/retrieval.py` (transcripts chunked by speaker turn with role; annual reports by page section, embedded lazily; hybrid search with RRF), `finn/deep/maths.py` (all deep-dive figures), `finn/deep/engine.py` (eight recipes, cached as `reports.kind = 'deep:<recipe>'` for 24 h, receipts), prompts `financials_extract.v1-v2`, `dodge_label.v1`, `worry_topic.v1`, `fine_print.v1`, route `GET /api/v2/deep/{symbol}/{recipe}`, and a "Go deeper" block on the research screen.

**Results on Polycab.**
- Hedge-meter (pure Python): 3.1-5.0 cautious phrases per 1,000 management words across eight calls.
- Language diff (embeddings, threshold 0.8): the newest call adds "we are expecting the EHV plant to now commission somewhere in FY 2028", a timeline that earlier calls put at FY26 / CY2026.
- Dodge detector: Q2 FY27 22 answered, 6 partial, 2 deflected of 30 (e.g. "we would not like to give any guidance on that" to a question on wires vs cables growth); every label quotes the answer.
- Worry map: competition questions rose to 5 on the latest call against ~1.8 before (flagged as rising).
- Cash quality / balance sheet: OCF ₹3,811 cr, capex ₹1,480 cr, FCF ₹2,330 cr, interest cover 17.5×, net cash ₹3,702 cr (net debt / EBITDA −0.87). PAT and the working-capital lines did not verify, so OCF/PAT and the days show "Not in sources".
- Fine print: key audit matters (revenue recognition, inventory valuation of wires & cables), no qualification or emphasis of matter, contingent liabilities ₹83.6 cr vs ₹68.1 cr (+22.8 %).
- Peer compare (vs HAVELLS, whose cold research took 90.3 s): Havells 58/100 on 4 measured promises (40 % too vague to check); Polycab not scored (2 measured of 60).

**Decisions and rejected alternatives.**
- Found a real bug: `gemini-embedding-2` treats a list of plain strings as ONE multi-part input and returns a single vector. 778 chunks got 16 vectors, each embedding 50 joined passages, and retrieval returned noise ("why did export growth slow" → a capex question). Each text is now wrapped as its own Content and the count is checked; the 19 bad cached vectors were purged and everything re-embedded.
- Full-text queries OR quoted phrases: `websearch_to_tsquery` ANDs bare words, so "key audit matters emphasis of matter qualified opinion" matched no page and fine print found no audit matters at first.
- Financial extraction runs as two focused calls (flows: cash flow + P&L; stocks: balance sheet + notes); one call over 16 pages returned ~20 figures.
- Implausible working-capital days (outside 7-365) carry a visible "check the quoted line" note rather than being hidden or trusted.
- Peer compare compares stored reports; it never re-reads documents.

**Problems.** PyMuPDF splits statement rows into cells, so a model quote of a full row ("Trade receivables 4,477.84 3,912.10") often isn't a contiguous string on the page and fails verification: correct behaviour, but it leaves cash-quality inputs missing. A table-aware page text (pdfplumber for statement pages) is the next fix. Re-embedding 778 chunks took 7.4 min on the free tier; search is ~2.3 s, mostly the query embedding call.

**Checkpoint.** All 8 recipes run on real documents; LLM-backed ones verify every quote on its page (figures must be printed in the quote) and every number comes from `finn/deep/maths.py`. Cached reopen 0.29-0.37 s (target < 1 s). Tests: 230 passed. ✅

## Phase 9: Copilot

**Plan.** What: a bounded tool-using agent (≤ 6 steps, cost cap) over FINN's own data, with company and portfolio scope, streamed steps and cited answers. Why: questions the fixed screens don't answer ("what did they say about competition?"). Risks: invented facts, advice language, an agent loop that burns the free tier.

**Built.** `finn/copilot.py`: six tools (`search_docs` = hybrid retrieval, `get_page`, `get_metric`, `get_filings`, `get_thesis`, `get_ledger`); the cheap tier picks each step, the strong tier writes the answer from evidence only; every tool result is evidence `E1…` with its document/page or filing; answers are verified sentence by sentence (citations must exist, numbers must appear in the evidence, advice phrasing removed); `Not in sources.` when nothing supports an answer. Cap: $0.02 per question. Route `GET /api/v2/copilot?q=&symbol=` streams `step` and `answer` events. UI: an "Ask about SYM…" bar on Holding and Research, a sheet with suggested questions, live steps, and citations that open the source page. Prompts `copilot_step.v1`, `copilot_answer.v1-v2`.

**Decisions and rejected alternatives.**
- Two tiers inside the loop: steps are cheap and frequent; only the final answer uses the strong tier.
- First eval run: 15/15 cited, 0 unsupported numbers, 0 advice, but only 5 of 10 answerable questions answered. The step model often called `search_docs` with no query; the tool said so, the identical retry ended the loop, and nothing was gathered. Now a missing query defaults to the question, and if a loop ends with no real evidence a fixed plan (search documents, recent filings, promise ledger) runs within the step budget. Answered rose to 8/10.
- Answer prompt v2 speaks of "the filings" and "the call", never "the evidence".

**Checkpoint.** `evals/copilot.jsonl` (15 questions: 10 answerable, 1 not in FINN's data, 3 advice traps, 1 either): cited or "Not in sources" 15/15, sentences with unsupported numbers 0, buy/sell/hold or price-target language 0 (all three traps answered with "FINN doesn't give investment advice" plus what the filings show); answered when an answer exists 8/10 (misses: Bajaj Finance FY27 AUM guidance; KEI's FY26 volume miss, which the ledger has but the agent didn't fetch). One answer cites Polycab's older 50-55-day working-capital range rather than the newest 45-50. Results stored in `evals/copilot_results.json` and re-scored by `python -m evals.run`. ✅

## Phase 10: Experience extras

**Built.** `finn/experience.py` and UI:
- Time-travel replay: `/api/v2/replay?as_of=` shows the feed with only what had been filed by that day, plus each surfaced stock's next-5-session move. Replaying 26 Oct 2026: Dixon +10.4 % and L&T +7.1 % over the next five sessions were both on the feed beforehand. A slider (2-88 days back) on Today.
- Audio brief: "Listen (60 s)" reads the unreviewed attention cards with the browser's Web Speech API (~160 words, en-IN), free and offline.
- Decision journal: "Acting on this" + a one-line reason from any card; the holding page lists entries with the material filings that came after each.
- Evidence strength: ledger rows backed by two or more sources (a promise repeated across calls, or matched to a reported actual) are drawn with an accent bar.
- Feedback loop: Useful / Not useful / Should be higher adjusts that filing type's weight by 0.05 per net signal, bounded to ±0.15 (spec), logged in `user_state.prefs.feedback_log`; recent cards are re-scored immediately.
- "Why am I seeing this?": the score breakdown sheet on every card (since Phase 7).

**Checkpoint.** Each works locally on the phone layout; replay shows which items FINN surfaced before a ≥ 5 % move. Tests for the bounded feedback maths and forward moves. ✅

## Phase 11: Prepare deploy (not deployed)

**Built.** `Dockerfile` (python:3.11-slim, health check on `/health`), `scripts/start.sh` (migrate on Neon's direct endpoint, idempotent seed, serve; boots even if the database is down), `render.yaml` (Singapore, free plan, `/health`, secrets marked `sync: false`), `.github/workflows/daily.yml` (18:30 IST: migrate, ingest, pipeline, snapshot check, run inside GitHub Actions against Neon so the free web instance never runs long jobs; keep-awake ping to `/health` every 10 minutes), `scripts/export_snapshot.py` + `finn/snapshot.py` (DEMO_SNAPSHOT: `data/demo_snapshot.json`, 1.2 MB, served for the main read routes when the database is unreachable or `DEMO_SNAPSHOT=1`, marked with `X-FINN-Snapshot`). `.dockerignore` excludes secrets, local state and the Node/Neon tooling.

**Seeding production.** The intended path is a Neon branch: `neon branches create --name production-v2 --parent dev` copies the dev data copy-on-write in seconds; `python tasks.py setup` is the idempotent alternative on an empty branch.

**Checkpoint.** Not met: Docker isn't installed on this machine, so `docker build` couldn't run. The start script path was exercised instead (migrate + seed + serve), and both snapshot modes were tested (forced, and with an unreachable database: holdings served from the snapshot, `/health` still 200). The README lists the deploy steps.

## Phase 12: Docs

**Built.** `README.md` rewritten for v2: what and why, local run via `tasks.py`, architecture diagram, the evaluation table (matches `python -m evals.run`), cost and latency table, decisions, what was left out, next steps (the Wave 2 Research Arm). `docs/WRITEUP.md` drafted from this log.

**Problem found at the end.** Three legacy tests failed on a second run: the new copilot tests import `finn.llm` during collection, which loaded the Step 1-8 SQLite store before `test_core` set its throwaway path, so those tests wrote into the old `data/finn.db` (gitignored, unused by v2). `tests/conftest.py` now sets the path before any import; two consecutive runs pass.

**Checkpoint.** `python -m evals.run`: ALL PASS, every figure in the README table. Tests: 236 passed (twice in a row). ✅

**Open items carried forward.** Cold research < 90 s on the free tier; `docker build`; real-device push; OCR and bank P&L; table-aware annual-report statements; human review of every `claude-provisional` label.

## After Phase 12: automatic refresh when hosted locally

**Why.** Hosted on a laptop, nothing ran ingest + pipeline (the daily GitHub workflow only fires once the repo is pushed), so after four days the 24h feed was empty. Once a day is also too slow for an inbox: filings land through market hours and results often after 16:00 IST.

**Built.** `finn/scheduler.py`, started with the app: a catch-up run at startup; then every 30 minutes on NSE trading days 08:00-21:00 IST (`FINN_REFRESH_MINUTES`) it fetches the last 7 days of BSE filings and runs the pipeline (classify, score, enrich, thesis, alerts, push); once each evening after 18:30 IST it also refreshes prices, the BSE snapshot and the catalysts calendar. Runs never overlap. It is off on Render (`RENDER` is set there; the workflow refreshes) and with `FINN_SCHEDULER=0`. `GET /api/v2/scheduler` shows the last run; `POST /api/v2/refresh` and Settings → "Refresh now" run one immediately. Filings are content-hashed and model answers cached, so a quiet half-hour costs no model calls.

## Final fix pass, step 1: robust data ingest

**Why.** BSE refused FINN's requests (HTTP 403) from about 10 Nov 2026, and FINN had one source per data type, so the feed silently froze. A production system needs failover, visible health, and runs that cannot corrupt or overlap.

**BSE diagnosis.** The `bse` library (3.3.4, the latest on PyPI) already sends a browser User-Agent, `Origin` and `Referer: https://www.bseindia.com/` on every call, so headers were never the cause. On 15 Nov one diagnostic call returned `200 OK` (`Akamai-GRN` header present, i.e. Akamai in front): the refusal had lifted by itself, consistent with a temporary block after the heavy early-November backfill. FINN does not disguise itself to get past refusals; it is now a politer client: 1.5 s between companies, exponential backoff (2 s, 4 s) on 403/429, stop asking BSE after 3 consecutive refusals in a run, and every 4xx/5xx is logged with status, key headers and the body start (`/health/data` → `last_bse_refusal`).

**Built.**
- `finn/sources/nse.py`: NSE announcements API, NSE announcements RSS, NSE event calendar, NSE bhavcopy. Pure parsers over recorded responses (`tests/fixtures/sources/`). All four answer FINN's own honest User-Agent.
- `finn/sources/chain.py`: one chain per data type, each source getting only the companies the previous one failed for. Announcements BSE → NSE API → NSE RSS; prices yfinance → NSE bhavcopy; events BSE → NSE, plus events derived from stored filings (board meetings and, new, analyst/investor meets with their dates) on every run. Validation before any write (known company, non-empty subject, timestamp not in the future, https PDF, sane prices). One transaction per source write (a failing source never discards another's good rows), idempotent inserts, nothing deleted.
- Circuit breaker per source in a new `source_health` table (migration `c7a1d2e9f310`): 3 consecutive failed runs → skipped for 3 h, then tried again; one success closes it. Shared by every process that ingests.
- Cross-exchange dedupe: same company, within 20 minutes, and the same disclosure topic (director change, analyst meet, allotment, newspaper notice, ...), near-identical text or the same PDF file name → stored with status `duplicate`, hidden from the feed and the pipeline, never deleted. After PDFs are fetched, identical PDF bytes (`documents.sha256`) link the pair exactly. Dry run on 18 real NSE announcements for HDFCBANK, POLYCAB and TCS over 20 days: 16 matched their BSE twin; the 2 unmatched have no BSE filing within 20 minutes on the same topic (checked by hand). A first version matched on text similarity alone and caught only 5 of 18: NSE's summaries are generic ("... has informed the Exchange about ...").
- `finn/ingest.py`: one run = announcements → prices → events → pipeline (classify, score, enrich, thesis, alerts, push) → PDF-duplicate linking. A lease lock (a `source_health` row taken with one atomic UPDATE, 90-minute lease) means GitHub Actions, the local scheduler and `POST /tasks/ingest` never overlap; a crashed run frees it when the lease ends. The first version used a Postgres session advisory lock; the 90-day backfill then died at the unlock (Neon closed the idle locking connection, and session locks are unreliable through the pooled endpoint anyway). The data written by that run was complete; only the unlock failed.
- Schedule 08:00, 16:30, 19:00, 22:00 IST: `.github/workflows/ingest.yml` (cron in UTC: 02:30, 11:00, 13:30, 16:30; `concurrency` group) and `finn/scheduler.py` for a locally hosted app (same slots; catches up on start only if a slot was missed since the last successful run). `POST /tasks/ingest` with header `X-FINN-Task-Token` (= `FINN_TASK_TOKEN`) for cron-job.org.
- Monitoring: `GET /health/data` (freshness, per-source last success / last error / rows added / breaker state, last BSE refusal). If every announcement source has failed for 24 h the run pushes the owner and `python -m finn.ingest` exits 1, failing the GitHub job.
- "Data updated Xh ago" in the Today header (links to Settings), amber only when the last successful announcements fetch is more than 26 h old on a trading day. Settings lists every source with OK / Failing / Paused.
- `.github/workflows/probes.yml` (manual): one request per source from a GitHub-hosted runner, summary in the job page, writes nothing, needs no secrets. **Not run yet**: it needs the repository on GitHub.

**Two latent bugs found and fixed.** A regex `\b` (word boundary) had become a literal backspace character (0x08) in two places: in the new topic table during this step (caught by testing before commit) and, already committed, in `finn/enrich/parser.py`, where it disabled the "dense numbers" signal that ranks candidate P&L pages (ranking only; detection was unaffected). `tests/test_source_hygiene.py` now fails on any control character in source files.

**Results.**
- Probes from this machine (15 Nov 2026): BSE announcements OK, NSE API OK, NSE RSS OK, NSE event calendar OK (18 rows), yfinance OK, NSE bhavcopy OK (13 Nov file).
- 90-day backfill: BSE 879 announcements seen, 39 new (the 10-15 Nov gap); NSE API filled 49 announcements for KEI and ASTRAL (research peers with no BSE code); yfinance 120 new price rows. Filings 943.
- A scheduled run right after: 104.8 s end to end, 21 announcements and 280 price rows re-read, 0 added (idempotent), pipeline rescored 43 cards, 0 failures, lock released.
- Week-ahead: BSE's calendar is healthy (15 results dates and 38 corporate actions market-wide in the next 45 days) but has none for FINN's companies: the September-quarter season is over. Events derived from filings now add 3 real upcoming analyst meets (Havells 17 and 18 Nov, Asian Paints 18 Nov). When there are none, the strip says so.
- Tests: 202 passed (fixtures for every adapter, failover, breaker open/half-open/close, dedupe, idempotence, lease lock, freshness rule, scheduler slots, task-token check, date extraction).

**Open.** Cloud-IP probe results wait for the push. cron-job.org needs `FINN_TASK_TOKEN` set on Render and in the cron job.

## Final fix pass, step 2: alert noise

**Why.** 97 unread alerts, most of them volume spikes of 2-3× on large caps where a 2× day is ordinary, and up to four separate alerts for one company on one day (results + price + volume + peer).

**Built.**
- Volume rule: volume ≥ 3.0× the **20-session average** (was 2.0× the median) **and** a 20-session average traded value of at least `min_avg_value_cr` (default ₹10 crore a day; slider in Settings, 0-200). Escalation-only inside a burst is kept. Trade-off, stated: the mean is lifted by an earlier spike in the window, so a second spike shortly after a first must be larger to count; that is the quieter behaviour asked for.
- One alert per company per day: every rule still records its trigger, then `consolidate()` keeps the highest-priority one active (filing > price move > promise change > peer read-across > volume), lists the others under "Also" on it, and marks them `superseded` (new `alerts.status`, `superseded_by`; migration `d4e8f1a2b3c5`). Superseded alerts are kept, never shown, never pushed.
- Price alerts on corporate-action ex-dates and record dates were already suppressed (Phase 6); unchanged and still tested.
- `python -m finn.alerting.engine --reevaluate` applies the current rules to every stored alert from scratch (undo merges → restate or supersede volume alerts → merge again), so it can be re-run after any rule change. Nothing is deleted.
- The Alerts badge counts unread alerts from the last 7 days; older unread alerts stay listed under their dates ("10 new this week · 48 older unread below").

**Results (live data, 15 Nov 2026).** Re-evaluation of 97 alerts: 30 volume alerts below the new rule superseded, 14 surviving volume alerts restated with the average-based ratio (their titles still said "median"), 9 same-company same-day groups merged. Active alerts 58; **unread in the last 7 days: 10** (target 5-15). The 48 older unread are mostly real results and management-change filings from the Oct-Nov results season that nobody opened. Tests: 205 passed (volume average + liquidity floor, grouping and priority, idempotent consolidation, repeatable re-evaluation).

## Final fix pass, step 3: the slow verdict

**Why.** Rebuilding the POLYCAB report took 503 s, 476 s of it in "Writing the verdict" (two model calls).

**Diagnosis, from `llm_calls` for that rebuild (budget key = the job id).**

| Step | Model | Attempts | Latency each | Outcome |
|---|---|---|---|---|
| synthesis | gemini-3.8-flash | 3 | 28.5-30.8 s | 504 DEADLINE_EXCEEDED |
| synthesis | gemini-3.7-flash | 3 | 28.5-28.7 s | 504 DEADLINE_EXCEEDED |
| synthesis | gemini-3.5-flash | 1 | 15.7 s | OK (15,835 tokens in, 598 out) |
| devils_advocate | gemini-3.8-flash / 3.7-flash / 3.5-flash | 3 + 3 + 3 | 28.2-29.5 s | 504 DEADLINE_EXCEEDED |
| devils_advocate | gemini-3.5-flash-lite | 1 | 2.3 s | OK (16,024 in, 205 out) |

**Cause.** The router gave the SDK a 30 s timeout, which the SDK forwards as the server-side deadline; the strong model needed longer on ~16k input tokens, so the server cut it off at ~28.5 s with 504, and the router treated that as transient and retried the same model three times before falling back. 15 timed-out attempts × ~29 s ≈ 7.3 min, plus backoff sleeps. The two steps also ran one after the other, and each sent every promise, actual and snapshot item.

**Built.**
- Router: 60 s per request (`LLM_TIMEOUT_S`); a deadline moves straight to the next model; on the strong tier a 503/429 also moves on at once (synthesis has a fast model behind it); strong tier order is STRONG_MODEL → CHEAP_MODEL → the rest (CHEAP_MODEL second even when it is also listed as a strong fallback); `deadline_s` caps a step's total time (75 s for synthesis); every result carries `fallback_reasons`, and the receipt shows them (`synthesis_fallbacks`, and the strong-tier note names the reason).
- Synthesis input: verified JSON only (as before), now capped at 24,000 characters, measured promises first, then pending and unverifiable, drift, snapshot, actuals; the receipt states how many items were left out.
- Synthesis and the devil's advocate run in parallel. The devil's advocate now argues against the ledger's own computed reading (score, measured, delivered/missed/pending, vagueness: prompt `devils_advocate.v2`) instead of the synthesis text, which is what made the two independent.
- Bug found by running them in parallel: two threads could each create the shared Gemini client, and the discarded one was closed while in use ("client has been closed"). Client creation is now locked; a test runs 8 threads.

**Results.**
- POLYCAB forced rebuild, warm extraction caches: 503 s → **86 s** on the first run (STRONG_MODEL 503 then no answer in 60 s, flash-lite answered), → 9.8 s once the synthesis answers were cached. Cached reopen: **569 ms**.
- TCS forced rebuild with 19 live extraction calls: 132.7 s total, verdict stage 27.1 s. On that run CHEAP_MODEL had used up its daily free-tier quota and both strong models answered 503 "high demand"; the router moved past each in 4-7 s instead of waiting.
- Target "cold report under 90 s" is met for the verdict stage but not for a fully cold report on the free tier today (extraction calls take 8-18 s each under contention). Step 6 adds the partial state for that path.
- Tests: 212 passed (deadline fallback, strong→cheap order, busy strong model not retried, step budget, evidence cap order, client created once under concurrency).


## Final fix pass, step 4: results extraction

**Why.** Of the 20 results cards for the holdings, 1 was verified, 12 carried a "Verify" badge and 7 showed no figures.

**Card by card, before** (reason as stored): 11 × "differs from BSE's snapshot (it may be on the other basis)" (ASIANPAINT, BAJFINANCE, CAMS, DMART, LT, PERSISTENT, PIDILITIND, POLYCAB, TCS, TITAN), 2 × a confirmation filed as a warning (APOLLOHOSP "BSE's figures confirm the extracted value", SUNPHARMA "revenue and PAT match BSE's structured figures"), 2 × scanned PDF (DEEPAKNTR, HAL), 2 × "no P&L statement found" (DIXON, NAUKRI), 2 × "could not be read and verified" (HDFCBANK, TATAPOWER), 1 × "100× off BSE's numbers" (MARUTI), 1 × verified (KAYNES).

**What the mismatches were.** BSE's structured summary is the **standalone** statement; FINN shows **consolidated**. Example: L&T BSE ₹43,583.58 cr vs consolidated ₹78,749.94 cr; TCS BSE ₹60,441 cr vs ₹73,188 cr. Reading the standalone statement from the same PDFs gives BSE's figures to the paisa (Polycab ₹9,839.66 cr / ₹965.17 cr, TCS ₹60,441 cr / ₹14,104 cr). So the consolidated figures were right; the "Verify" badges expressed no real uncertainty.

**Our bugs, found while checking each card, all fixed:**
1. The parser stopped at the first statement it read and never compared the other basis with BSE.
2. PyMuPDF's text splits a table row's label from its numbers on these layouts; the parser needs pdfplumber's layout text, which enrichment used only for the first 40 pages. Now: candidate P&L pages are found on every page with the fast text, and layout text is taken for those pages (and the page after each) only.
3. Tables that continue onto the next page were never candidates (revenue on one page, profit before tax on the next).
4. OCR artefacts: spaces as thousands separators ("13 066.27" read as 66.27, CAMS PAT), decimal commas ("36024,07", "19,885,3": Sun Pharma's consolidated PAT read as 198,853), "Revenue from operalions", labels with no spaces ("Net Profitfortheperiod/year", Dixon), unit headers mangled ("~ Crore", "I~ in Croresl").
5. A statement that prints no unit at all (Persistent: a header graphic) was unusable. The unit is now inferred only when revenue AND PAT equal BSE's to within 0.05% at that scale, and the card says so.
6. Confirmations were stored as warnings, so "BSE confirms" showed a Verify badge. Warnings (`notes`) and confirmations (`checks`) are separate now; Verify appears only for warnings or low confidence.
7. "Is there a consolidated statement?" looked only at candidate pages, so a standalone reading was accepted while an unread consolidated statement existed (Sun Pharma). The model now gets the consolidated statement's pages first, and a card that shows standalone while consolidated results exist says so.
8. A filing PDF stored as a byte-duplicate of another document returned no pages (`docs.pages` did not follow the link).
9. Regression from step 1: the new ingest chain dropped the BSE results-summary refresh, so HAL and Tata Power had no BSE figures for the quarter. Restored as its own step with a circuit breaker (`bse_snapshot`); an empty answer never overwrites a good summary.

**Built.**
- Mismatch explanation in the card: "BSE's summary is the standalone statement (revenue ₹X cr, PAT ₹Y cr); FINN's standalone reading of the same PDF (₹X cr, ₹Y cr, page N) matches it exactly / is within 0.5% of it". When only the other statement's revenue can be read (L&T page 16: its profit line is OCR-damaged), the card says exactly that.
- When the PDF cannot be read (a scan, OCR garbage, or FINN's own reading fails its checks), the card shows BSE's own revenue and net profit for the quarter, QoQ from BSE's previous quarter, labelled "BSE's own figures · the filing PDF could not be read" with the reason; no year-on-year, EBITDA or PBT (not in BSE's summary).
- XBRL was not used: the BSE summary covers the unreadable PDFs; XBRL parsing is listed under Next steps.
- `python -m finn.enrich.run` gained `reextract_results()` (keeps the results-day comparison), used after any parser change.

**After (20 cards, re-extracted 15 Nov 2026).** 20 show figures, 0 Verify badges:
- FINN's own reading, confirmed by BSE directly: APOLLOHOSP, NAUKRI (standalone; Naukri's consolidated statement is too damaged by OCR to locate, and the card says so).
- FINN's consolidated reading, BSE's standalone summary matched by FINN's standalone reading of the same PDF: ASIANPAINT, BAJFINANCE, CAMS, DMART, PERSISTENT (unit inferred), PIDILITIND, POLYCAB, SUNPHARMA, TCS, TITAN (within 0.5%), LT (standalone revenue only).
- FINN's reading, parser and model agreeing: KAYNES.
- BSE's own figures, labelled: DEEPAKNTR and HAL (scanned), MARUTI (OCR garbage, rejected as 100× off), DIXON (no readable P&L), TATAPOWER (not readable), HDFCBANK (bank layout; FINN's reading failed its checks).

**For manual checking** (₹ crore, current / previous quarter / year ago, PDF page):
- POLYCAB Q2 FY27 consolidated (filing 574, page 4): revenue 9,898.90 / 8,209.73 / 6,477.21; PBT 1,306.22 / 1,058.22 / 921.00; PAT 990.00 / 796.65 / 692.96. Standalone page 10: 9,839.66 / PAT 965.17 = BSE.
- TCS Q2 FY27 consolidated (filing 686, page 8): revenue 73,188 / 72,275 / 65,799; PBT 18,638 / 17,944 / 16,068; PAT 13,934 / 13,420 / 12,131. Standalone page 17: 60,441 / 14,104 = BSE.
- ASIANPAINT Q2 FY27 consolidated (filing 30, page 12): revenue 9,976.51 / 10,541.94 / 8,531.27; PBT 1,477.78 / 2,095.75 / 1,391.52; PAT 1,085.35 / 1,559.45 / 1,018.23. Standalone page 5: 8,453.83 / 1,095.55 = BSE.
- EBITDA on these cards is derived (PBT + finance costs + depreciation − other income) and marked with *.

Tests: 221 passed (both bases and continuation pages, OCR spaces and decimal commas, unit headers, unit inference only from an exact match, BSE-summary card, labels without spaces, basis explanations, confirmations not warnings).
