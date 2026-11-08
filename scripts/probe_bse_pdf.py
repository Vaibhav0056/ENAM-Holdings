"""Probe BSE PDF attachment URLs (AttachLive vs AttachHis) and category spread."""
from collections import Counter
from datetime import datetime, timedelta
import requests
from bse import BSE

b = BSE(download_folder="data")
ann = b.announcements(scripcode="500325", from_date=datetime.now() - timedelta(days=60), to_date=datetime.now())
rows = ann["Table"]
print("rows", len(rows), "rowcnt", ann["Table1"])
print(Counter((r["CATEGORYNAME"], r["SUBCATNAME"]) for r in rows).most_common())
h = {"User-Agent": "Mozilla/5.0 (FINN research app)", "Referer": "https://www.bseindia.com/"}
r0 = rows[0]
for base in ["AttachLive", "AttachHis"]:
    u = f"https://www.bseindia.com/xml-data/corpfiling/{base}/{r0['ATTACHMENTNAME']}"
    resp = requests.get(u, headers=h, timeout=30)
    print(base, r0["PDFFLAG"], resp.status_code, resp.headers.get("content-type"), len(resp.content), resp.content[:5])
b.exit()
