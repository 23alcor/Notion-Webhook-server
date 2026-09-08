"""Event-processing pipeline, debounce, and downstream Notion writes."""
import httpx
import pytest
import respx

from routes import notion

NOTION = "https://api.notion.com/v1"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "tkn")


# ----- _maybe_update_deadline_text (debounce) -----

def test_maybe_update_deadline_text_runs_first_call(monkeypatch):
    monkeypatch.setattr(notion, "_last_deadline_update_monotonic", 0.0)
    calls = []
    monkeypatch.setattr(notion, "update_deadline_text", lambda: calls.append(1))
    notion._maybe_update_deadline_text()
    assert calls == [1]


def test_maybe_update_deadline_text_debounces_second_call(monkeypatch):
    # Simulate an update that just happened.
    import time as _time
    monkeypatch.setattr(notion, "_last_deadline_update_monotonic", _time.monotonic())
    calls = []
    monkeypatch.setattr(notion, "update_deadline_text", lambda: calls.append(1))
    notion._maybe_update_deadline_text()
    assert calls == []


# ----- _process_notion_event -----

def test_process_event_missing_page_id_returns(monkeypatch):
    calls = []
    monkeypatch.setattr(notion, "_maybe_update_deadline_text", lambda: calls.append("dl"))
    notion._process_notion_event({"type": "page.created", "entity": {}})
    # early-return before deadline refresh
    assert calls == []


def test_process_event_non_created_only_refreshes_deadline(monkeypatch):
    calls = []
    monkeypatch.setattr(notion, "_maybe_update_deadline_text", lambda: calls.append("dl"))
    monkeypatch.setattr(notion, "read_page", lambda pid: pytest.fail("should not read"))
    notion._process_notion_event({"type": "page.updated", "entity": {"id": "pg1"}})
    assert calls == ["dl"]


def test_process_event_created_reads_thinks_updates(monkeypatch):
    order = []
    monkeypatch.setattr(notion, "_maybe_update_deadline_text", lambda: order.append("dl"))
    monkeypatch.setattr(notion, "read_page",
                        lambda pid: {"properties": {"Name": {"title": [{"plain_text": "hello"}]}}})
    monkeypatch.setattr(notion, "ai_think", lambda title: f"review of {title}")
    monkeypatch.setattr(notion, "update_page_text",
                        lambda pid, text: order.append(("write", pid, text)))
    notion._process_notion_event({"type": "page.created", "entity": {"id": "pg1"}})
    assert order == ["dl", ("write", "pg1", "review of hello")]


def test_process_event_created_untitled_when_empty_title(monkeypatch):
    seen = {}
    monkeypatch.setattr(notion, "_maybe_update_deadline_text", lambda: None)
    monkeypatch.setattr(notion, "read_page", lambda pid: {"properties": {"Name": {"title": []}}})
    monkeypatch.setattr(notion, "ai_think", lambda title: seen.setdefault("title", title))
    monkeypatch.setattr(notion, "update_page_text", lambda pid, text: None)
    notion._process_notion_event({"type": "page.created", "entity": {"id": "pg1"}})
    assert seen["title"] == "Untitled"


def test_process_event_swallows_request_error(monkeypatch):
    monkeypatch.setattr(notion, "_maybe_update_deadline_text", lambda: None)

    def _boom(pid):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(notion, "read_page", _boom)
    # Must not raise.
    notion._process_notion_event({"type": "page.created", "entity": {"id": "pg1"}})


def test_process_event_swallows_generic_error(monkeypatch):
    monkeypatch.setattr(notion, "_maybe_update_deadline_text", lambda: None)
    monkeypatch.setattr(notion, "read_page", lambda pid: {"properties": {"Name": {"title": [{"plain_text": "t"}]}}})

    def _boom(text):
        raise RuntimeError("openai down")

    monkeypatch.setattr(notion, "ai_think", _boom)
    notion._process_notion_event({"type": "page.created", "entity": {"id": "pg1"}})


# ----- change_deadline_text -----

@respx.mock
def test_change_deadline_text_with_explicit_block_id():
    route = respx.patch(f"{NOTION}/blocks/blk1").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    assert notion.change_deadline_text([], block_id="blk1") == {"ok": True}
    assert route.called


@respx.mock
def test_change_deadline_text_reads_env_block_id(monkeypatch):
    monkeypatch.setenv("NOTION_DEADLINE_BLOCK_ID", "envblk")
    respx.patch(f"{NOTION}/blocks/envblk").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    assert notion.change_deadline_text([]) == {"ok": True}


@respx.mock
def test_change_deadline_text_logs_and_raises_on_4xx():
    respx.patch(f"{NOTION}/blocks/blk1").mock(
        return_value=httpx.Response(400, json={"message": "bad"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        notion.change_deadline_text([], block_id="blk1")


# ----- get_project_names -----

def test_get_project_names_no_env_returns_empty(monkeypatch):
    monkeypatch.delenv("NOTION_PROJECTS_ID", raising=False)
    assert notion.get_project_names() == {}


@respx.mock
def test_get_project_names_maps_ids_to_titles(monkeypatch):
    monkeypatch.setenv("NOTION_PROJECTS_ID", "projdb")
    respx.post(f"{NOTION}/databases/projdb/query").mock(
        return_value=httpx.Response(200, json={
            "results": [
                {"id": "p1", "properties": {"Name": {"title": [{"plain_text": "Alpha"}]}}},
                {"id": "p2", "properties": {"Name": {"title": []}}},  # Unnamed branch
            ],
            "has_more": False,
        })
    )
    assert notion.get_project_names() == {"p1": "Alpha", "p2": "Unnamed"}


def test_get_project_names_swallows_error(monkeypatch):
    monkeypatch.setenv("NOTION_PROJECTS_ID", "projdb")
    def _boom(*a, **k):
        raise RuntimeError("nope")
    monkeypatch.setattr(notion, "query_database", _boom)
    assert notion.get_project_names() == {}


# ----- get_combined_items -----

def test_get_combined_items_joins_todos_and_tasks(monkeypatch):
    monkeypatch.setenv("NOTION_TODO_ID", "todo_db")
    monkeypatch.setenv("NOTION_TASKS_ID", "tasks_db")

    def _fake_query(db_id, query_payload=None):
        if db_id == "todo_db":
            return [{"id": "t1", "properties": {
                "Name": {"title": [{"plain_text": "Buy milk"}]},
                "Status": {"status": {"name": "Inbox"}},
                "Done": {"checkbox": False},
                "Date": {"date": {"start": "2026-09-01"}},
                "Date Due": {"date": {"start": "2026-09-10"}},
                "Text": {"rich_text": []},
                "image": {"files": []},
            }}]
        if db_id == "tasks_db":
            return [{"id": "k1", "properties": {
                "Name": {"title": [{"plain_text": "Ship"}]},
                "Status": {"status": {"name": "In Progress"}},
                "Action Date": {"date": {"start": "2026-09-10"}},
                "Description": {"rich_text": []},
                "Projects": {"relation": [{"id": "proj-1"}]},
            }}]
        return []

    monkeypatch.setattr(notion, "query_database", _fake_query)
    monkeypatch.setattr(notion, "get_project_names", lambda: {"proj-1": "Alpha"})

    out = notion.get_combined_items()
    sources = sorted(item["source"] for item in out)
    assert sources == ["task", "todo"]
    task = next(i for i in out if i["source"] == "task")
    assert task["project_name"] == "Alpha"
