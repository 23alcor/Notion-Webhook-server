"""Small tests to close remaining coverage gaps: openai client construction,
the two-line glue functions, and a couple of parser edge branches."""
import threading

import pytest

from ai import openai_client
from routes import notion


# ----- ai.openai_client.get_openai_client -----

def test_get_openai_client_missing_key_raises(monkeypatch):
    monkeypatch.setattr(openai_client, "DEVELOPER_MODE", False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY missing"):
        openai_client.get_openai_client()


def test_get_openai_client_returns_client(monkeypatch):
    monkeypatch.setattr(openai_client, "DEVELOPER_MODE", False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    client = openai_client.get_openai_client()
    assert client is not None


def test_get_openai_client_developer_mode_missing_project_key(monkeypatch):
    monkeypatch.setattr(openai_client, "DEVELOPER_MODE", True)
    monkeypatch.delenv("OPENAI_API_KEY_PROJECT", raising=False)
    with pytest.raises(RuntimeError):
        openai_client.get_openai_client()


def test_get_openai_client_developer_mode_with_project_key(monkeypatch):
    monkeypatch.setattr(openai_client, "DEVELOPER_MODE", True)
    monkeypatch.setenv("OPENAI_API_KEY_PROJECT", "sk-proj-fake")
    assert openai_client.get_openai_client() is not None


# ----- build_important_things_callout: malformed due_date bucket -----

def test_important_things_malformed_due_date_goes_to_eventually(monkeypatch):
    from freezegun import freeze_time

    monkeypatch.setattr("ai.google_calendar.get_today_events", lambda: [])
    monkeypatch.setattr("ai.google_calendar.get_tomorrow_events", lambda: [])
    monkeypatch.setattr("ai.google_calendar.format_events", lambda ev: "")
    monkeypatch.setattr(openai_client, "get_openai_client", lambda: None)

    with freeze_time("2026-09-06 09:00:00"):
        rt = openai_client.build_important_things_callout(
            [{"source": "todo", "title": "x", "due_date": "not-iso"}]
        )
        text = "".join(b["text"]["content"] for b in rt)
        assert "1 tasks eventually" in text


# ----- routes.notion glue -----

def test_update_deadline_text_glue(monkeypatch):
    monkeypatch.setattr(notion, "get_combined_items", lambda: [])
    monkeypatch.setattr(notion, "build_deadlines_callout", lambda items: [{"marker": True}])
    seen = {}
    monkeypatch.setattr(notion, "change_deadline_text",
                        lambda rt, block_id=None: seen.setdefault("rt", rt))
    notion.update_deadline_text()
    assert seen["rt"] == [{"marker": True}]


def test_update_important_things_text_glue(monkeypatch):
    monkeypatch.setenv("NOTION_IMPORTANT_THINGS_BLOCK_ID", "imp_blk")
    monkeypatch.setattr(notion, "get_combined_items", lambda: [{"x": 1}])

    # Patch the function inside the ai.openai_client module — routes.notion
    # imports it locally inside update_important_things_text.
    import ai.openai_client as oc
    monkeypatch.setattr(oc, "build_important_things_callout", lambda items: [{"y": 2}])

    seen = {}
    def _fake_change(rt, block_id=None):
        seen["rt"] = rt
        seen["block_id"] = block_id
    monkeypatch.setattr(notion, "change_deadline_text", _fake_change)

    notion.update_important_things_text()
    assert seen == {"rt": [{"y": 2}], "block_id": "imp_blk"}


# ----- worker thread smoke test -----

def test_worker_thread_processes_events_and_exits(monkeypatch):
    """Feed one event and a shutdown sentinel through the worker function."""
    from queue import Queue
    q = Queue()
    processed = []
    monkeypatch.setattr(notion, "_event_queue", q)
    monkeypatch.setattr(notion, "_process_notion_event", lambda d: processed.append(d))

    t = threading.Thread(target=notion._worker_thread, daemon=True)
    t.start()

    q.put({"type": "page.updated", "entity": {"id": "pg1"}})
    q.put(None)  # sentinel -> break loop
    t.join(timeout=2.0)

    assert processed == [{"type": "page.updated", "entity": {"id": "pg1"}}]
    assert not t.is_alive()


def test_worker_thread_swallows_processor_exceptions(monkeypatch):
    """A raising _process_notion_event must not kill the worker."""
    from queue import Queue
    q = Queue()
    calls = []

    def _boom(data):
        calls.append(data)
        raise RuntimeError("oops")

    monkeypatch.setattr(notion, "_event_queue", q)
    monkeypatch.setattr(notion, "_process_notion_event", _boom)

    t = threading.Thread(target=notion._worker_thread, daemon=True)
    t.start()
    q.put({"n": 1})
    q.put({"n": 2})
    q.put(None)
    t.join(timeout=2.0)

    assert calls == [{"n": 1}, {"n": 2}]
    assert not t.is_alive()
