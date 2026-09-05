"""Unit tests for the HMAC signature helper in routes.notion."""
import hmac
import hashlib
from routes.notion import _verify_notion_signature

SECRET = "test_secret_xyz"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature_returns_true(monkeypatch):
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", SECRET)
    body = b'{"event":"page.created","id":"abc"}'
    assert _verify_notion_signature(body, _sign(body)) is True


def test_wrong_secret_rejected(monkeypatch):
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", SECRET)
    body = b'{"event":"page.created"}'
    assert _verify_notion_signature(body, _sign(body, "wrong_secret")) is False


def test_tampered_body_rejected(monkeypatch):
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", SECRET)
    sig = _sign(b'{"event":"page.created"}')
    tampered = b'{"event":"page.deleted"}'
    assert _verify_notion_signature(tampered, sig) is False


def test_missing_header_rejected(monkeypatch):
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", SECRET)
    assert _verify_notion_signature(b'{"x":1}', None) is False


def test_empty_header_rejected(monkeypatch):
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", SECRET)
    assert _verify_notion_signature(b'{"x":1}', "") is False


def test_no_secret_configured_rejects(monkeypatch):
    monkeypatch.delenv("NOTION_WEBHOOK_SECRET", raising=False)
    body = b'{"x":1}'
    assert _verify_notion_signature(body, _sign(body)) is False


def test_signature_without_sha256_prefix_rejected(monkeypatch):
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", SECRET)
    body = b'{"x":1}'
    bare = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert _verify_notion_signature(body, bare) is False


def test_uppercase_hex_rejected(monkeypatch):
    """hexdigest() is lowercase; uppercase must not match."""
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", SECRET)
    body = b'{"x":1}'
    assert _verify_notion_signature(body, _sign(body).upper()) is False
