# FINN

**Inbox zero for a long-term investor's portfolio.** FINN answers one question all day: *has anything happened that changes why I own this?*

Built for ENAM's way of investing: Buffett-Munger, 8-10 year holds, judged on business quality and management quality. It covers 20 Indian holdings and 5 research peers, and researches any NSE ticker on demand. Decision support only: FINN never says buy, sell or hold, and never trades.

## What it does

- **Today: a feed you can finish.** Every exchange filing for the holdings is typed, scored and bucketed into *Needs attention*, *Worth a look* and a collapsed *Routine* row. Cards answer first: results as a ₹ crore table with QoQ and YoY (and, on results day, the quarter against management's own guidance), a management change as who is in and who is out, a governance flag with its quote. A pulse grid shows every holding's day and thesis status; a week-ahead strip lists results dates and analyst meets. One tap shows **why a card scored that way** (type × magnitude × novelty × thesis), another the **evidence page**. Swipe right to review, left to snooze, until *You're caught up*.
- **Thesis Guardian.** Each holding has a thesis drafted from management's own claims (labelled as such, editable), with breakers and KPIs Python checks on every verified results card. Each holding shows **Intact / Watch / At risk** with the reason and the filing that triggered it.
- **Management Credibility Ledger.** For any NSE ticker, up to 8 earnings calls and 2 annual reports are read; every forward-looking promise is extracted with its quote and page, matched in Python to what was later reported, and given a verdict: *delivered, partial, missed, pending, unverifiable*. That yields a recency-weighted **credibility score** (or an honest *Not scored*), guidance drift, a business snapshot, bull and bear cases, the strongest counter-argument, and a receipt.
- **Charts from verified data only.** One year of price with the filings that moved it, 8 quarters of results with EBITDA margin, the EBITDA-to-PAT margin bridge, thesis KPIs (or management's own company-wide target) against their threshold, promise hit-rate by quarter, guidance drift, and a chart in each deep dive. Every point opens its source page; every chart has "View as table".
- **Go deeper.** Results history, cash quality, balance sheet, language diff between calls, hedge-meter, dodge detector, analyst worry map, fine print, peer compare.
- **Ask.** A bounded copilot answers from FINN's own documents, cites every sentence, and says *Not in sources* when it can't.
- **Alerts.** One per company per day: material filings, price moves (fixed or beyond 3σ of the stock's own history net of the NIFTY, never on ex-dates), volume ≥ 3× the 20-day average on stocks trading ≥ ₹10 crore a day, new or dropped management promises, peer read-across.
- **Portfolio.** Add a company by NSE symbol (validated, then onboarded in the background with visible progress) or remove one (its history is kept).

## What's novel

1. **A ledger of what management promised versus what it delivered**, built from primary documents with every line one tap from its quote and page, scored over time. Most tools summarise what management *says*; FINN keeps score.
2. **Filtering, not aggregating.** Scoring is explicit and inspectable ("Why am I seeing this?"), and the feed is designed to be finished.
3. **Results read twice.** A deterministic parser and a model each read the statement; BSE's own structured summary referees. When BSE publishes the standalone statement and FINN shows consolidated, FINN reads the standalone statement from the same PDF and shows that it matches BSE to the paisa.
4. **Every number on screen is computed in Python** from verified extractions; models read and write prose, never arithmetic.

## How FINN stays honest

- **Grounding is enforced.** Every extracted fact carries document, page and quote; the quote must match its page (rapidfuzz ≥ 90, ±1 page) and its figures must be printed in it, or the fact is dropped. Generated prose loses any sentence with a number absent from the evidence and any buy/sell/hold or price-target phrasing.
- **It says what it doesn't know.** *Not in sources*, *Unverifiable*, *1 source*, *Not scored* (Polycab's credibility shows "Not scored: fewer than 3 promises have matured", not a number built on one promise). A results card from a scanned PDF shows BSE's own figures, labelled as BSE's, or nothing.
- **It caught its own bugs.** The ledger once reported that Polycab "cut" its capex guidance from "₹1,200-1,600 crore" to "₹12-16 billion": the same amount, compared without converting units. Money targets are now compared in ₹ crore, with a regression test. The results parser had a literal backspace character where a regex word boundary belonged, quietly disabling one signal; a test now rejects control characters in source files.
- **"Verify" means real doubt.** 19 of the 20 holdings' latest results cards are verified (by BSE's figures directly, by the other basis matching BSE, or by parser and model agreeing); Tata Power carries Verify because BSE's standalone figure stays unexplained.
- **Errors are sentences.** A busy model, a refused download or a missing transcript is explained in plain words ("6 of 8 earnings-call transcripts could not be read: the publisher's website refused the download"), never as a stack trace.

## How FINN keeps data fresh

- **Four runs a day**, 08:00, 16:30, 19:00 and 22:00 IST: announcements → prices → events → BSE results summaries → classify, score, enrich, thesis → alerts → push. Triggered by GitHub Actions (`.github/workflows/ingest.yml`), by the app itself when hosted locally (`finn/scheduler.py`), or by cron-job.org through `POST /tasks/ingest` with the header `X-FINN-Task-Token`. A lease lock in Postgres means runs never overlap; every write is idempotent.
- **A chain of sources per kind of data**, each source getting only what the previous one could not deliver: announcements BSE → NSE API → NSE RSS; prices Yahoo Finance → NSE bhavcopy; events BSE → NSE, plus dates read from the filings themselves. A source that fails 3 runs in a row is skipped for 3 hours (circuit breaker). Rows are validated before they are written; nothing is ever deleted; the same disclosure on both exchanges is stored once (the second copy kept as a hidden duplicate).
- **Visible health.** "Data updated Xh ago" in the Today header, amber only after 26 hours on a trading day; Settings lists every source as OK, Failing or Paused; `GET /health/data` has the detail. If every announcement source has failed for 24 hours, the owner gets a push and the GitHub job fails.
- **Research is precomputed** after the 22:00 run (6 reports a night on the free model tier, resuming where it stopped) and cached for 7 days.

## Evaluation (`python -m evals.run`, all pass)

| Suite | Metric | Result | Target |
|---|---|---|---|
| Push, 40 labelled filings | classification accuracy | 95 % | ≥ 90 % |
| | material filings in *Needs attention* | 100 % (4/4) | 100 % |
| Push, 20-filing blind holdout | classification accuracy | 100 % | ≥ 90 % |
| | material filings in *Needs attention* | 100 % (2/2) | 100 % |
| Results, 10 hand-read statements | exact match (90 cells) | 94 % | ≥ 90 % |
| | unit errors | 0 | 0 |
| | scanned PDFs showing no figures read by FINN | 1/1 | all |
| Guidance, Polycab × 4 calls (30 promises) | promise recall | 83 % | ≥ 80 % |
| | verdict agreement | 96 % | ≥ 80 % |
| | unverified citations shown | 0 | 0 |
| Thesis, 30 cards | "affects the thesis?" agreement | 90 % | ≥ 80 % |
| Copilot, 15 questions | cited or "Not in sources" | 15/15 | 15/15 |
| | sentences with unsupported numbers / advice | 0 / 0 | 0 |
| | answered when an answer exists | 8/10 | report |

Plus 239 unit tests (`python tasks.py test`; SQLite and a fake model client, no network) and a golden-path end-to-end check (`python tests/e2e/golden_path.py`, Chrome at 390 px through every screen: no console errors, every interaction under 1 s). Labels marked `claude-provisional` in `evals/` were set by the build agent from the source text and are meant to be corrected by a person; the push holdout was labelled blind and never tuned on.

## Cost and latency

Measured locally (India → Neon Singapore, free Gemini tier), November 2026.

| | |
|---|---|
| Today feed `/digest` | p50 38 ms, p95 46 ms (precomputed windows) |
| Holding page and its charts | ~10 ms from the warm cache; 0.4-0.7 s cold |
| Golden path, slowest interaction | 0.5-0.8 s |
| Chart render in the browser | 0.8 ms (8-quarter bars) to 3.6 ms (250-day price); budget 50 ms |
| Cached research report | ~0.1-0.6 s |
| Cold research report, 5 unseen tickers | 67-102 s (4 of 5 under 90 s; the slow one shows "taking longer than usual") |
| One cold research report | 14-20 model calls, ≈ $0.024-0.032 at list price (the free tier bills $0) |
| A scheduled ingest run | ~105 s (BSE for 25 companies, prices, events, pipeline) |
| Lighthouse (mobile) | not re-measured after this UI pass; steps in `docs/build-log.md`, step 7 |

## Decisions and trade-offs

- **Neon Postgres** (relational + JSONB + full-text + pgvector in one place) on a dev branch locally, the same engine as production.
- **Parser first, model second, BSE as referee.** A deterministic read that BSE confirms costs nothing; a figure off by an exact power of ten is rejected, never shown.
- **Strong tier falls back fast.** A model that hits its deadline or answers "busy" is skipped at once for the cheap model, and the receipt says so (this took a report rebuild from 503 s to 86 s).
- **Free tier as a design constraint.** Research is precomputed nightly and cached for a week; deep dives degrade to a stated reason when the day's quota is gone.
- **Plain CSS on design tokens, Alpine served from FINN, no build step.** The Tailwind CDN compiles CSS in the browser and fails the mobile performance budget.
- **Polite to public sources.** FINN identifies itself honestly, spaces requests, backs off on 403/429 and never disguises itself to get past a refusal.

## Left out on purpose

- Trade execution, price targets and buy/sell/hold language: decision support only.
- Anything not verified end to end ships **off** behind a feature flag (`finn/flags.py`, `FINN_FLAGS`): the **audio brief** (depends on each device's speech engine) and **Web Push** (needs an installed PWA on a real phone). Alerts still appear in the Alerts tab.
- OCR for scanned filings: BSE's own figures are shown instead, labelled.

## Next steps

1. Verify Web Push and the audio brief on real phones, then turn their flags on.
2. Run Lighthouse in Chrome DevTools (mobile) on Today, Holding and Research, and fix what it reports.
3. Bank-format P&L parsing (HDFC Bank shows BSE's figures today) and XBRL results parsing.
4. Finish precomputing research for all holdings and the large caps (the nightly job does 6 a night), and language diffs for DIXON and CAMS when the embedding quota resets.
5. Run the GitHub probe workflow from a cloud runner to see whether cloud IPs are blocked by any source.
6. Human-reviewed gold sets for every evaluation.

## Run it locally (Windows-friendly)

```powershell
python -m venv .venv; .venv\Scripts\activate; pip install -r requirements.txt
copy .env.example .env      # then fill in DATABASE_URL, DATABASE_URL_UNPOOLED (Neon) and GEMINI_API_KEY
python tasks.py setup; python tasks.py ingest; python tasks.py pipeline; python tasks.py serve
```

Open http://localhost:8000 (Chrome device mode, or a phone on the same Wi-Fi via the PC's LAN address). While it runs, FINN refreshes itself at the four IST slots (`FINN_SCHEDULER=0` turns that off).

| Command | What it does |
|---|---|
| `python tasks.py setup` | migrations on Neon, then the idempotent seed (25 companies, alert rules) |
| `python -m finn.ingest` | one scheduled run (`--backfill 90`, `--backfill-results`, `--probe` for the source check) |
| `python -m finn.precompute` | research reports in priority order (`--showcase` for showcase deep dives) |
| `python -m finn.research.engine KEI` | research any NSE ticker from the command line |
| `python tasks.py test` | unit tests |
| `python -m evals.run` | every evaluation above |
| `python tests/e2e/golden_path.py --runs 5` | the golden path (needs a running server and Chrome) |

## Deploy

1. In Neon, create a production branch from dev (or run `python tasks.py setup` on an empty branch).
2. Push the repo to GitHub. On Render, create a Blueprint from `render.yaml`: native Python runtime, Singapore, `pip install -r requirements.txt`, then `scripts/start.sh` (migrate, seed, `uvicorn` on `$PORT`), health check `/health` (no database). Set the `sync: false` secrets: `DATABASE_URL`, `DATABASE_URL_UNPOOLED`, `GEMINI_API_KEY`, `FINN_TASK_TOKEN` (and optionally `TAVILY_API_KEY`, the VAPID keys and `VAPID_SUBJECT`).
3. In the GitHub repository secrets set the same values plus `FINN_URL`. The ingest workflow then runs the four daily slots, precomputes research nightly and keeps the free instance awake; the probes workflow (manual) checks every data source from a cloud runner.
4. Optional: point a cron-job.org job at `POST {FINN_URL}/tasks/ingest` with the header `X-FINN-Task-Token`.
5. If the database is ever unreachable, the main screens are served from `data/demo_snapshot.json` (`DEMO_SNAPSHOT=1` forces it).

The `Dockerfile` is an optional alternative; `docker build` was not run (Docker is not installed on the build machine).

## Credits

The UI patterns (bottom sheets, tabs, dropdown, context menu, toasts, sliders, switches, badges, tables, steppers, timelines, skeletons, empty states) were adapted from three MIT-licensed kits and restyled with FINN's own tokens: **[Pines UI](https://github.com/thedevdojo/pines)** by DevDojo, **[HyperUI](https://github.com/markmead/hyperui)** by Mark Mead, and **[TailAdmin](https://github.com/TailAdmin/tailadmin-free-tailwind-dashboard-template)**. Their licences are in `vendor/ui-kit/licenses/`. Alpine.js (MIT) is served from `static/vendor/`; icons follow Lucide's style (ISC).

## Layout

```
api.py                 FastAPI app: routes, timing, DEMO_SNAPSHOT fallback, health, static PWA
finn/ingest.py         one scheduled run (lock, chains, pipeline, alarm); --probe, --backfill
finn/sources/          BSE, NSE, prices, PDF store, BSE summaries, events, source chains + breakers
finn/feed/             classify, score, pipeline, digest (+ warmer)
finn/enrich/           results (parser + model + BSE referee), management, governance
finn/research/         transcripts, ledger maths, engine
finn/thesis/           KPI maths, guardian
finn/deep/             deep-dive maths and recipes
finn/alerting/         alert rules, one-per-company-per-day consolidation, Web Push
finn/charts.py         chart specs computed from verified data
finn/portfolio.py      add / remove holdings with background onboarding
finn/precompute.py     nightly research precompute
finn/flags.py          feature flags
finn/llm/              router (fallbacks, cache, budget) + versioned prompts
static/                PWA: index.html, app.js, charts.js, app.css, sw.js, vendor/alpine
evals/                 labelled sets and scorers
tests/                 unit tests, fixtures, e2e golden path
docs/                  build log, UI brief and quality checklist, UI screenshots
```
