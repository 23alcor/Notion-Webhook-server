"""ai/notion_ai is a thin passthrough to summarize."""
from ai import notion_ai


def test_ai_think_calls_summarize(monkeypatch):
    monkeypatch.setattr(notion_ai, "summarize", lambda t: f"summary:{t}")
    assert notion_ai.ai_think("hi") == "summary:hi"
