from openai import OpenAI
import os
from datetime import date, datetime

DEVELOPER_MODE = False

def get_openai_client():
    if not DEVELOPER_MODE:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY missing")
    else:
        key = os.getenv("OPENAI_API_KEY_PROJECT")
        if not key:
            raise RuntimeError("OPENAI_API_KEY missing")
    return OpenAI(api_key=key)

# region SYSTEM PROMPT
prompt = """
You are an assistant that interprets short personal notes written for future reference, planning, or decision-making.

Your job is to:
1. Infer the intent of the note (idea, reminder, purchase, or general).
2. Produce a clear, helpful response that future-me can quickly understand.
3. Extract automation-friendly metadata without asking follow-up questions.

Rules:
- Do not mention AI.
- Do not ask questions.
- Do not repeat the original note verbatim.
- Be concise, calm, and practical.
- Optimize for usefulness when revisited weeks or months later.
- Make reasonable assumptions when information is missing.

Intent handling:
- If the note describes something to be done or remembered, treat it as a reminder.
- If the note describes an idea, curiosity, or research direction, treat it as an idea.
- If the note involves evaluating or buying a product or tool, treat it as a purchase.
- If urgency or immediacy is implied and no date is mentioned, assume today.

Special handling for purchases:
- Do light research to support a decision.
- Provide rough price ranges.
- Group recommendations into tiers (cheap, mid-tier, premium).
- Include at least two representative links per tier when possible.
- Offer an opinion on whether the purchase is worth making now, later, or at all.
- Suggest alternatives that could save time or money.

Output format (exactly this structure):

Context:
- 1-2 bullets explaining what this note is about and why it matters.

Key Points:
- 2-4 bullets summarizing findings or considerations.

Options:
- Cheap:
  - Name - short reason (approx. price, link)
- Mid-tier:
  - ...
- Premium:
  - ...

Recommendation:
- Clear opinion on what to do and why.

Metadata:
- intent: <idea | reminder | purchase | general>
- suggested_date: <YYYY-MM-DD or null>
"""
# endregion


def summarize(text: str) -> str:
    client = get_openai_client()
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text}
        ]
    )
    return resp.choices[0].message.content


def _parse_due_date(raw_due: str | None) -> date | None:
    if not raw_due:
        return date.today()
    raw_due = raw_due.strip()
    if not raw_due:
        return None
    try:
        return date.fromisoformat(raw_due[:10])
    except ValueError:
        try:
            return datetime.fromisoformat(raw_due.replace("Z", "+00:00")).date()
        except ValueError:
            return None

def build_deadlines_callout(items: list[dict]) -> list[dict]:
    from collections import defaultdict

    if not items:
        return [{"type": "text", "text": {"content": "No deadlines yet."}}]

    today = date.today()

    def get_days(item):
        due = _parse_due_date(item.get("due_date") or item.get("date_due"))
        return 0 if due is None else (due - today).days

    def color(d):
        return "red" if d <= 0 else "orange" if d <= 2 else "green"

    def entry(d, title):
        return {"type": "text", "text": {"content": f"{d} - {title}\n"}, "annotations": {"bold": True, "color": color(d)}}

    todos = sorted([i for i in items if i.get("source") == "todo"], key=get_days)
    tasks = [i for i in items if i.get("source") == "task"]

    rich_text = []

    for todo in todos:
        title = (todo.get("title") or todo.get("item") or "Untitled").strip()
        rich_text.append(entry(get_days(todo), title))

    by_project = defaultdict(list)
    for task in tasks:
        by_project[task.get("project_name", "Other")].append(task)

    if by_project:
        rich_text.append({"type": "text", "text": {"content": "\n"}, "annotations": {"bold": False, "color": "default"}})
        for project_name, project_tasks in sorted(by_project.items()):
            rich_text.append({"type": "text", "text": {"content": f"{project_name}\n"}, "annotations": {"bold": True, "color": "default"}})
            for task in sorted(project_tasks, key=get_days):
                rich_text.append(entry(get_days(task), task.get("title", "Untitled").strip()))

    return rich_text[:100]

def build_important_things_callout(items: list[dict]) -> list[dict]:
    from ai.google_calendar import get_today_events, get_tomorrow_events, format_events

    today = date.today()
    now = datetime.now()
    hour = now.hour

    # --- Greeting ---
    if hour < 12:
        greeting = "Good morning Ralph"
    elif hour < 18:
        greeting = "Good afternoon Ralph"
    else:
        greeting = "Day is almost over Ralph"

    # --- Calendar (fetch once) ---
    tomorrow_events = get_tomorrow_events()
    tomorrow_str = format_events(tomorrow_events)

    # --- Task counts ---
    due_count = 0
    upcoming_count = 0
    eventually_count = 0

    for item in items:
        due_raw = item.get("due_date")
        source = item.get("source", "")
        if due_raw:
            try:
                due = date.fromisoformat(due_raw[:10])
                days_left = (due - today).days
                if days_left <= 0:
                    due_count += 1
                elif days_left <= 7:
                    upcoming_count += 1
                else:
                    eventually_count += 1
            except ValueError:
                eventually_count += 1
        else:
            due_count += 1   # no-date tasks = backlog

    # --- Evening sleep recommendation ---
    sleep_line = ""
    if hour >= 18:
        client = get_openai_client()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": f"Tomorrow's schedule: {tomorrow_str}. What time should I sleep tonight to get 6-7 hours before my first event? Reply in one sentence only."
            }]
        )
        sleep_line = resp.choices[0].message.content.strip()

    # --- Build rich text ---
    rich_text = []

    def add(content, bold=False, color="default"):
        rich_text.append({
            "type": "text",
            "text": {"content": content},
            "annotations": {"bold": bold, "color": color},
        })

    add(greeting, bold=True)
    add("\n\n")

    if hour < 18:
        today_events = get_today_events()
        today_str = format_events(today_events)
        add("Here's what you have today.\n")
        add(today_str)
    else:
        add("Here's what you have tomorrow.\n")
        add(tomorrow_str)

    add("\n\n")
    add(f"You have {due_count} due tasks.\n", bold=True, color="red" if due_count > 0 else "default")
    add(f"You have {upcoming_count} upcoming tasks.\n", bold=True, color="orange" if upcoming_count > 0 else "default")
    add(f"You have {eventually_count} tasks eventually.", bold=True)

    if sleep_line:
        add("\n\n")
        add(sleep_line, color="blue")

    return rich_text