"""Run with pytest after installing jobs.py as routes/jobs.py on the Pi."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app import app
from routes import jobs

client = TestClient(app)


@pytest.fixture
def headers(monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "phone-test-key")
    monkeypatch.setenv("COMMAND_CENTER_URL", "https://jobs.example.com")
    monkeypatch.setenv("COMMAND_CENTER_CAPTURE_TOKEN", "capture-" + "a" * 40)
    return {"Authorization": "Bearer phone-test-key"}


def mock_remote(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(jobs.httpx, "AsyncClient", lambda **options: original(transport=httpx.MockTransport(handler), **options))


def test_capture_requires_existing_phone_auth(headers):
    assert client.post("/jobs", json={"url": "https://example.com/job"}).status_code == 401


def test_phone_shape_is_forwarded_without_notion(monkeypatch, headers):
    def handle(request):
        import json
        assert str(request.url) == "https://jobs.example.com/api/jobs"
        assert request.headers["authorization"].startswith("Bearer capture-")
        assert json.loads(request.content)["role"] == "Product Engineer"
        return httpx.Response(201, json={"id": "job-123", "duplicate": False})
    mock_remote(monkeypatch, handle)
    result = client.post("/jobs", headers=headers, json={"url": "https://employer.example/job", "name": "Product Engineer"})
    assert result.status_code == 201
    assert result.json() == {"status": "created", "id": "job-123", "duplicate": False, "url": "https://jobs.example.com", "title": "Product Engineer"}


def test_duplicate_is_not_reported_as_new(monkeypatch, headers):
    mock_remote(monkeypatch, lambda request: httpx.Response(200, json={"id": "same-job", "duplicate": True}))
    result = client.post("/jobs", headers=headers, json={"url": "https://example.com/job"})
    assert result.status_code == 200
    assert result.json()["status"] == "exists"
    assert result.json()["duplicate"] is True


@pytest.mark.parametrize("payload", [[], {"url": "javascript:alert(1)"}, {"url": "https://name:password@example.com/job"}, {"url": 42}, {"url": "https://example.com", "notes": {}}])
def test_invalid_capture_is_rejected(headers, payload):
    assert client.post("/jobs", headers=headers, json=payload).status_code == 400


def test_timeout_does_not_claim_a_save(monkeypatch, headers):
    def timeout(request):
        raise httpx.ReadTimeout("secret internal connection detail", request=request)
    mock_remote(monkeypatch, timeout)
    result = client.post("/jobs", headers=headers, json={"url": "https://example.com/job"})
    assert result.status_code == 502
    assert "Could not confirm" in result.json()["detail"]
    assert "secret internal" not in result.text


def test_upstream_errors_and_misconfiguration_do_not_claim_success(monkeypatch, headers):
    mock_remote(monkeypatch, lambda request: httpx.Response(401, json={"error": "private diagnostic"}))
    result = client.post("/jobs", headers=headers, json={"url": "https://example.com/job"})
    assert result.status_code == 502
    assert "private diagnostic" not in result.text
    monkeypatch.delenv("COMMAND_CENTER_CAPTURE_TOKEN")
    assert client.post("/jobs", headers=headers, json={"url": "https://example.com/job"}).status_code == 503


def test_body_size_and_json_validation(headers):
    assert client.post("/jobs", headers=headers, content="invalid").status_code == 400
    assert client.post("/jobs", headers=headers, json={"url": "https://example.com", "notes": "x" * 33000}).status_code == 413
