"""Small helpers in routes.notion: env, test-token auth, and edge branches
of the pure parsers (parse_database, parse_todo image handling)."""
import pytest
from fastapi import HTTPException

from routes import notion


# ----- _require_env -----

def test_require_env_returns_value(monkeypatch):
    monkeypatch.setenv("SOME_VAR", "hi")
    assert notion._require_env("SOME_VAR") == "hi"


def test_require_env_raises_when_missing(monkeypatch):
    monkeypatch.delenv("SOME_VAR", raising=False)
    with pytest.raises(RuntimeError, match="SOME_VAR missing"):
        notion._require_env("SOME_VAR")


# ----- _require_test_token -----

def test_require_test_token_no_secret_configured(monkeypatch):
    monkeypatch.delenv("TEST_TOKEN", raising=False)
    with pytest.raises(HTTPException) as exc:
        notion._require_test_token(authorization="Bearer whatever")
    assert exc.value.status_code == 503


def test_require_test_token_missing_header(monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "s3cret")
    with pytest.raises(HTTPException) as exc:
        notion._require_test_token(authorization=None)
    assert exc.value.status_code == 401


def test_require_test_token_wrong_scheme(monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "s3cret")
    with pytest.raises(HTTPException) as exc:
        notion._require_test_token(authorization="Basic zzz")
    assert exc.value.status_code == 401


def test_require_test_token_wrong_value(monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "s3cret")
    with pytest.raises(HTTPException) as exc:
        notion._require_test_token(authorization="Bearer nope")
    assert exc.value.status_code == 401


def test_require_test_token_valid_returns_none(monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "s3cret")
    assert notion._require_test_token(authorization="Bearer s3cret") is None


# ----- parse_database -----

def _db_page(**overrides):
    """Builder that returns a properties-dict shaped like Notion sends."""
    page = {
        "id": "pg1",
        "properties": {
            "Name": {"title": [{"plain_text": overrides.get("title", "T")}]},
            "Date": {"date": {"start": overrides["date"]} if overrides.get("date") else None},
            "Done": {"checkbox": overrides.get("done", False)},
            "Status": {"status": {"name": overrides["status"]} if overrides.get("status") else None},
            "Text": {"rich_text": [{"plain_text": overrides["text"]}] if overrides.get("text") else []},
            "image": {"files": overrides.get("files", [])},
        },
    }
    return page


def test_parse_database_full_record():
    p = _db_page(title="hi", date="2026-09-06", done=True, status="Inbox", text="body")
    out = notion.parse_database([p])
    assert out == [{
        "id": "pg1", "title": "hi", "date": "2026-09-06", "done": True,
        "status": "Inbox", "text": "body", "image": None,
    }]


def test_parse_database_empty_title_becomes_untitled():
    p = _db_page()
    p["properties"]["Name"]["title"] = []
    assert notion.parse_database([p])[0]["title"] == "Untitled"


def test_parse_database_reads_external_image_url():
    p = _db_page(files=[{"type": "external", "external": {"url": "https://ex/img.png"}}])
    assert notion.parse_database([p])[0]["image"] == "https://ex/img.png"


def test_parse_database_reads_notion_hosted_file_url():
    p = _db_page(files=[{"type": "file", "file": {"url": "https://s3/img.png"}}])
    assert notion.parse_database([p])[0]["image"] == "https://s3/img.png"


# ----- parse_todo image branches -----

def _todo_page_with_image(files):
    return {
        "id": "p1",
        "properties": {
            "Name": {"title": [{"plain_text": "t"}]},
            "Status": {"status": {"name": "Inbox"}},
            "Done": {"checkbox": False},
            "Date": {"date": None},
            "Date Due": {"date": None},
            "Text": {"rich_text": []},
            "image": {"files": files},
        },
    }


def test_parse_todo_external_image_url():
    p = _todo_page_with_image([{"type": "external", "external": {"url": "https://x/i.png"}}])
    assert notion.parse_todo([p])[0]["image"] == "https://x/i.png"


def test_parse_todo_notion_file_image_url():
    p = _todo_page_with_image([{"type": "file", "file": {"url": "https://x/f.png"}}])
    assert notion.parse_todo([p])[0]["image"] == "https://x/f.png"


def test_parse_todo_unknown_image_type_returns_none():
    p = _todo_page_with_image([{"type": "weird"}])
    assert notion.parse_todo([p])[0]["image"] is None


# ----- parse_tasks additional coverage -----

def test_parse_tasks_untitled_and_no_status():
    page = {
        "id": "p", "properties": {
            "Name": {"title": []},
            "Status": {"status": None},
            "Action Date": {"date": None},
            "Description": {"rich_text": []},
            "Projects": {"relation": []},
        },
    }
    out = notion.parse_tasks([page])
    assert out[0]["title"] == "Untitled"
    assert out[0]["status"] is None
    assert out[0]["due_date"] is None
