# FINN

**Investment intelligence for a long-term portfolio of Indian stocks.** FINN reads every exchange filing, earnings call and annual report for your holdings and answers one question: *has anything happened that changes why I own this?*

## Features

### Today
- A daily digest you can finish: every filing is sorted into **Needs attention**, **Worth a look** and **Routine**.
- Results arrive as a clean ₹ crore table with quarter-on-quarter and year-on-year change.
- Management changes show who joined and who left; governance flags show the exact quote.
- A **portfolio pulse** grid shows each holding's day move and thesis status at a glance.
- A **week-ahead** strip lists upcoming results dates and analyst meets.
- Swipe right to mark reviewed, left to snooze.

### Thesis Guardian
- Every holding has an investment thesis, drafted from management's own claims and fully editable.
- Key numbers to watch (revenue growth, margins, capex) are checked against each new results filing.
- Each holding shows **Intact**, **Watch** or **At risk**, with the filing that caused it.

### Research: Promise vs Performance
- Search any NSE ticker to get a full research report.
- FINN reads up to 8 earnings calls and 2 annual reports, extracts every promise management made, and checks it against what was later reported: **Delivered**, **Partial**, **Missed**, **Pending** or **Unverifiable**.
- Produces a **management credibility score**, guidance drift, a business snapshot, bull and bear cases, and the strongest counter-argument.

### Deep dives
Results history, cash quality, balance sheet, management language changes, hedge-meter, dodge detector, analyst worry map, fine print, and peer comparison.

### Charts
- One-year price with the filings that moved it.
- 8 quarters of results with EBITDA margin, and the margin bridge.
- Promise hit-rate and guidance drift.
- Tap any point to open its source page, or switch any chart to a table.

### FINN Copilot
Ask questions about any company. Answers come only from filings, transcripts and reports, every sentence is cited, and FINN says *Not in sources* when it can't answer.

### Alerts
- One alert per company per day: material filings, unusual price moves, volume spikes, and peer read-across.
- Rules are adjustable in Settings.
- Optional push notifications.

### Portfolio
- Add a company by NSE symbol. FINN validates it and builds its data in the background with visible progress.
- Remove a company and its history is kept.

### Always fresh
- Data refreshes four times a day (08:00, 16:30, 19:00, 22:00 IST).
- Each kind of data has backup sources, so one outage doesn't stop the feed.
- The header shows when data was last updated.

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11, FastAPI, Uvicorn |
| Database | Neon Postgres, SQLAlchemy 2, Alembic, pgvector (semantic search), Postgres full-text search |
| AI | Google Gemini (extraction, synthesis, copilot, embeddings) via `google-genai`, Pydantic-validated JSON |
| Documents | pdfplumber, rapidfuzz (quote verification) |
| Market data | BSE and NSE announcements, Yahoo Finance (`yfinance`), NSE bhavcopy |
| Frontend | Installable PWA: Alpine.js, plain CSS, inline SVG charts, service worker, no build step |
| Notifications | Web Push (`pywebpush`, VAPID) |
| Hosting | Render (web service), GitHub Actions (scheduled data refresh) |
| Testing | pytest, Chrome DevTools Protocol end-to-end test |

UI patterns adapted from [Pines UI](https://github.com/thedevdojo/pines), [HyperUI](https://github.com/markmead/hyperui) and [TailAdmin](https://github.com/TailAdmin/tailadmin-free-tailwind-dashboard-template) (MIT; licences in `vendor/ui-kit/licenses/`).
