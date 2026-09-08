"""Cover the /test, POST /test, and /debug-items routes."""
import pytest
from fastapi.testclient import TestClient

from app import app
from routes import notion

client = TestClient(app)


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "sekret")
    return {"Authorization": "Bearer sekret"}


def test_get_test_requires_token():
    r = client.get("/test")
    assert r.status_code in (401, 503)


def test_get_test_happy_path(monkeypatch, token):
    monkeypatch.setattr(notion, "update_deadline_text", lambda: None)
    monkeypatch.setattr(notion, "update_important_things_text", lambda: None)
    monkeypatch.setattr("ai.google_calendar.get_tomorrow_events", lambda: [{"summary": "meet"}])
    monkeypatch.setattr("ai.google_calendar.format_events", lambda evs: "meet")
    r = client.get("/test", headers=token)
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_get_test_wraps_failures_in_502(monkeypatch, token):
    monkeypatch.setattr("ai.google_calendar.get_tomorrow_events",
                        lambda: (_ for _ in ()).throw(RuntimeError("cal down")))
    r = client.get("/test", headers=token)
    assert r.status_code == 502


def test_post_test_happy(monkeypatch, token):
    monkeypatch.setattr(notion, "update_deadline_text", lambda: None)
    monkeypatch.setattr(notion, "update_important_things_text", lambda: None)
    r = client.post("/test", headers=token)
    assert r.status_code == 200


def test_post_test_502_on_error(monkeypatch, token):
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(notion, "update_deadline_text", _boom)
    r = client.post("/test", headers=token)
    assert r.status_code == 502


def test_debug_items_buckets(monkeypatch, token):
    from datetime import date, timedelta
    today = date.today()
    monkeypatch.setattr(notion, "get_combined_items", lambda: [
        {"title": "overdue", "due_date": (today - timedelta(days=2)).isoformat(), "source": "todo"},
        {"title": "soon",    "due_date": (today + timedelta(days=3)).isoformat(), "source": "todo"},
        {"title": "later",   "due_date": (today + timedelta(days=30)).isoformat(), "source": "task"},
        {"title": "garbage", "due_date": "not-a-date", "source": "todo"},
        {"title": "empty",   "due_date": None,        "source": "todo"},
    ])
    r = client.get("/debug-items", headers=token)
    assert r.status_code == 200
    body = r.json()
    assert body["counts"] == {"due": 1, "upcoming": 1, "eventually": 1, "no_date": 2}
    assert body["due"][0]["title"] == "overdue"
    titles_nodate = {i["title"] for i in body["no_date"]}
    assert {"garbage", "empty"} <= titles_nodate
