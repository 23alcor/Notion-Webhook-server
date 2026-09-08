"""Cover ai/google_calendar.py without touching Google.

We fake both Credentials.from_authorized_user_file and googleapiclient.discovery.build,
returning a fluent chain that mimics the calendar service."""
from datetime import datetime

import pytest

from ai import google_calendar as gc


# ----- pure helpers -----

def test_local_day_to_utc_returns_utc():
    dt = datetime(2026, 9, 6, 0, 0, 0)
    out = gc._local_day_to_utc(dt)
    assert out.tzinfo is not None
    assert out.utcoffset().total_seconds() == 0


def test_is_excluded_by_exact_id(monkeypatch):
    monkeypatch.setattr(gc, "EXCLUDED_CALENDAR_IDS", {"a@x.com"})
    assert gc._is_excluded({"id": "a@X.com", "summary": "x"}) is True
    assert gc._is_excluded({"id": "b@x.com", "summary": "y"}) is False


def test_is_excluded_missing_id_and_summary():
    # Neither present; should not raise, should return False.
    assert gc._is_excluded({}) is False


def test_format_events_empty():
    assert gc.format_events([]) == "nothing scheduled"


def test_format_events_timed_and_allday_and_dedupe():
    events = [
        {"summary": "Sync", "start": {"dateTime": "2026-09-06T09:00:00-04:00"}},
        {"summary": "Sync", "start": {"dateTime": "2026-09-06T09:00:00-04:00"}},  # dup
        {"summary": "All-day", "start": {"date": "2026-09-06"}},
        {"start": {"date": "2026-09-06"}},  # no summary -> "Untitled"
    ]
    out = gc.format_events(events)
    lines = out.splitlines()
    assert any("Sync" in l for l in lines)
    assert "All-day" in out
    assert "Untitled" in out
    # De-dupe removed the second Sync line.
    assert sum("Sync" in l for l in lines) == 1


# ----- fake googleapiclient service -----

class _Exec:
    def __init__(self, value):
        self._v = value
    def execute(self):
        return self._v


class _Events:
    def __init__(self, per_calendar):
        self._per_calendar = per_calendar
    def list(self, calendarId, **_):
        items = self._per_calendar.get(calendarId, [])
        if isinstance(items, Exception):
            raise items
        return _Exec({"items": items})


class _CalendarList:
    def __init__(self, cals):
        self._cals = cals
    def list(self):
        return _Exec({"items": self._cals})


class _Service:
    def __init__(self, cals, per_cal_events):
        self._cals = cals
        self._per_cal = per_cal_events
    def calendarList(self):
        return _CalendarList(self._cals)
    def events(self):
        return _Events(self._per_cal)


@pytest.fixture
def fake_service(monkeypatch):
    """Install a fake build() + credentials so every call is offline."""
    monkeypatch.setattr(gc, "_get_credentials", lambda: object())

    holder = {}

    def _install(cals, per_cal_events=None):
        svc = _Service(cals, per_cal_events or {})
        monkeypatch.setattr(gc, "build", lambda *a, **k: svc)
        holder["svc"] = svc
        return svc

    return _install


def test_list_calendars_prints_and_flags(fake_service, capsys):
    fake_service([
        {"id": "keep@x", "summary": "Work", "accessRole": "owner"},
        {"id": "saraalfanek@gmail.com", "summary": "Sara", "accessRole": "reader"},
    ])
    gc.list_calendars()
    out = capsys.readouterr().out
    assert "keep" in out and "SKIP" in out


def test_get_all_events_skips_excluded_and_sorts(fake_service):
    fake_service(
        cals=[
            {"id": "keep@x", "summary": "Work"},
            {"id": "saraalfanek@gmail.com", "summary": "Sara"},  # excluded by default
        ],
        per_cal_events={
            "keep@x": [
                {"summary": "B", "start": {"dateTime": "2026-09-06T10:00:00Z"}},
                {"summary": "A", "start": {"dateTime": "2026-09-06T09:00:00Z"}},
            ],
        },
    )
    now = datetime(2026, 9, 6, 0, 0, 0)
    events = gc._get_all_events(gc._local_day_to_utc(now),
                                gc._local_day_to_utc(now.replace(hour=23)))
    assert [e["summary"] for e in events] == ["A", "B"]


def test_get_all_events_swallows_per_calendar_error(fake_service):
    fake_service(
        cals=[{"id": "good@x"}, {"id": "bad@x"}],
        per_cal_events={
            "good@x": [{"summary": "Ok", "start": {"dateTime": "2026-09-06T09:00:00Z"}}],
            "bad@x": RuntimeError("boom"),
        },
    )
    now = datetime(2026, 9, 6, 0, 0, 0)
    events = gc._get_all_events(gc._local_day_to_utc(now),
                                gc._local_day_to_utc(now.replace(hour=23)))
    assert [e["summary"] for e in events] == ["Ok"]


def test_get_today_and_tomorrow_events_go_through_service(fake_service):
    fake_service(
        cals=[{"id": "c1"}],
        per_cal_events={"c1": [{"summary": "hello", "start": {"dateTime": "2026-09-06T09:00:00Z"}}]},
    )
    today = gc.get_today_events()
    tomorrow = gc.get_tomorrow_events()
    assert today and tomorrow  # both return the mocked list
