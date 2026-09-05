"""Tests for Notion payload parsers: parse_todo, parse_tasks."""
from routes.notion import parse_todo, parse_tasks


def _todo_page(*, title="Buy milk", status="Inbox", done=False, due=None, text=""):
    return {
        "id": "p1",
        "properties": {
            "Name": {"title": [{"plain_text": title}]},
            "Status": {"status": {"name": status} if status else None},
            "Done": {"checkbox": done},
            "Date": {"date": None},
            "Date Due": {"date": ({"start": due} if due else None)},
            "Text": {"rich_text": ([{"plain_text": text}] if text else [])},
            "image": {"files": []},
        },
    }


def test_parse_todo_includes_inbox_undone():
    pages = [_todo_page(title="Do laundry", status="Inbox", done=False, due="2026-09-10")]
    out = parse_todo(pages)
    assert len(out) == 1
    assert out[0]["item"] == "Do laundry"
    assert out[0]["date_due"] == "2026-09-10"


def test_parse_todo_skips_non_inbox_by_default():
    assert parse_todo([_todo_page(status="Done")]) == []


def test_parse_todo_skips_done_by_default():
    assert parse_todo([_todo_page(status="Inbox", done=True)]) == []


def test_parse_todo_returns_untitled_when_no_title():
    p = _todo_page()
    p["properties"]["Name"]["title"] = []
    assert parse_todo([p])[0]["item"] == "Untitled"


def _task_page(*, title="Ship feature", status="In Progress", due=None, description="", project_id=None):
    return {
        "id": "p1",
        "properties": {
            "Name": {"title": [{"plain_text": title}]},
            "Status": {"status": {"name": status} if status else None},
            "Action Date": {"date": ({"start": due} if due else None)},
            "Description": {"rich_text": ([{"plain_text": description}] if description else [])},
            "Projects": {"relation": ([{"id": project_id}] if project_id else [])},
        },
    }


def test_parse_tasks_undone_included():
    pages = [_task_page(status="In Progress", due="2026-09-10", description="hello")]
    out = parse_tasks(pages)
    assert len(out) == 1
    assert out[0]["title"] == "Ship feature"
    assert out[0]["due_date"] == "2026-09-10"
    assert out[0]["description"] == "hello"
    assert out[0]["source"] == "task"


def test_parse_tasks_skips_done():
    assert parse_tasks([_task_page(status="Done")]) == []


def test_parse_tasks_captures_project_id():
    out = parse_tasks([_task_page(status="Todo", project_id="proj-1")])
    assert out[0]["project_id"] == "proj-1"


def test_parse_tasks_no_project_relation():
    assert parse_tasks([_task_page(status="Todo")])[0]["project_id"] is None
