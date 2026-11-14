"""/health must answer without touching any database."""
from fastapi.testclient import TestClient


def test_health_needs_no_database(monkeypatch):
    import api
    from finn import database

    def boom(*a, **k):
        raise AssertionError("health touched the database")

    monkeypatch.setattr(database, "session", boom)
    monkeypatch.setattr(database, "read_session_run", boom)
    r = TestClient(api.app).get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True
