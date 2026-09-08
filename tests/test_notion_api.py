"""HTTP-boundary tests for routes.notion.

The real Notion API is never hit: respx intercepts httpx calls and returns
canned responses, so these run offline, fast, and deterministically.
"""
import json

import httpx
import pytest
import respx

from routes import notion

NOTION = "https://api.notion.com/v1"


@pytest.fixture(autouse=True)
def _notion_env(monkeypatch):
    # _notion_headers() needs a token; value is irrelevant since nothing real is called.
    monkeypatch.setenv("NOTION_TOKEN", "secret_token_test")


@respx.mock
def test_query_database_collects_results():
    route = respx.post(f"{NOTION}/databases/db123/query").mock(
        return_value=httpx.Response(
            200, json={"results": [{"id": "a"}, {"id": "b"}], "has_more": False}
        )
    )
    out = notion.query_database("db123")
    assert [p["id"] for p in out] == ["a", "b"]
    assert route.called


@respx.mock
def test_query_database_follows_pagination():
    respx.post(f"{NOTION}/databases/db123/query").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"results": [{"id": "a"}], "has_more": True, "next_cursor": "cur1"},
            ),
            httpx.Response(200, json={"results": [{"id": "b"}], "has_more": False}),
        ]
    )
    out = notion.query_database("db123")
    assert [p["id"] for p in out] == ["a", "b"]


@respx.mock
def test_query_database_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(notion.time, "sleep", lambda *_: None)  # skip real backoff
    calls = {"n": 0}

    def _flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"results": [{"id": "ok"}], "has_more": False})

    respx.post(f"{NOTION}/databases/db123/query").mock(side_effect=_flaky)
    out = notion.query_database("db123")
    assert calls["n"] == 2
    assert out == [{"id": "ok"}]


@respx.mock
def test_query_database_raises_after_max_retries(monkeypatch):
    monkeypatch.setattr(notion.time, "sleep", lambda *_: None)

    def _always_timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    respx.post(f"{NOTION}/databases/db123/query").mock(side_effect=_always_timeout)
    with pytest.raises(httpx.ReadTimeout):
        notion.query_database("db123")


@respx.mock
def test_read_page_returns_json():
    respx.get(f"{NOTION}/pages/pg1").mock(
        return_value=httpx.Response(200, json={"id": "pg1"})
    )
    assert notion.read_page("pg1") == {"id": "pg1"}


@respx.mock
def test_read_page_raises_on_http_error():
    respx.get(f"{NOTION}/pages/pg1").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        notion.read_page("pg1")


@respx.mock
def test_read_page_raises_on_network_error():
    respx.get(f"{NOTION}/pages/pg1").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(httpx.RequestError):
        notion.read_page("pg1")


@respx.mock
def test_update_page_text_writes_text_and_backfills_due_date():
    # update_page_text first read_page()s to inspect Date / Date Due.
    respx.get(f"{NOTION}/pages/pg1").mock(
        return_value=httpx.Response(
            200,
            json={
                "properties": {
                    "Date": {"date": {"start": "2026-09-10"}},
                    "Date Due": {"date": None},
                }
            },
        )
    )
    patch_route = respx.patch(f"{NOTION}/pages/pg1").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    result = notion.update_page_text("pg1", "hello world")
    assert result == {"ok": True}
    assert patch_route.called

    sent = json.loads(patch_route.calls.last.request.content)
    assert sent["properties"]["Text"]["rich_text"][0]["text"]["content"] == "hello world"
    # Date Due was empty, so it is backfilled from Date.
    assert sent["properties"]["Date Due"]["date"]["start"] == "2026-09-10"
