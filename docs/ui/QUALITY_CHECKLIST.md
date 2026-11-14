# FINN UI quality checklist (anti-"AI slop")

Run this on every screen before calling it done.

## Content
- [ ] Real, verified data only (real filings, tickers and numbers checked against the source PDF). No lorem ipsum or sample names in the final build.
- [ ] Headlines judge, not describe ("PAT +18%: clears your 16% margin bar", not "Q2 results announced").
- [ ] No marketing words: seamless, powerful, unlock, revolutionary, AI-powered, insights ✨, supercharge.
- [ ] Missing data says "Not in sources" / "Unverifiable" / "1 source", never a guess.
- [ ] No buy/sell/hold or target-price language anywhere.

## Visual
- [ ] Numbers: tabular figures, right-aligned, ₹ crore, 1 decimal for %.
- [ ] Colour only for meaning: green/red = deltas, amber = Watch/low confidence, accent = links/active. Nothing decorative.
- [ ] No gradients, glow, glassmorphism, emoji, sparkle icons, stock illustrations or hero sections.
- [ ] One spacing scale (8pt, 4 for tight), one radius, one type scale, applied everywhere.
- [ ] 1px hairline borders, no shadows.
- [ ] Contrast ≥ 4.5:1, and status is never shown by colour alone.

## FINN signatures (at least one per screen)
- [ ] Pulse grid with thesis-status edges · verdict chips · evidence sheet (quote + page) · report receipt · "Why am I seeing this?" · "You're caught up".
- [ ] Swap-the-logo test: a screenshot is still obviously FINN.

## States
- [ ] Loading (skeleton matching the layout), empty, error with retry, stale data, server waking, low confidence: all designed.

## Interaction
- [ ] Depth on request: headline → summary → Go deeper → evidence. No walls of text on cards (≤ 2 lines).
- [ ] The copilot opens from context. Chat is not the home screen.
- [ ] Tap targets ≥ 44px, primary actions in the bottom 40% of the screen.

## The 5-question test
1. Could this screen belong to any other app?
2. Is there a word that sounds like marketing?
3. Is there a colour that means nothing?
4. Is every number verified?
5. Would a busy portfolio manager get it in 3 seconds?
