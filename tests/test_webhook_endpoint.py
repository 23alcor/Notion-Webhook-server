"""End-to-end tests of POST /notion-webhook auth behavior."""
import hmac
import hashlib
import json
from fastapi.testclient import TestClient
from app import app

client = TestClient(app)
SECRET = "test_secret_xyz"


def _sig(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verification_handshake_returns_ok():
    """Notion's one-time subscription handshake; no signature needed."""
    r = client.post("/notion-webhook", json={"verification_token": "secret_abc"})
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_challenge_backcompat():
    r = client.post("/notion-webhook", json={"challenge": "echo-me"})
    assert r.status_code == 200
    assert r.json() == {"challenge": "echo-me"}


def test_invalid_json_returns_400():
    r = client.post(
        "/notion-webhook",
        content=b"not-json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_valid_signature_accepted(monkeypatch):
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", SECRET)
    body = json.dumps({"type": "page.updated", "entity": {"id": "abc"}}).encode()
    r = client.post(
        "/notion-webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Notion-Signature": _sig(body)},
    )
    assert r.status_code == 200
    assert r.json() == {"status": "accepted"}


def test_missing_signature_rejected(monkeypatch):
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", SECRET)
    body = json.dumps({"type": "page.updated"}).encode()
    r = client.post("/notion-webhook", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 401


def test_bogus_signature_rejected(monkeypatch):
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", SECRET)
    body = json.dumps({"type": "page.updated"}).encode()
    r = client.post(
        "/notion-webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Notion-Signature": "sha256=deadbeef"},
    )
    assert r.status_code == 401


def test_tampered_body_rejected(monkeypatch):
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", SECRET)
    original = json.dumps({"type": "page.created", "entity": {"id": "abc"}}).encode()
    sig = _sig(original)
    tampered = json.dumps({"type": "page.deleted", "entity": {"id": "abc"}}).encode()
    r = client.post(
        "/notion-webhook",
        content=tampered,
        headers={"Content-Type": "application/json", "X-Notion-Signature": sig},
    )
    assert r.status_code == 401


def test_fail_open_when_no_secret(monkeypatch):
    """When NOTION_WEBHOOK_SECRET is unset the endpoint currently fails open."""
    monkeypatch.delenv("NOTION_WEBHOOK_SECRET", raising=False)
    body = json.dumps({"type": "page.updated"}).encode()
    r = client.post("/notion-webhook", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 200
    assert r.json() == {"status": "accepted"}
