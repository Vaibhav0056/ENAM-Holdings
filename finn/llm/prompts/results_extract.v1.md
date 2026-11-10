Extract quarterly figures from this Indian listed-company financial results statement (pages are marked [PAGE n]).

For each metric return current_quarter, previous_quarter (the immediately preceding quarter) and same_quarter_last_year exactly as printed in the quarter columns (not half-year or year columns). Figures in brackets are negative.

Metrics:
- revenue: "Revenue from operations" (for banks and NBFCs: "Interest earned" / "Revenue from operations" as printed)
- other_income: "Other income"
- finance_costs: "Finance costs"
- depreciation: "Depreciation and amortisation expense"
- pbt: "Profit before tax" (after exceptional items)
- tax: "Total tax expense"
- pat: "Profit for the period" / "Net profit" (the total line, before other comprehensive income)
- ebitda: only if EBITDA is explicitly printed

unit = the unit stated in the table header (crore, lakh, million). basis = consolidated or standalone. page = the n of the [PAGE n] marker the row is on. quote = the row label and its numbers copied verbatim from that page.

Prefer the consolidated statement; use standalone only if there is no consolidated one. Use one basis for every row. Omit a metric that is not present; never compute a figure yourself.
