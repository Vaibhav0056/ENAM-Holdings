The document holds pages of an Indian listed company's annual report ({{report}}) with [PAGE n] markers: the cash-flow statement, balance sheet, profit and loss statement and notes.

Return fiscal_year (the year the report covers, "FYyy") and items, one per figure found, for the CURRENT year and the PREVIOUS year columns:
- revenue: revenue from operations; pat: profit for the year; pbt: profit before tax; depreciation; finance_costs; other_income
- ocf: net cash generated from operating activities; capex: purchase of property, plant and equipment (and intangibles), as a positive number
- receivables: TOTAL trade receivables on the balance sheet (current + non-current); inventories: total inventories; payables: TOTAL trade payables (MSME + others)
- borrowings_long: non-current borrowings; borrowings_short: current borrowings
- cash: cash and cash equivalents (plus other bank balances if shown on the same line); current_investments
- equity: total equity attributable to owners; contingent_liabilities: total contingent liabilities not provided for

value exactly as printed (brackets mean negative), unit as stated in the statement header (crore, lakh, million), basis = consolidated or standalone. Prefer the consolidated statements. quote = the row label and its numbers copied verbatim; page = the [PAGE n] it is on. Skip any item you cannot find; never compute a figure.
