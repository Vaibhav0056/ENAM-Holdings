"""Probe a BSE results PDF: page count, which pages hold the P&L table, and text shape."""
import io, sys, requests, pdfplumber

url = sys.argv[1]
h = {"User-Agent": "Mozilla/5.0 (FINN research app)", "Referer": "https://www.bseindia.com/"}
pdf_bytes = requests.get(url, headers=h, timeout=60).content
print("bytes", len(pdf_bytes))
with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
    print("pages", len(pdf.pages))
    for i, p in enumerate(pdf.pages[:12], 1):
        t = p.extract_text() or ""
        low = t.lower()
        hit = [k for k in ["revenue from operations", "profit before tax", "finance costs", "depreciation", "other income", "consolidated", "crore", "lakh"] if k in low]
        print(f"--- page {i} chars={len(t)} hits={hit}")
    target = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    print(pdf.pages[target - 1].extract_text()[:3000])
