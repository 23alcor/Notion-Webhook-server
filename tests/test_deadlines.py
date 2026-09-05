"""Pure helpers in ai/openai_client.py — no OpenAI, no Notion, no calendar."""
from datetime import date, timedelta
from ai.openai_client import _parse_due_date, build_deadlines_callout


def test_parse_due_date_iso_date():
    assert _parse_due_date("2026-09-10") == date(2026, 9, 10)


def test_parse_due_date_iso_datetime_z():
    assert _parse_due_date("2026-09-10T14:30:00Z") == date(2026, 9, 10)


def test_parse_due_date_iso_datetime_offset():
    assert _parse_due_date("2026-09-10T14:30:00-04:00") == date(2026, 9, 10)


def test_parse_due_date_none_returns_today():
    assert _parse_due_date(None) == date.today()


def test_parse_due_date_blank_returns_none():
    assert _parse_due_date("   ") is None


def test_parse_due_date_garbage_returns_none():
    assert _parse_due_date("not-a-date") is None


def test_build_deadlines_callout_empty():
    result = build_deadlines_callout([])
    assert len(result) == 1
    assert "No deadlines" in result[0]["text"]["content"]


def test_build_deadlines_callout_overdue_is_red():
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    items = [{"source": "todo", "title": "Late", "due_date": yesterday}]
    rt = build_deadlines_callout(items)
    entry = next(r for r in rt if "Late" in r["text"]["content"])
    assert entry["annotations"]["color"] == "red"


def test_build_deadlines_callout_far_future_is_green():
    future = (date.today() + timedelta(days=10)).isoformat()
    items = [{"source": "todo", "title": "Later", "due_date": future}]
    rt = build_deadlines_callout(items)
    entry = next(r for r in rt if "Later" in r["text"]["content"])
    assert entry["annotations"]["color"] == "green"


def test_build_deadlines_callout_tasks_grouped_by_project():
    d = (date.today() + timedelta(days=5)).isoformat()
    items = [
        {"source": "task", "title": "T1", "due_date": d, "project_name": "Alpha"},
        {"source": "task", "title": "T2", "due_date": d, "project_name": "Alpha"},
    ]
    joined = "".join(r["text"]["content"] for r in build_deadlines_callout(items))
    assert "Alpha" in joined and "T1" in joined and "T2" in joined


def test_build_deadlines_callout_caps_at_100_entries():
    """Notion caps rich_text arrays at 100 entries per block."""
    d = (date.today() + timedelta(days=1)).isoformat()
    items = [{"source": "todo", "title": f"item{i}", "due_date": d} for i in range(200)]
    assert len(build_deadlines_callout(items)) <= 100
