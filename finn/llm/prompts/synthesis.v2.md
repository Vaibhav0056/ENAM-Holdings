You are given ONLY verified evidence about {{company}} as JSON: management's promises with Python-computed verdicts and a credibility score, reported actuals, guidance drift between calls, and a business snapshot. Every item has an id.

Write, for a long-term investor who judges management quality:
- credibility: up to 4 bullets on how reliably this management does what it says (use the verdicts, the score, the drift and the vagueness).
- bull: exactly 3 bullets, the strongest case that the business and its management are on track.
- bear: exactly 3 bullets, the strongest case that they are not.

The credibility score runs from 0 to 100 and is computed only over promises whose deadline has passed (`measured`); `total` counts every promise including pending ones. Never present one number as a fraction of another unless the evidence states that ratio.

Rules: each bullet at most 30 words; cite the ids it rests on in `evidence`; use only numbers that appear in the evidence; when evidence is thin, say so rather than filling the gap. Never recommend buying, selling or holding, and never give a price view.

Evidence:
{{evidence}}
