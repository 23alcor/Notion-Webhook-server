import os
import time
import httpx
import threading
from queue import Queue
from typing import Any



from fastapi import APIRouter, Request, HTTPException

from ai.notion_ai import ai_think
from ai.openai_client import build_deadlines_callout

router = APIRouter()

# Debounce deadline refreshes to avoid repeated expensive work during event bursts.
_DEADLINE_MIN_INTERVAL_SECONDS = 20.0
_deadline_lock = threading.Lock()
_last_deadline_update_monotonic = 0.0

# Event queue for background processing (separate worker thread)
_event_queue = Queue()

def _worker_thread():
    """Worker thread that processes Notion events from the queue."""
    while True:
        try:
            data = _event_queue.get()
            if data is None:  # Sentinel value to shut down
                break
            _process_notion_event(data)
        except Exception as exc:
            print(f"[WORKER ERROR] {exc}")

# Start worker thread as daemon
_worker = threading.Thread(target=_worker_thread, daemon=True)
_worker.start()

# region backend

def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} missing")
    return value


def _notion_token() -> str:
    return _require_env("NOTION_TOKEN")


def _notion_headers():
    return {
        "Authorization": f"Bearer {_notion_token()}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

# endregion

def query_database(database_id, query_payload=None):
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    payload = dict(query_payload or {})
    results = []
    next_cursor = None
    timeout = httpx.Timeout(30.0)
    max_retries = 3

    with httpx.Client(timeout=timeout) as client:
        while True:
            page_payload = dict(payload)
            if next_cursor:
                page_payload["start_cursor"] = next_cursor

            data = None
            for attempt in range(1, max_retries + 1):
                try:
                    response = client.post(
                        url,
                        headers=_notion_headers(),
                        json=page_payload,
                    )
                    response.raise_for_status()
                    data = response.json()
                    break
                except httpx.ReadTimeout:
                    if attempt == max_retries:
                        raise
                    time.sleep(0.5 * attempt)

            results.extend(data.get("results", []))

            if not data.get("has_more"):
                break
            next_cursor = data.get("next_cursor")
            if not next_cursor:
                break

    return results


def parse_database(pages):
    clean = []

    for page in pages:
        props = page.get("properties", {})

        # ---------- TITLE ----------
        title_prop = props.get("Name", {}).get("title", [])
        title = title_prop[0]["plain_text"] if title_prop else "Untitled"

        # ---------- DATE ----------
        date_obj = props.get("Date", {}).get("date")
        date = date_obj["start"] if date_obj else None

        # ---------- DONE ----------
        done = props.get("Done", {}).get("checkbox", False)

        # ---------- STATUS ----------
        status_obj = props.get("Status", {}).get("status")
        status = status_obj["name"] if status_obj else None

        # ---------- TEXT ----------
        text_prop = props.get("Text", {}).get("rich_text", [])
        text = text_prop[0]["plain_text"] if text_prop else ""

        # ---------- IMAGE ----------
        files_prop = props.get("image", {}).get("files", [])
        image_url = None

        if files_prop:
            file_obj = files_prop[0]

            # could be external or file
            if file_obj["type"] == "external":
                image_url = file_obj["external"]["url"]
            elif file_obj["type"] == "file":
                image_url = file_obj["file"]["url"]

        clean.append(
            {
                "id": page.get("id"),
                "title": title,
                "date": date,
                "done": done,
                "status": status,
                "text": text,
                "image": image_url,
            }
        )

    return clean

def parse_todo(pages, *, only_inbox=True, only_undone=True):
    clean = []

    def _plain_text(rich_items):
        if not rich_items:
            return ""
        return "".join(
            part.get("plain_text", "")
            for part in rich_items
            if isinstance(part, dict)
        )

    def _first_file_url(files):
        if not files:
            return None
        first = files[0]
        if first.get("type") == "external":
            return first.get("external", {}).get("url")
        if first.get("type") == "file":
            return first.get("file", {}).get("url")
        return None

    for page in pages:
        props = page.get("properties", {})

        title = _plain_text(props.get("Name", {}).get("title", [])) or "Untitled"

        date = props.get("Date", {}).get("date") or {}
        due_date = props.get("Date Due", {}).get("date") or {}

        done = props.get("Done", {}).get("checkbox", False)

        status_obj = props.get("Status", {}).get("status")
        status = status_obj["name"] if status_obj else None

        if only_undone and done:
            continue
        if only_inbox and status != "Inbox":
            continue

        text = _plain_text(props.get("Text", {}).get("rich_text", []))

        image_url = _first_file_url(props.get("image", {}).get("files", []))

        clean.append(
            {
                "item": title,
                "date_due": due_date.get("start"),
                "status": status,
                "text": text,
                "image": image_url,
                "date created": date.get("start")
            }
        )

    return clean


def read_page(page_id: str):
    """
    Fetch a Notion page by ID.
    Returns parsed JSON if successful.
    Raises detailed error if request fails.
    """

    url = f"https://api.notion.com/v1/pages/{page_id}"

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(url, headers=_notion_headers())

            # Raise exception if status code is not 200
            response.raise_for_status()

            return response.json()

    except httpx.HTTPStatusError as http_err:
        print(f"[HTTP ERROR] {http_err.response.status_code} - {http_err.response.text}")
        raise http_err

    except httpx.RequestError as err:
        print(f"[REQUEST ERROR] {err}")
        raise err


def update_page_text(page_id, text):
    url = f"https://api.notion.com/v1/pages/{page_id}"

    page = read_page(page_id)
    props = page.get("properties", {})
    current_due = props.get("Date Due", {}).get("date") or {}
    current_date = props.get("Date", {}).get("date") or {}


    body = {
        "properties": {
            "Text": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": f"{text}"},
                    }
                ]
            }
        }
    }
    
    if not current_due.get("start") and current_date.get("start"):
        body["properties"]["Date Due"] = {
            "date": {
                "start": current_date["start"],
            }
        }


    with httpx.Client(timeout=20.0) as client:
        r = client.patch(url, headers=_notion_headers(), json=body)
        r.raise_for_status()
        return r.json()

# region deadline code

def change_deadline_text(rich_text, block_id: str | None = None):
    block_id = block_id or _require_env("NOTION_DEADLINE_BLOCK_ID")
    url = f"https://api.notion.com/v1/blocks/{block_id}"

    body = {
        "callout": {
            "rich_text": rich_text[:100]
        }
    }

    with httpx.Client(timeout=20.0) as client:
        r = client.patch(url, headers=_notion_headers(), json=body)
        if r.status_code >= 400:
            print(f"Notion error: {r.status_code} - {r.text}")
        r.raise_for_status()
        return r.json()

def update_deadline_text():
    combined = get_combined_items()
    deadline_rich_text = build_deadlines_callout(combined)
    change_deadline_text(deadline_rich_text)
    print("Deadline was changed")


def _maybe_update_deadline_text() -> None:
    global _last_deadline_update_monotonic

    now = time.monotonic()
    with _deadline_lock:
        if now - _last_deadline_update_monotonic < _DEADLINE_MIN_INTERVAL_SECONDS:
            print("Skipped deadline update due to debounce window.")
            return
        _last_deadline_update_monotonic = now

    update_deadline_text()


def _process_notion_event(data: dict[str, Any]) -> None:
    try:
        event_type = data.get("type", "")
        entity = data.get("entity") or {}
        page_id = entity.get("id")

        if not page_id:
            print("Webhook payload missing page id; skipping.")
            return

        # Keep deadline block reasonably fresh while avoiding N updates per burst.
        _maybe_update_deadline_text()

        if event_type != "page.created":
            print("Page edited but not created, skipping page review.")
            return

        load = read_page(page_id)
        title_items = load.get("properties", {}).get("Name", {}).get("title", [])
        title = title_items[0].get("plain_text", "") if title_items else ""
        if not title:
            title = "Untitled"

        print("New page created.")
        review = ai_think(title)
        update_page_text(page_id, review)
        print("Updated the page.")
    except httpx.RequestError as exc:
        print(f"[REQUEST ERROR] webhook background task failed: {exc}")
    except Exception as exc:
        print(f"[ERROR] webhook background task failed: {exc}")

# endregion

def get_page_blocks(page, max_depth=None, client=None):
    page_id = page.get("id") if isinstance(page, dict) else page
    if not page_id:
        raise ValueError("Missing Notion page id.")

    should_close_client = False
    if client is None:
        client = httpx.Client(timeout=10.0)
        should_close_client = True

    try:
        def fetch_children(block_id):
            url = f"https://api.notion.com/v1/blocks/{block_id}/children"
            blocks = []
            start_cursor = None
            while True:
                params = {"start_cursor": start_cursor} if start_cursor else None
                response = client.get(url, headers=_notion_headers(), params=params)
                response.raise_for_status()
                data = response.json()
                results = data.get("results")
                if results:
                    blocks.extend(results)
                if not data.get("has_more"):
                    break
                start_cursor = data.get("next_cursor")
                if not start_cursor:
                    break
            return blocks

        def build_tree(blocks, depth):
            if depth == 0:
                return blocks
            next_depth = None if depth is None else depth - 1
            for block in blocks:
                if block.get("has_children"):
                    children = fetch_children(block["id"])
                    block["children"] = build_tree(children, next_depth)
            return blocks

        top_level = fetch_children(page_id)
        return build_tree(top_level, max_depth)
    finally:
        if should_close_client:
            client.close()


def parse_blocks_readable(blocks):
    def plain_text(rt_list):
        if not rt_list:
            return ""
        return "".join(
            part.get("plain_text", "") for part in rt_list if isinstance(part, dict)
        )

    def parse_block(block):
        btype = block.get("type", "unknown")
        node = {"type": btype, "page_id": block.get("parent", {}).get("page_id")}

        if btype.startswith("heading_"):
            data = block.get(btype, {})
            text = plain_text(data.get("rich_text"))
            if text:
                node["text"] = text
        elif btype == "paragraph":
            text = plain_text(block.get("paragraph", {}).get("rich_text"))
            if text:
                node["text"] = text
        elif btype == "equation":
            expr = block.get("equation", {}).get("expression")
            if expr:
                node["expression"] = expr
        elif btype == "callout":
            data = block.get("callout", {})
            text = plain_text(data.get("rich_text"))
            if text:
                node["text"] = text
            color = data.get("color")
            if color:
                node["color"] = color
        elif btype == "image":
            img = block.get("image", {})
            caption = plain_text(img.get("caption"))
            if caption:
                node["caption"] = caption
            url = img.get("file", {}).get("url") or img.get("external", {}).get("url")
            if url:
                node["url"] = url
        elif btype == "child_database":
            title = block.get("child_database", {}).get("title")
            if title:
                node["title"] = title
        elif btype == "column":
            width = block.get("column", {}).get("width_ratio")
            if isinstance(width, (int, float)):
                node["width_ratio"] = width

        children = block.get("children")
        if children:
            node["children"] = [parse_block(child) for child in children]

        return node

    return [parse_block(b) for b in (blocks or [])]


@router.post("/notion-webhook")
async def notion_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    # Handle Notion webhook verification challenge
    if "challenge" in data:
        return {"challenge": data["challenge"]}

    # Enqueue for background processing; return immediately.
    _event_queue.put(data)
    return {"status": "accepted"}

def update_important_things_text(rich_text):
    block_id = _require_env("NOTION_IMPORTANT_THINGS_BLOCK_ID")
    from ai.openai_client import build_important_things_callout

    block_id = _require_env("NOTION_IMPORTANT_THINGS_BLOCK_ID")
    items = get_combined_items()
    rich_text = build_important_things_callout(items)
    print(f"Rich text length: {len(rich_text)}")  # add this
    
    return change_deadline_text(rich_text, block_id=block_id)

def get_project_names() -> dict:
    projects_id = os.getenv("NOTION_PROJECTS_ID")
    if not projects_id:
        return {}
    try:
        pages = query_database(projects_id)
        return {
            page.get("id", ""): (page.get("properties", {}).get("Name", {}).get("title") or [{}])[0].get("plain_text", "Unnamed")
            for page in pages
        }
    except Exception as e:
        print(f"[PROJECTS] {e}")
        return {}

def parse_tasks(pages, *, only_undone=True):
    clean = []

    def _plain_text(rich_items):
        if not rich_items:
            return ""
        return "".join(
            part.get("plain_text", "")
            for part in rich_items
            if isinstance(part, dict)
        )

    for page in pages:
        props = page.get("properties", {})

        title = _plain_text(props.get("Name", {}).get("title", [])) or "Untitled"

        status_obj = props.get("Status", {}).get("status")
        status = status_obj["name"] if status_obj else None

        if only_undone and status == "Done":
            continue

        action_date = props.get("Action Date", {}).get("date") or {}
        due_date = action_date.get("start")

        description = _plain_text(props.get("Description", {}).get("rich_text", []))

        # Project relation — just grab the first linked project ID for now
        project_rel = props.get("Projects", {}).get("relation", [])
        project_id = project_rel[0]["id"] if project_rel else None

        clean.append({
            "title": title,
            "due_date": due_date,
            "status": status,
            "description": description,
            "project_id": project_id,
            "source": "task",
        })

    return clean

def get_combined_items() -> list[dict]:
    """Query todo and tasks databases, return a unified normalized list."""
    todo_id = _require_env("NOTION_TODO_ID")
    tasks_id = _require_env("NOTION_TASKS_ID")

    # --- Todo ---
    todo_pages = query_database(todo_id)
    todo_parsed = parse_todo(todo_pages, only_inbox=True, only_undone=True)
    todo_items = [
    {
        "title": item["item"],
        "due_date": item["date_due"] or item.get("date created"),  # ← fallback
        "description": item.get("text", ""),
        "source": "todo",
        "project_id": None,
    }
    for item in todo_parsed
    ]

    # --- Tasks ---
    tasks_pages = query_database(tasks_id)
    tasks_parsed = parse_tasks(tasks_pages, only_undone=True)
    project_map = get_project_names()
    task_items = [
        {
            "title": item["title"],
            "due_date": item["due_date"],
            "description": item.get("description", ""),
            "source": "task",
            "project_id": item.get("project_id"),
            "project_name": project_map.get(item.get("project_id", ""), "Other") if item.get("project_id") else "Other",
        }
        for item in tasks_parsed
    ]

    return todo_items + task_items

def update_important_things_text():
    from ai.openai_client import build_important_things_callout

    block_id = _require_env("NOTION_IMPORTANT_THINGS_BLOCK_ID")
    items = get_combined_items()
    rich_text = build_important_things_callout(items)
    change_deadline_text(rich_text, block_id=block_id)
    print("Important things was changed")

@router.get("/test")
async def test_webhook():
    try:
        from ai.google_calendar import get_tomorrow_events, format_events
        events = get_tomorrow_events()
        print(f"Tomorrow events: {[e.get('summary') for e in events]}")
        update_deadline_text()
        update_important_things_text()
        return {"status": "ok"}
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=str(exc)) from exc

@router.post("/test")
async def test_webhook_post():
    try:
        update_deadline_text()
        update_important_things_text()
        return {"status": "ok"}
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=str(exc)) from exc

@router.get("/debug-items")
async def debug_items():
    from datetime import date
    items = get_combined_items()
    today = date.today()
    
    due, upcoming, eventually, no_date = [], [], [], []
    for item in items:
        raw = item.get("due_date")
        if raw:
            try:
                d = date.fromisoformat(raw[:10])
                days = (d - today).days
                if days <= 0:
                    due.append({"title": item["title"], "days": days, "source": item["source"]})
                elif days <= 7:
                    upcoming.append({"title": item["title"], "days": days, "source": item["source"]})
                else:
                    eventually.append({"title": item["title"], "days": days, "source": item["source"]})
            except ValueError:
                no_date.append({"title": item["title"], "raw_date": raw, "source": item["source"]})
        else:
            no_date.append({"title": item["title"], "raw_date": None, "source": item["source"]})
    
    return {
        "due": due,
        "upcoming": upcoming,
        "eventually": eventually,
        "no_date": no_date,
        "counts": {"due": len(due), "upcoming": len(upcoming), "eventually": len(eventually), "no_date": len(no_date)}
    }