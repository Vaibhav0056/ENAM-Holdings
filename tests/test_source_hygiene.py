"""No control characters in source files. A regex written as "\\b" through a non-raw string once became a literal
backspace (0x08) in two places, silently turning word boundaries into "match a backspace"."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = {".venv", ".git", "node_modules", "__pycache__", "data", "fixtures"}


def test_no_control_bytes_in_source():
    bad = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or SKIP & set(p.parts) or p.suffix not in (".py", ".js", ".css", ".html", ".yml", ".yaml", ".md"):
            continue
        b = p.read_bytes()
        if any(c < 32 and c not in (9, 10, 13) for c in b):
            bad.append(str(p.relative_to(ROOT)))
    assert bad == []
