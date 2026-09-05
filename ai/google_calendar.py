import os
import time as _time
from datetime import datetime, timedelta, timezone
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_PATH = os.path.join(os.path.dirname(__file__), "..", "token.json")


def _get_credentials():
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return creds


def _local_day_to_utc(local_dt: datetime) -> datetime:
    offset = timedelta(seconds=-_time.timezone)
    return (local_dt - offset).replace(tzinfo=timezone.utc)


# --- Calendar filtering -------------------------------------------------
# Exact calendar IDs to skip. A calendar shared with you usually has the
# owner's email address as its ID. Override without a code change via env:
DEFAULT_EXCLUDED_CALENDARS = "saraalfanek@gmail.com"
EXCLUDED_CALENDAR_IDS = {
    c.strip().lower()
    for c in os.getenv("EXCLUDED_CALENDARS", DEFAULT_EXCLUDED_CALENDARS).split(",")
    if c.strip()
}

# Substrings matched against the calendar ID *and* its display name.
EXCLUDED_CALENDAR_KEYWORDS = ()


def _is_excluded(calendar: dict) -> bool:
    cal_id = (calendar.get("id") or "").lower()
    name = (calendar.get("summaryOverride") or calendar.get("summary") or "").lower()
    if cal_id in EXCLUDED_CALENDAR_IDS:
        return True
    return any(
        k.lower() in cal_id or k.lower() in name
        for k in EXCLUDED_CALENDAR_KEYWORDS
    )


def list_calendars() -> None:
    """Print every calendar on the account so you can copy exact IDs."""
    service = build("calendar", "v3", credentials=_get_credentials())
    for cal in service.calendarList().list().execute().get("items", []):
        flag = "SKIP" if _is_excluded(cal) else "keep"
        name = cal.get("summaryOverride") or cal.get("summary")
        print(f"[{flag}] {name}  ->  {cal['id']}  ({cal.get('accessRole')})")


def _get_all_events(time_min: datetime, time_max: datetime) -> list:
    creds = _get_credentials()
    service = build("calendar", "v3", credentials=creds)

    calendar_list = service.calendarList().list().execute()
    all_events = []

    for calendar in calendar_list.get("items", []):
        cal_id = calendar["id"]

        if _is_excluded(calendar):
            print(f"[CALENDAR] Excluding {cal_id}")
            continue

        try:
            result = service.events().list(
                calendarId=cal_id,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            all_events.extend(result.get("items", []))
        except Exception as e:
            print(f"[CALENDAR] Skipping calendar {cal_id}: {e}")
            continue

    def sort_key(e):
        start = e.get("start", {})
        return start.get("dateTime", start.get("date", ""))

    all_events.sort(key=sort_key)
    return all_events


def get_today_events() -> list:
    now = datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return _get_all_events(_local_day_to_utc(start), _local_day_to_utc(end))


def get_tomorrow_events() -> list:
    now = datetime.now()
    start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return _get_all_events(_local_day_to_utc(start), _local_day_to_utc(end))


def format_events(events: list) -> str:
    if not events:
        return "nothing scheduled"

    parts = []
    seen = set()

    for e in events:
        title = e.get("summary", "Untitled")
        start = e.get("start", {})

        if "dateTime" in start:
            dt = datetime.fromisoformat(start["dateTime"])
            if dt.tzinfo is not None:
                dt = dt.astimezone()
            label = f"{dt.strftime('%-I:%M%p').lower()} - {title}"
        else:
            label = title

        if label not in seen:
            seen.add(label)
            parts.append(label)

    return "\n".join(parts)
