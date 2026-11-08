"""Render FINN app icons (PNG) with PyMuPDF: dark tile, accent ring, letter F."""
import pymupdf

def icon(size, path, maskable=False):
    doc = pymupdf.open()
    page = doc.new_page(width=size, height=size)
    bg, accent = (0.043, 0.051, 0.063), (0.541, 0.706, 1.0)
    page.draw_rect(page.rect, color=None, fill=bg)
    pad = size * (0.22 if maskable else 0.14)
    r = pymupdf.Rect(pad, pad, size - pad, size - pad)
    page.draw_oval(r, color=accent, width=size * 0.045)
    fs = size * (0.40 if maskable else 0.48)
    tw = pymupdf.get_text_length("F", fontname="helv", fontsize=fs)
    page.insert_text((size / 2 - tw / 2, size / 2 + fs * 0.36), "F", fontname="helv", fontsize=fs, color=accent)
    page.get_pixmap(alpha=False).save(path)

for s in (192, 512):
    icon(s, f"static/icons/icon-{s}.png")
icon(512, "static/icons/maskable-512.png", maskable=True)
icon(180, "static/icons/apple-touch-icon.png")
print("ok")
