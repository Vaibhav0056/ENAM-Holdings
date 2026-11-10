"""Probe screener.in Documents for research depth: how many transcripts and annual reports are linked, and what
the links look like. Polite: generic User-Agent, 1 s between requests. Usage: probe_screener_docs.py SYM [SYM...]"""
import json
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (compatible; FINN-research-app/0.2)"}


def probe(sym: str) -> dict:
    r = requests.get(f"https://www.screener.in/company/{sym}/consolidated/", headers=UA, timeout=30)
    out = {"symbol": sym, "status": r.status_code, "bytes": len(r.text)}
    soup = BeautifulSoup(r.text, "html.parser")
    docs = soup.select_one("section#documents")
    if not docs:
        out["documents_section"] = False
        return out
    calls = []
    for li in docs.select(".concalls li"):
        when = li.select_one(".ink-600, .font-size-15, div")
        links = {a.get_text(strip=True): a.get("href") for a in li.select("a")}
        calls.append({"when": " ".join(li.get_text(" ", strip=True).split())[:24], "links": list(links),
                      "transcript": links.get("Transcript")})
    ars = [{"title": " ".join(a.get_text(" ", strip=True).split())[:60], "href": a.get("href")}
           for a in docs.select(".annual-reports li a")]
    transcripts = [c for c in calls if c["transcript"]]
    out.update(documents_section=True, concall_rows=len(calls), transcripts=len(transcripts),
               transcript_hosts=sorted({re.sub(r"^https?://([^/]+)/.*", r"\1", c["transcript"]) for c in transcripts}),
               first_transcripts=[(c["when"], c["transcript"]) for c in transcripts[:2]],
               annual_reports=len(ars), first_annual_reports=ars[:2])
    return out


if __name__ == "__main__":
    for i, s in enumerate(sys.argv[1:] or ["DIVISLAB"]):
        if i:
            time.sleep(1)
        print(json.dumps(probe(s), indent=1))
