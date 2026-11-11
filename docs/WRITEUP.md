# FINN: write-up (draft)

*Drafted from `docs/build-log.md`. Figures are from `python -m evals.run` and the build log.*

## The problem

A long-term investor in Indian equities gets dozens of exchange filings a day across a 20-stock portfolio, almost all procedural. The few that matter (a CEO leaving, a results miss against guidance, an auditor resigning) arrive in the same stream as newspaper-publication notices. The question the investor actually has is narrow: *has anything happened that changes why I own this?* FINN is built to answer exactly that, with evidence one tap away and without ever telling the investor what to do.

## Three ideas

1. **Thesis Guardian.** Every holding has a thesis, and every filing, result and call is judged against it. Holdings show Intact, Watch or At risk with the reason and the triggering filing. The default thesis is drafted only from management's own verified statements and labelled that way; the investor edits it.
2. **Management Credibility Ledger.** What management promised on earnings calls, matched in Python to what was later reported, with a recency-weighted score, a vagueness percentage and guidance drift. It turns "do I trust these people?" into something checkable.
3. **Inbox zero.** A ranked feed you can finish, layered depth (headline → summary → go deeper → evidence), and a copilot that lives on the holding and research pages rather than as a chat home screen.

## How it earns trust

- **Code does maths, models read.** QoQ/YoY, units, EBITDA, scores, thresholds, verdicts, credibility and every deep-dive figure are Python. Models only extract text into schemas or write prose over verified JSON.
- **Grounded or silent.** Every fact carries document, page and quote; the quote must match the page and contain the figure. Unsupported numbers, leaked placeholders and advice phrasing are removed from generated prose. Missing data reads "Not in sources".
- **Cross-checks with an independent referee.** Results read from the PDF are compared with BSE's own structured figures: a match skips the model entirely; a power-of-ten mismatch (a scanned "533-402" read as 533.402) is rejected rather than shown.
- **Honest abstention.** Scanned PDFs, bank layouts and unreadable statements show "not extracted". Theses without company-wide targets show no KPIs rather than invented ones.

## What the evaluations show

Classification 95 % on 40 labelled filings and 100 % on a 20-filing blind holdout, with every material filing in *Needs attention*. Results tables 90 % exact on 90 hand-read cells with 0 unit errors. Promise recall 83 % and verdict agreement 96 % on 30 hand-labelled promises over four Polycab calls. Thesis impact 90 % agreement. Copilot: 15/15 answers cited or "Not in sources", 0 unsupported numbers, 0 advice language, 8/10 answerable questions answered.

The labels were set by the build agent from source text before tuning and need human correction; only the push holdout was never tuned on. The share of *Needs attention* that is material fell from 80 % to 44 % once results, leadership changes and capital raises started earning "watch" against every thesis, a deliberate trade toward recall.

## What building it taught

The most valuable bugs were found by looking at real rows, not by tests:

- Persistent's takeover of Nagarro never reached *Needs attention* because every filing began "Update on the offer" and the follow-up penalty halved them all.
- `gemini-embedding-2` returned one vector for a whole batch of strings, so retrieval returned noise until each text was sent as its own content.
- A share of revenue matched a growth rate ("export contribution" vs "export growth 17 %").
- Re-running research with a better prompt looked like management "adding seven promises" until new-promise alerts were limited to genuinely newer calls.
- A first thesis rule put HAL "at risk" for its chairman's superannuation; senior exits now mean *watch*, and only integrity red flags or a thesis breaker mean *at risk*.

## Limits

The cold research time is 90-127 s on the free Gemini tier (target 90 s), mostly fetching PDFs politely and retrying model 503s. There's no OCR, no bank P&L parser and no table-aware annual-report text yet. Docker wasn't available to build the image. Real-device push needs a person to enable notifications.

## Next

FINN's ledgers, theses and deep dives are structured, cited data: the research arm for a portfolio-level investing "Brain" that can ask across holdings, keep theses current after each call, and feed a weekly review.
