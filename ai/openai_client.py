# ai/openai_client.py
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
- 1–2 bullets explaining what this note is about and why it matters.

Key Points:
- 2–4 bullets summarizing findings or considerations.

Options:
- Cheap:
  - Name – short reason (approx. price, link)
  - Name – short reason (approx. price, link)
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


def build_deadlines_callout(todos: list[dict]) -> list[dict]:
    if not todos:
        return [{"type": "text", "text": {"content": "No deadlines yet."}}]

    def _parse_due_date(raw_due: str | None) -> date | None:
        if not raw_due:
            return None
        raw_due = raw_due.strip()
        if not raw_due:
            return None

        # Handles "YYYY-MM-DD" and Notion datetime strings like "YYYY-MM-DDTHH:MM:SS.sssZ".
        try:
            return date.fromisoformat(raw_due[:10])
        except ValueError:
            try:
                return datetime.fromisoformat(raw_due.replace("Z", "+00:00")).date()
            except ValueError:
                return None

    today = date.today()
    ranked: list[tuple[int, int, str]] = []

    for idx, todo in enumerate(todos):
        item = (todo.get("item") or "Untitled").strip() or "Untitled"
        due = _parse_due_date(todo.get("date_due"))
        if due is None:
            # Put unknown due dates after known ones while preserving input order.
            ranked.append((1, idx, item))
            continue
        days_left = (due - today).days
        ranked.append((0, days_left, item))

    ranked.sort(key=lambda x: (x[0], x[1]))

    rich_text: list[dict] = []
    for idx, (group, value, item) in enumerate(ranked):
        has_known_due = group == 0
        if has_known_due:
            urgency_days = value
            left = f"{urgency_days} Days Left"
        else:
            urgency_days = None
            left = "Due date unclear"

        if urgency_days is None:
            urgency_color = "default"
        elif urgency_days <= 0:
            urgency_color = "red"
        elif urgency_days <= 2:
            urgency_color = "orange"
        else:
            urgency_color = "green"

        urgency_text = f"{left} - " if item else left
        rich_text.append(
            {
                "type": "text",
                "text": {"content": urgency_text},
                "annotations": {"bold": True, "color": urgency_color},
            }
        )

        if item:
            rich_text.append(
                {
                    "type": "text",
                    "text": {"content": item},
                    "annotations": {"bold": True, "color": urgency_color},
                }
            )

        if idx < len(ranked) - 1:
            rich_text.append({"type": "text", "text": {"content": "\n"}})

    return rich_text
