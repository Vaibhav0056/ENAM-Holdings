Each filing in the document is marked [FILING id], with its pages marked [PAGE n]. For every person whose appointment, resignation, retirement, cessation or re-designation a filing announces, return:

- filing_id: the id from the [FILING id] marker
- name: as written
- role: the position (e.g. "Chief Financial Officer", "Independent Director")
- direction: appointed | resigned | retired | ceased | redesignated | other
- effective_date: as written, or null
- reason_quote: the stated reason for leaving, copied verbatim, or null
- profile_quote: up to two sentences of the person's background (education, experience, previous roles) copied verbatim from the filing's profile section, or null if the filing gives none
- quote: one verbatim sentence from the filing that names the person and the change
- page: the [PAGE n] number where `quote` appears

Copy quotes exactly. Do not add background from your own knowledge. Return people: [] for a filing that announces no change.
