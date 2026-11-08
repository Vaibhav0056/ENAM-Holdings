"""Run the deterministic results parser over every ingested results PDF and print the table."""
import json
from finn import db, pdfs, results

for a in db.q("select id,symbol,pdf_url from announcements where category='Result' order by symbol"):
    try:
        path = pdfs.download(a["pdf_url"], symbol=a["symbol"], kind="results")
        pages = pdfs.pages_pdfplumber(path)
        cands = results.candidate_pages(pages)
        r = results.extract(pages)
        if not r:
            print(a["symbol"], "NO RESULT; candidates", cands, "unit?", [results.detect_unit(pages[c-1]) for c in cands[:2]])
            continue
        row = {t["metric"]: (t["current"], t["qoq"], t["yoy"]) for t in r["table"]}
        print(a["symbol"], r["basis"], r["unit_reported"], r["confidence"], r["notes"], json.dumps(row))
    except Exception as e:
        print(a["symbol"], "ERROR", e)
