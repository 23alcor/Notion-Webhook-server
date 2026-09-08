"""Tests for ai.openai_client functions that would otherwise call OpenAI and
Google Calendar. Both boundaries are faked, so no API keys or network needed.
Time is frozen so greeting/branch logic is deterministic.
"""
from freezegun import freeze_time

from ai import openai_client


# --- Minimal fake mirroring the OpenAI client shape we use --------------
class _FakeCompletions:
    def __init__(self, content):
        self._content = content
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        msg = type("M", (), {"content": self._content})
        choice = type("C", (), {"message": msg})
        return type("R", (), {"choices": [choice]})


class _FakeClient:
    def __init__(self, content="FAKE"):
        self.chat = type("Chat", (), {"completions": _FakeCompletions(content)})


def test_summarize_returns_model_content(monkeypatch):
    fake = _FakeClient("a concise summary")
    monkeypatch.setattr(openai_client, "get_openai_client", lambda: fake)

    out = openai_client.summarize("buy milk tomorrow")

    assert out == "a concise summary"
    # The user note is forwarded to the model as the last message.
    sent = fake.chat.completions.calls[0]
    assert sent["messages"][-1]["content"] == "buy milk tomorrow"


def _stub_calendar(monkeypatch):
    monkeypatch.setattr("ai.google_calendar.get_today_events", lambda: [])
    monkeypatch.setattr("ai.google_calendar.get_tomorrow_events", lambda: [])
    monkeypatch.setattr("ai.google_calendar.format_events", lambda ev: "no events")


@freeze_time("2026-09-06 09:00:00")
def test_important_things_morning_does_not_call_openai(monkeypatch):
    _stub_calendar(monkeypatch)

    def _boom():
        raise AssertionError("OpenAI must not be called before evening")

    monkeypatch.setattr(openai_client, "get_openai_client", _boom)

    items = [{"source": "todo", "title": "late", "due_date": "2026-09-01"}]  # overdue
    rt = openai_client.build_important_things_callout(items)
    text = "".join(b["text"]["content"] for b in rt)

    assert "Good morning Ralph" in text
    assert "1 due tasks" in text


@freeze_time("2026-09-06 20:00:00")
def test_important_things_evening_calls_openai_for_sleep_line(monkeypatch):
    _stub_calendar(monkeypatch)
    fake = _FakeClient("Sleep by 11pm.")
    monkeypatch.setattr(openai_client, "get_openai_client", lambda: fake)

    rt = openai_client.build_important_things_callout([])
    text = "".join(b["text"]["content"] for b in rt)

    assert "Day is almost over Ralph" in text
    assert "Sleep by 11pm." in text


@freeze_time("2026-09-06 09:00:00")
def test_important_things_buckets_by_due_date(monkeypatch):
    _stub_calendar(monkeypatch)
    monkeypatch.setattr(openai_client, "get_openai_client", lambda: None)

    items = [
        {"source": "todo", "title": "overdue", "due_date": "2026-09-01"},   # due
        {"source": "todo", "title": "soon", "due_date": "2026-09-09"},      # upcoming (<=7d)
        {"source": "todo", "title": "later", "due_date": "2026-10-30"},     # eventually
        {"source": "todo", "title": "no date"},                            # backlog -> due
    ]
    rt = openai_client.build_important_things_callout(items)
    text = "".join(b["text"]["content"] for b in rt)

    assert "2 due tasks" in text        # overdue + no-date
    assert "1 upcoming tasks" in text
    assert "1 tasks eventually" in text
