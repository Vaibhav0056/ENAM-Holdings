# FINN

FINN is a daily inbox for a portfolio of Indian (NSE/BSE) stocks. It **filters, it doesn't aggregate**.

- **Push: what changed and why should I care.** Corporate announcements from BSE are ranked by how much they matter. Results arrive as a QoQ/YoY table, management changes as person + role + bio, and routine filings are collapsed into a single row. Threshold alerts (volume spikes, big moves, material filing types) land in an Alerts tab and as Web Push notifications.
- **Pull: on-demand research for any NSE ticker.** The hero view is **Promised vs Delivered**: what management guided on earnings calls, matched to what they later reported. Every row opens to the exact quote, document and page. It is grounded only in primary documents (concall transcripts and the annual report).
- **Decision support only.** No buy/sell calls, no trading.

## Run locally (3 commands)

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # macOS/Linux: .venv/bin/pip
cp .env.example .env    # add GEMINI_API_KEY; then: python scripts/gen_vapid.py >> .env
.venv/Scripts/python -m uvicorn api:app --port 8000                     # open http://localhost:8000
```

To start empty instead of from the committed seed, delete `data/finn.db` and run the pipeline:

```bash
python -m finn.ingest && python -m finn.push && python -m finn.alerts   # or Settings → Refresh data
python -m finn.pull POLYCAB                                            # pre-run research for a ticker
python -m pytest -q tests
```

`scripts/start.sh` copies `data/seed.db` into place on first boot, so a fresh checkout or deploy opens with real data.

## Deploy (Render)

1. Push the repo to GitHub and create a **Blueprint** on Render from `render.yaml`. It is one Docker web service with a 1 GB persistent disk at `/var/data`.
2. Set the secret env vars: `GEMINI_API_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` (from `python scripts/gen_vapid.py`), and optionally `TAVILY_API_KEY`.
3. On first boot the seed DB is copied to the disk. Use **Settings → Refresh data** to pull fresh filings. On the free plan (no disk), remove the `disk` block and `FINN_DB`; the app then re-seeds from `data/seed.db` on every boot.

## Notifications: add to Home Screen

- **iPhone / iPad (iOS 16.4+):** Web Push only works for installed web apps. Open FINN in Safari, tap **Share → Add to Home Screen**, open FINN from the home-screen icon, then go to **Settings → Enable notifications**. FINN shows a one-time hint about this on iPhone.
- **Android / desktop Chrome:** tap **Settings → Enable notifications**. Permission is requested only on that tap.
- **Send test notification** confirms delivery. Tapping a notification opens the alert's card. The Alerts tab is the source of truth; a push only points to it.

## How it works

| Stage | Module | Notes |
|---|---|---|
| Ingest | `finn/ingest.py` | yfinance 90d prices (auto-adjusted); `bse` announcements for 30 days per scrip, deduped on a content hash; idempotent |
| Classify | `finn/push.py` | Ordered keyword rules first; only unmatched filings go to the cheap model, in one batched, enum-constrained call |
| Score | `finn/push.py` | `type_weight × magnitude × novelty`; attention ≥ 0.7, worth a look ≥ 0.3, else routine (never deleted) |
| Results | `finn/results.py` | Model reads the P&L table with quote + page; Python normalises to ₹ crore, derives EBITDA, computes QoQ/YoY; a deterministic parser cross-checks |
| Mgmt change | `finn/push.py` | Name, role, in/out, effective date and filing profile (+ Tavily bio when a key is set) |
| Alerts | `finn/alerts.py`, `finn/notify.py` | Rules editable in Settings; dedupe per (rule, ticker, date); pywebpush; 404/410 subscriptions pruned |
| Research | `finn/pull.py` | screener.in Documents → last 4 transcripts + latest annual report → management turns only → guidance/actuals/snapshot → Python matching → one strong-model synthesis over verified JSON |

## Design decisions

- **Code does the maths, LLMs do the reading.** QoQ/YoY, unit conversion, EBITDA derivation, 20-day volume averages, thresholds, scoring and guidance-vs-actual verdicts are all plain Python, covered by `tests/test_core.py`.
- **One LLM gateway.** Every call goes through `llm(task, prompt, schema, tier)` in `finn/llm.py`. It returns Pydantic-validated JSON, caches on sha256(model + prompt + input) in SQLite, and logs tokens per task in `llm_log`. Re-running any stage costs zero model calls.
- **Grounding is enforced, not requested.** Each extracted fact carries a quote + page. Quotes are fuzzy-matched against that page's text (rapidfuzz `partial_ratio ≥ 85`), and failures are dropped. Missing data shows "Not found in sources", never a guess. Synthesis sentences containing a number absent from the evidence JSON are removed, and every bullet must cite known evidence IDs.
- **Filing text is data.** Document text is sent inside `<document>` tags, and the system instruction tells the model to ignore any instructions it contains.
- **Failure isolation.** A bad PDF, ticker or model error marks that item "couldn't process" and the run continues. Push failures never block the pipeline.
- **Novelty groups duplicate filings.** "Financial Results" and "Outcome of Board Meeting" for the same event become one attention card; the twin drops to routine.
- **Honest verdicts.** Vague targets ("healthy growth") are *unverifiable*. Targets whose horizon hasn't ended are *pending*, but show the latest reported figure and whether it is tracking. That tracking is shown only for ratios like %, bps or days, never for amounts like a quarter of capex against a 5-year plan.
- **Free-tier friendly.** Gemini's free tier allows about 20 requests per model per day. Classification and one-liners are batched, and each tier rotates through a model pool when a daily quota is hit. Gemini 2.5 models are retired for new keys, and no Pro-class model is available on a free key, so the defaults are `gemini-3.5-flash` (cheap) and `gemini-3.8-flash` (strong). Override them in `.env`.

## Deliberately left out

- Buy/sell signals, price targets and any trading or broker integration.
- NSE's own announcement feed. BSE covers the same filings for dual-listed stocks, and the `bse` library is stable.
- OCR for scanned PDFs (e.g. Dixon's results). They show "Figures not extracted" instead of a guess.
- Bank-format P&L parsing (interest earned / operating profit). HDFC Bank's results fall back to a one-liner.
- User accounts and multi-portfolio support. One portfolio lives in `data/portfolio.csv`.
- Offline caching in the service worker, since stale financial data is worse than none.
- Historical guidance beyond the last 4 calls. Older promises would need older transcripts.

## Layout

```
api.py               FastAPI app: feed, alerts, rules, push, research jobs, static SPA
finn/                db, llm, ingest, push, results, alerts, notify, pull, pdfs, grounding
static/              index.html, app.js, sw.js, manifest.json, icons/
scripts/             source probes, portfolio validation, VAPID keys, icons, seed export, start.sh
data/portfolio.csv   16 validated NSE stocks (symbol, BSE code, Yahoo symbol, name, sector)
docs/build-log.md    what / decision / issue for every build step
tests/test_core.py   maths, normalisation, scoring, thresholds, grounding, matching
```
