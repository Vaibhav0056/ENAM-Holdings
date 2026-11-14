Build the FINN frontend from this UI brief. Read it fully before writing any code. Build it with mock data first (the JSON in §9), so the UI is finished and polished independently of the backend. Then wire it to the API.

## 1. Product in one line
FINN is **inbox zero for a long-term investor's portfolio**. It answers one question all day: *"Has anything happened that changes why I own this?"* It filters, it does not aggregate. It is decision support only: no buy/sell calls.

Users: a small, high-conviction value-investing team (8–10 year holds) checking FINN on a phone between meetings. They're time-poor, numerate and sceptical. They trust evidence, not adjectives.

## 2. Design principles
1. **Answer first.** Every card's headline says why it matters ("PAT +18% YoY: supports margin thesis"), not what the document is called.
2. **Density with calm.** As information-dense as a terminal, as quiet as a well-set newspaper. White space separates groups, not items.
3. **Evidence one tap away.** Every number and claim can open its source quote, document and page.
4. **Colour means something.** Green/red only for good/bad deltas. Amber only for "Watch". Nothing is decorative.
5. **Finishable.** The feed can be cleared. "You're caught up" is a designed state, not an empty one.
6. **Thumb-first.** Primary actions sit in the bottom 40% of the screen. Swipe gestures on cards. Tap targets ≥ 44px.

## 3. Visual system
**Chosen style: teal** (`docs/ui/reference-teal.html`): calm dark slate, Inter + IBM Plex Mono, radius 14. The tokens below are the source of truth and are implemented once, as CSS custom properties on `:root` in `static/app.css`.

**Theme:** dark by default, with a light theme via `prefers-color-scheme`, overridable in Settings (or `?theme=light|dark` for QA).

| Token | Dark | Light | Use |
|---|---|---|---|
| bg | #0B0E13 | #F7F8FA | App background |
| surface | #12161D | #FFFFFF | Cards, sheets |
| surface-2 | #1A2029 | #F0F2F5 | Inner modules, inputs |
| border | #242B36 | #DDE1E7 | 1px hairlines only |
| text | #E8ECF1 | #14181F | Primary text |
| text-2 | #9AA4B2 | #5B6573 | Secondary text, labels |
| accent | #2DD4BF (teal) | #0F9F8C | Links, active tab, primary buttons |
| up | #22C55E | #15803D | Positive deltas, Intact |
| down | #F43F5E | #BE123C | Negative deltas, At risk |
| watch | #F59E0B | #B45309 | Watch status, low-confidence badges |

**Type:** Inter for UI text, and IBM Plex Mono (or Inter with `font-variant-numeric: tabular-nums`) for every number.
- **Sizes:** 28/34 bold for screen titles, 17/24 semibold for card headlines, 15/22 regular for body text, 13/18 medium for labels and chips, 11/14 for UPPERCASE eyebrows (letter-spacing 0.06em).
- **Numbers:** always right-aligned in tables, with ₹ crore shown as "₹1,234 cr" and percentages to 1 decimal place.

**Spacing:** an 8pt grid (4 for tight gaps), 16px screen padding, 12px card padding, 12px between cards.
**Shape:** card radius 14, chip radius 999, sheet radius 20 at the top.
**Elevation:** none. Separate things with surface colour and 1px borders only. No shadows or gradients.
**Icons:** Lucide, 18px, 1.75 stroke.
**Motion:** 150–200ms ease-out. Cards slide on swipe with a colour reveal (teal = reviewed, slate = snooze). Bottom sheets spring up. Respect `prefers-reduced-motion`.

## 4. App frame
- **Bottom tab bar** (fixed, safe-area aware): **Today · Holdings · Research · Alerts** (unread badge). Settings sits behind an avatar icon at the top-right of Today.
- **Copilot bar:** a slim pill above the tab bar on Holding and Research screens that reads "Ask about INFY…". Tapping it opens the Copilot sheet with that context already loaded.
- **Routing:** hash routes (`#today`, `#holding-INFY`, `#research-INFY`, `#alerts`, `#settings`) so notification deep links work.

## 5. Screens

### 5.1 Today (home)
1. **Header:** "Today", the date, and a **window picker** chip (▾ → Since I last checked · 24h · 3d · 7d · 30d · Custom range in a bottom-sheet date picker, IST, ≤ 90 days). The header shows the active window, e.g. "Since Fri 6:00 pm · 23 filings".
2. **Summary line:** "3 need attention · 2 alerts · 14 routine hidden" (each part tappable to filter).
3. **Portfolio pulse grid:** 16 compact tiles (4 × 4) showing the ticker, day % change (tabular), a status-coloured left edge (Intact/Watch/At risk) and an unread dot. Tapping a tile opens that Holding.
4. **Section "Needs attention"** → cards. **Section "Worth a look"** → cards.
5. **Routine row:** "14 routine filings" with a chevron that expands to a compact list (ticker · type · one line · time).
6. **Caught-up state:** when every card is reviewed, show a calm checkmark, "You're caught up", and the next event ("Next: TCS results · Thu 13 Nov").

### 5.2 Card anatomy (shared by every card type)
```
[TICKER] [type chip] [thesis-impact chip?]          2h ago
Headline: what happened + why it matters (≤ 2 lines)
┌ type-specific module ┐
└──────────────────────┘
Evidence · Open filing · Dig deeper →
```
- **Thesis-impact chip:** "Supports thesis" (green), "Watch" (amber), "Threatens thesis" (red), or nothing.
- **Confidence badge:** only when it's low ("Verify: extraction uncertain").
- **Gestures:** swipe right = Reviewed, swipe left = Snooze (until tomorrow). Long-press opens a menu: Mute this type for this stock, Not useful, Should be higher.

**Type-specific modules:**
- **Results:** a 4-row table with columns `Metric | Q2 FY26 | QoQ | YoY`, rows Revenue, EBITDA, PBT, PAT. The thesis metric row is highlighted with an accent left bar. A footer reads "Consolidated · ₹ crore". Deltas show ▲/▼ and colour.
- **Management change:** two rows, **IN** and **OUT**. Each has an initials avatar, name, role, a one-line background ("ex-CFO, Hindustan Unilever · 22 yrs"), and the effective date. A "Bio source" link sits under the incoming person.
- **Governance flag:** a red eyebrow "GOVERNANCE", a plain-language line ("Statutory auditor resigned mid-term"), and the reason quoted from the filing.
- **Price/volume alert:** a mini sparkline (20 days) with the threshold line and "Volume 2.7× 20-day avg", plus the day's % change.
- **Pre-results:** "Reports Thu 13 Nov", "Last promised: revenue growth 12–14% (Q1 call, p.6)", "Watch: EBITDA margin vs 18% thesis".
- **Generic:** the headline, the one-liner and the filing link only.

### 5.3 Holding page (`#holding-TICKER`)
- **Header:** the name, ticker, price, day %, and a 1Y sparkline.
- **Thesis block:** the status pill (Intact/Watch/At risk), the thesis sentence, the numbers to watch with current vs threshold (as a small progress bar for each), and "What would break it". There's an Edit button, and a "Draft from filings" button for new holdings that's labelled *Draft from management's own claims*.
- **Status timeline:** a vertical list of events that changed the status, with dates.
- **Credibility gauge:** e.g. "Delivered 5 of 7" as a semicircle with a mini bar per quarter. Tapping it opens Research.
- **Recent triggers:** the last 5 cards in compact form.
- **Primary button:** "Run research".

### 5.4 Research (`#research-TICKER`), the hero screen
- **Search:** "Any NSE ticker", with recent searches as chips.
- **Running state:** a vertical stepper with live stages (Finding documents → Reading transcripts → Extracting guidance → Matching to results → Writing verdict → Verifying citations), each with a check or spinner and the elapsed time. Partial results appear as they're ready.
- **Report layout:**
  1. **Credibility score:** a large number (e.g. **71%**) with the label "promises delivered (5/7 measurable)", a trend across 4 quarters, and a one-line read.
  2. **Promise ledger:** one row per promise: `Quarter said | Promise | Actual | Verdict chip` (Delivered green / Partial amber / Missed red / Pending grey / Unverifiable outlined). Tapping a row expands it to the exact quote, speaker and page for both the promise and the actual.
  3. **Guidance drift:** a small step chart showing how one key target moved across quarters.
  4. **Business snapshot:** segment revenue-mix bar (horizontal stacked), with 3 lines on what the company does.
  5. **Bull | Bear:** two columns, or a segmented control on narrow screens, with 3 points each. Every point has superscript citation numbers.
  6. **Footer:** "Sources: 4 transcripts, FY25 annual report", the generated time, and the cost of the report (small text).
- **Citations:** tapping any superscript opens a **bottom sheet** with the document name, page, highlighted quote, and an "Open PDF at page" link.
- **Empty or partial:** "No concall transcripts found for this company. Showing annual-report sections only."

### 5.5 Alerts
- A reverse-chronological list grouped by day, each item with an unread dot, icon by kind, ticker, one line and time. Tapping one opens its card.
- **Top rules strip:** an editable chip row: "Volume ≥ 2.0× ▾", "Price ±5% ▾", "Types: Results, Mgmt, M&A, Governance ▾".
- **Notification banner** (if push isn't enabled): "Get alerts on your lock screen · Enable". On iPhone, this shows the "Add to Home Screen" hint instead.

### 5.6 Copilot (bottom sheet, not a page)
- Opens from the Copilot bar with a context chip ("INFY · Q2 results card").
- **Suggested question chips:**
  - "Why did margins move?"
  - "What did they promise on capex?"
  - "Has this CFO changed before?"
  - "Which holdings are At risk and why?"
- **Answers render as structured blocks:** a short answer (≤ 3 lines), an evidence list (quote · doc · page), and a confidence badge. When the answer isn't in the documents it says "Not in sources", designed as a normal state, not an error.
- The keyboard-safe input stays pinned at the bottom.

### 5.7 Settings
Portfolio list (add/remove ticker), theses, alert thresholds, filing types, notifications (Enable + "Send test"), theme, "Refresh data", and an About line: "Decision support only. Not investment advice."

## 6. States (design every one)
- **Loading:** skeleton cards that match the real layout. No spinners on full screens.
- **Server waking** (cold start): "Waking FINN up… (~30s)" with a progress shimmer.
- **Stale data:** a small amber "Last updated 18h ago" pill in the header.
- **Error:** an inline card saying what failed and offering "Retry". Never a blank screen.
- **Empty:** a designed message for each screen (e.g. Alerts: "No alerts in this window. Your thresholds: Volume 2×, Price ±5%").
- **Low confidence:** an amber "Verify" badge plus the reason in the evidence sheet.

## 7. Accessibility and quality bar
- Contrast ≥ 4.5:1 for text. Status is never shown by colour alone (each has an icon or label too).
- Everything works at 360–430px width, and up to tablet width with a centred 560px column.
- Respect the safe-area insets. Lighthouse Accessibility ≥ 95, and the PWA is installable.
- Test with real filings: long company names, 3-digit % changes and missing values must not break the layout.

## 8. Don'ts (anti-"AI slop")
No purple or blue gradients, glassmorphism, sparkle icons, "AI-powered insights ✨" labels, emoji in the UI, paragraphs longer than 2 lines on cards, a chat UI as the home screen, centred hero text, decorative illustrations, or "Buy/Sell" language anywhere.

## 9. Mock data contracts (build the UI against these first)
```json
{
  "digest": {
    "window": "24h",
    "counts": {"attention": 3, "worth_a_look": 4, "routine": 14, "alerts": 2},
    "items": [
      {"id": "f1", "ticker": "INFY", "type": "results", "bucket": "attention",
       "headline": "PAT +18% YoY; EBITDA margin 24.1% clears 22% thesis bar",
       "thesis_impact": "supports", "confidence": "high", "filed_at": "2026-11-10T16:42:00+05:30",
       "source": "BSE", "filing_url": "https://...",
       "results": {"period": "Q2 FY26", "basis": "consolidated", "unit": "₹ cr",
         "rows": [
           {"metric": "Revenue", "value": 42500, "qoq": 3.2, "yoy": 8.1},
           {"metric": "EBITDA", "value": 10240, "qoq": 4.0, "yoy": 12.5, "thesis_metric": true},
           {"metric": "PBT", "value": 9100, "qoq": 2.1, "yoy": 15.0},
           {"metric": "PAT", "value": 6800, "qoq": 1.9, "yoy": 18.0}]}},
      {"id": "f2", "ticker": "POLYCAB", "type": "mgmt_change", "bucket": "attention",
       "headline": "New CFO from Havells: finance continuity looks solid",
       "thesis_impact": "watch",
       "mgmt": {"in": {"name": "A. Sharma", "role": "CFO", "background": "ex-Finance Head, Havells · 18 yrs", "bio_source": "https://..."},
                "out": {"name": "R. Mehta", "role": "CFO"}, "effective": "2026-12-01"}},
      {"id": "f3", "ticker": "DIXON", "type": "governance", "bucket": "attention",
       "headline": "Promoter pledge rose to 9% of holding", "thesis_impact": "threatens",
       "quote": "...", "page": 2}
    ],
    "routine": [{"ticker": "TCS", "type": "routine_compliance", "line": "Trading window closure", "filed_at": "..."}]
  },
  "holding": {"ticker": "INFY", "status": "intact",
    "thesis": "Margin resilience via deal mix and automation",
    "kpis": [{"name": "EBITDA margin", "current": 24.1, "threshold": 22, "unit": "%", "direction": "above"}],
    "breakers": "Two consecutive quarters of margin < 21% or CEO exit",
    "credibility": {"delivered": 5, "measurable": 7, "by_quarter": [0.5, 0.67, 0.75, 0.71]}},
  "research": {"ticker": "INFY", "credibility_pct": 71,
    "ledger": [{"quarter_said": "Q3 FY25", "promise": "FY25 revenue growth 3.75–4.5% cc",
                "actual": "4.2% cc", "verdict": "delivered",
                "promise_cite": {"doc": "Q3 FY25 transcript", "page": 5, "quote": "..."},
                "actual_cite": {"doc": "Q4 FY25 transcript", "page": 3, "quote": "..."}}],
    "bull": [{"text": "...", "cites": [1, 3]}], "bear": [{"text": "...", "cites": [2]}],
    "sources": ["Q3 FY25 transcript", "Q4 FY25 transcript", "Q1 FY26 transcript", "Q2 FY26 transcript", "FY25 Annual Report"],
    "cost_usd": 0.004}
}
```
Note: these numbers are placeholders for layout only. The real UI must render data from the API.

## 10. Implementation notes
- Stack: plain HTML + Alpine.js, served from /static, with no build step. **Deviation (Phase 13 UI pass):** the tokens and the Pines / HyperUI / TailAdmin patterns are written as plain CSS custom properties and component classes in `static/app.css` instead of Tailwind: the Tailwind Play CDN compiles in the browser (≈ 300 KB of JS on every load, which fails the Lighthouse Performance ≥ 90 bar on a mid phone), and the standalone CLI would add a build step. One `index.html`, `app.js` (store + hash router), `components/*.js` if helpful, and `sw.js` + `manifest.json` for the PWA.
- Fonts from Google Fonts (Inter, IBM Plex Mono), loaded non-blocking with system fallbacks. Icons: Lucide-style paths inlined as one SVG sprite (18px, 1.75 stroke), no icon CDN.
- Charts: small inline SVG for sparklines, the gauge and the drift step chart, so no chart library is needed.
- Build a `#gallery` route (in the app itself, Settings → Component gallery) that shows every card type and state with the mock data, for fast visual QA.

## 11. Additional features (all required)
- **Layering everywhere:** every section and card follows headline → summary (tap) → **"Go deeper ▸"** (generated on demand with a mini progress line, then cached) → evidence sheet. Use one consistent "Go deeper" component.
- **Week-ahead strip** on Today, under the pulse grid: horizontal chips like "TCS results · Thu", "HAL AGM · Fri". Tapping one opens the pre-results card.
- **Results-day verdict** in the Results card: a top line "Promised 12–14% → delivered 11.2% · **Miss** · 3rd miss in 4 quarters", with a verdict chip and the cited promise.
- **New-promise card:** "3 new promises logged · 1 earlier promise dropped" with a mini list. **Peer read-across card:** "HAVELLS: copper-cost squeeze · relevant to POLYCAB margin thesis" with a peer chip.
- **"Why am I seeing this?"** on the card long-press menu and an (i) icon. A sheet shows the score breakdown as small horizontal bars (type × magnitude × novelty × thesis) plus the bucket thresholds.
- **Research additions:** the credibility block shows "measured 7 of 11" and **vagueness %** as a secondary stat. Bull | Bear is followed by a **"Strongest counter-argument"** (devil's advocate) block. The report footer is a **receipt**: "12 sources · 41 s · $0.004 · 0 unverified claims".
- **Deep-dive screens** share one layout (title, a 1-line finding, a compact chart or table, evidence list):
  - **Cash quality:** OCF/PAT bars by year.
  - **Balance sheet:** net debt line + working-capital days.
  - **Language diff:** two columns, removed phrases struck through in red-muted, added in green-muted.
  - **Dodge detector:** a list of Q→A pairs with an "deflected" tag.
  - **Hedge-meter:** a sparkline per quarter.
  - **Worry map:** a topic × quarter heat grid.
  - **Fine-print:** a year-over-year diff list.
  - **Peer compare:** two ledgers side by side.
- **Time-travel replay:** a "Replay" control in the Today header opens a date slider. The whole feed re-renders "as of" that date with a persistent amber banner ("Viewing 12 Aug 2026 · Exit replay").
- **Audio brief:** a "▶ 60-sec brief" pill at the top of Today, using the Web Speech API. It shows a progress bar and the line being read, and can be paused.
- **Decision journal:** a card menu action "Acting on this" with a one-line reason input. The journal appears on the Holding page timeline, and later events show "You noted: …" beside them.
- **Evidence strength:** claims backed by 2+ sources render in the full text colour; single-source claims render in text-2 with a "1 source" micro-label.
- **Feedback:** "Useful / Not useful / Should be higher" in the card menu, with a toast "FINN will weigh this type differently for you".

## 12. Done when
- Every screen in §5 and every state in §6 renders with the mock data at 375px, in both dark and light themes.
- The gallery page shows all card variants side by side.
- Swipe, the bottom sheets, the window picker, the hash routing and the deep links all work on a real phone.
- Nothing in §8 appears anywhere.
