"""Probe screener.in company page: find the Documents section and the concall / annual report links."""
import sys, time, requests
from bs4 import BeautifulSoup

sym = sys.argv[1] if len(sys.argv) > 1 else "POLYCAB"
H = {"User-Agent": "Mozilla/5.0 (compatible; FINN-research-app/0.1)"}
for url in [f"https://www.screener.in/company/{sym}/consolidated/", f"https://www.screener.in/company/{sym}/"]:
    r = requests.get(url, headers=H, timeout=30)
    print(url, r.status_code, len(r.text))
    if r.ok:
        break
    time.sleep(1)
soup = BeautifulSoup(r.text, "html.parser")
docs = soup.select_one("section#documents")
print("documents section:", bool(docs))
for block in docs.select("div.documents, .annual-reports, .concalls") if docs else []:
    print("== block classes:", block.get("class"), "| heading:", (block.find(["h3", "h4"]) or {}).get_text(strip=True) if block.find(["h3","h4"]) else None)
    for li in block.select("li")[:6]:
        print("   LI:", " ".join(li.get_text(" ", strip=True).split())[:90])
        for a in li.select("a"):
            print("      a:", a.get_text(strip=True)[:30], "|", a.get("class"), "|", a.get("href"))
