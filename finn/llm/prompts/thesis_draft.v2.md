Draft an investment thesis for {{company}} for a long-term (8-10 year), quality-focused investor, built ONLY from management's own statements in the evidence below (strategy, business description, segments, risks they name, and promises they made). Every item has an id. This is labelled "Draft from management's claims": do not add outside knowledge, opinions or price views, and never advise buying, selling or holding.

Write ids only in the `evidence` lists, never inside the text.

Return:
- statement: 2-3 sentences: what the business must keep doing for the investment case to hold, in management's terms. Cite ids.
- kpis: 2-4 measurable checks on the WHOLE company, each with metric from this list only: revenue_growth_yoy, ebitda_margin, pat_growth_yoy, pat_margin, and at most one KPI per metric. op is ">=" or "<=". threshold is a number in %: take it from management's own company-wide target (the low end of a guided range). Never use a segment's target for a company KPI, and never set the bar at a recent peak; if management gave no company-wide target for a metric, leave that metric out. Cite the ids. why: one line.
- breakers: 2-3 events that would break the thesis (for example the loss of a stated competitive advantage, a governance failure, a strategy U-turn), each one line, each citing ids.

Evidence:
{{evidence}}
