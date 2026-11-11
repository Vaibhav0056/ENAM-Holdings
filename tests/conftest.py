"""Unit tests never touch Neon or Gemini: DATABASE_URL is blanked so finn.database falls back to a throwaway
SQLite file, and LLM tests inject a fake client."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DATABASE_URL"] = ""          # load_dotenv never overrides a variable that is already set
os.environ["FINN_NO_WARMER"] = "1"
os.environ["DATABASE_URL_UNPOOLED"] = ""
os.environ["FINN_TEST_DB"] = str(Path(tempfile.mkdtemp()) / "finn_test.sqlite")
# The Step 1-8 SQLite store (finn.db) reads FINN_DB at import time, and finn.llm imports it during collection,
# so the throwaway path must be set here, before any test module is imported.
os.environ["FINN_DB"] = str(Path(tempfile.mkdtemp()) / "legacy_test.db")

import pytest  # noqa: E402


@pytest.fixture()
def sqldb():
    """Fresh schema on the SQLite fallback for each test."""
    from finn import database
    from finn.models import Base
    database.reset_engine()
    eng = database.engine()
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    database.reset_engine()
