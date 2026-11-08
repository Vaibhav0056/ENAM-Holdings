"""Probe a concall transcript: download (AnnPdfOpen and AttachHis forms) and show speaker-turn lines."""
import re, sys, requests, fitz
name = sys.argv[1]
H = {"User-Agent": "Mozilla/5.0 (compatible; FINN-research-app/0.1)", "Referer": "https://www.bseindia.com/"}
for u in [f"https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname={name}",
          f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{name}",
          f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{name}"]:
    r = requests.get(u, headers=H, timeout=60)
    print(u.split('/')[-2][:20], r.status_code, r.headers.get("content-type"), len(r.content), r.content[:5])
    if r.content[:4] == b"%PDF":
        data = r.content
doc = fitz.open(stream=data, filetype="pdf")
print("pages", len(doc))
for i in (1, 3, 5):
    t = doc[i].get_text("text")
    print(f"----- page {i+1}\n", t[:1500])
