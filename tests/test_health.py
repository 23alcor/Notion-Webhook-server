"""Static endpoints: /health, /, /privacy."""
from fastapi.testclient import TestClient
from app import app

client = TestClient(app)


def test_health_returns_ok():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_homepage_returns_html():
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "webhook-server" in r.text.lower()


def test_privacy_returns_html():
    r = client.get("/privacy")
    assert r.status_code == 200
    assert "privacy" in r.text.lower()
