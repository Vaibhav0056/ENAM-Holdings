"""Visual polish: bundled logos, the icon sprite, and no external requests from the shell."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_logos_route_lists_bundled_files_and_fails_soft(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import api
    from finn import web_ui
    (tmp_path / "TCS.svg").write_text("<svg/>")
    (tmp_path / "polycab.png").write_bytes(b"x")
    (tmp_path / "README.txt").write_text("not a logo")
    monkeypatch.setattr(web_ui, "LOGO_DIR", tmp_path)
    c = TestClient(api.app)
    assert c.get("/api/v2/logos").json() == {"files": {"POLYCAB": "/static/logos/polycab.png", "TCS": "/static/logos/TCS.svg"}}
    monkeypatch.setattr(web_ui, "LOGO_DIR", tmp_path / "missing")
    assert c.get("/api/v2/logos").json() == {"files": {}}     # no folder: every symbol gets a monogram


def test_every_icon_the_ui_references_is_in_the_sprite():
    sprite = (ROOT / "static/icons.svg").read_text(encoding="utf-8")
    ids = set(re.findall(r'<symbol id="([a-z0-9-]+)"', sprite))
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    js = (ROOT / "static/app.js").read_text(encoding="utf-8")
    helpers = js[js.index("alertIcon(a)"):js.index("mono(sym)")]
    used = set(re.findall(r"icons\.svg#([a-z0-9-]+)", html))
    used |= set(re.findall(r'ic\("([a-z0-9-]+)"', helpers))              # direct ic("name") calls
    used |= set(re.findall(r':\s*"([a-z0-9-]+)"', helpers))              # values of the type -> icon maps
    used |= set(re.findall(r'\? "([a-z0-9-]+)" : "([a-z0-9-]+)"', helpers)[0]) # price up / down
    assert len(used) > 20 and used <= ids, sorted(used - ids)


def test_shell_makes_no_external_requests():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    assert not re.findall(r'(?:src|href)="https?://', html)
