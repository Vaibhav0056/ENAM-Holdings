"""Print unit-bearing lines and PAT/PBT rows from results candidate pages."""
import re, sys
from finn import db, pdfs, results
syms = sys.argv[1:] or None
for a in db.q("select symbol,pdf_url from announcements where category='Result' order by symbol"):
    if syms and a["symbol"] not in syms: continue
    pages = pdfs.pages_pdfplumber(pdfs.download(a["pdf_url"]))
    c = results.candidate_pages(pages)
    print("=====", a["symbol"], "pages", len(pages), "cands", c, "chars", [len(p) for p in pages[:6]])
    if not c: continue
    t = pages[c[0]-1]
    for ln in t.splitlines():
        l = ln.lower()
        if re.search(r"crore|lakh|million|₹|rs\.|inr|revenue from|profit before tax|profit for|net profit|finance cost|depreciation|other income", l):
            print("   ", ln[:160])
