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
            "rich_text": rich_text
        }
    }

    with httpx.Client(timeout=20.0) as client:
        r = client.patch(url, headers=_notion_headers(), json=body)
        r.raise_for_status()
        return r.json()

def update_deadline_text():
    try:
        todo_id = _require_env("NOTION_TODO_ID")
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    
    todo_database = query_database(todo_id)
    parsed_todo = parse_todo(
        todo_database,
        only_inbox=True,
        only_undone=True,
    )
    deadline_rich_text = build_deadlines_callout(parsed_todo)
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

_last_important_things_update = 0

async def _maybe_update_important_things():
    """Debounced important things update (20 second minimum)."""
    global _last_important_things_update
    now = time.time()
    if now - _last_important_things_update < 20:
        return
    
    _last_important_things_update = now
    
    todos = await parse_todo(await query_database(os.getenv("NOTION_TODO_ID")))
    overdue = parse_overdue_tasks(todos)
    week_tasks = parse_week_tasks(todos)
    projects = parse_projects_with_action_dates()
    recommendation = ai_recommend_work(overdue, week_tasks, projects)
    
    await update_important_things_text(overdue, week_tasks, projects, recommendation)


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
        _maybe_update_important_things()

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


@router.get("/test")
async def test_webhook():
    try:
        database_id = _require_env("NOTION_DATABASE_ID")
        home_id = _require_env("NOTION_HOME_PAGE_ID")
        todo_id = _require_env("NOTION_TODO_ID")
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    try:
        # raw_data = query_database(database_id)
        # clean_data = parse_database(raw_data)
        # home_page = read_page(home_id)
        # blocks = get_page_blocks(home_id)
        # parsed_blocks = parse_blocks_readable(blocks)

        update_deadline_text()
        
        parsed_todo = "Chicken Butt"

        return parsed_todo
    except requests.exceptions.ReadTimeout as exc:
        raise HTTPException(
            status_code=504,
            detail="Notion request timed out. Try again in a few seconds.",
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Notion request failed: {exc}",
        ) from exc

# Parse overdue tasks (Days Left ≤ 0)
def parse_overdue_tasks(todos: list[dict]) -> list[dict]:
    """Filter todos that are overdue."""
    overdue = []
    for todo in todos:
        due_date = _parse_due_date(todo.get("Date Due"))
        if due_date and (date.today() - due_date).days >= 0:
            days_left = (due_date - date.today()).days
            overdue.append({
                **todo,
                "days_left": days_left,
                "urgency": "overdue"
            })
    return sorted(overdue, key=lambda x: x["days_left"])

# Parse tasks due in next 7 days
def parse_week_tasks(todos: list[dict]) -> list[dict]:
    """Filter todos due in the next 7 days (excluding overdue)."""
    week = []
    for todo in todos:
        due_date = _parse_due_date(todo.get("Date Due"))
        if due_date:
            days_left = (due_date - date.today()).days
            if 0 < days_left <= 7:
                week.append({
                    **todo,
                    "days_left": days_left,
                    "urgency": "week"
                })
    return sorted(week, key=lambda x: x["days_left"])

# Query projects database
def query_projects() -> list[dict]:
    """Query the projects database."""
    projects_db_id = os.getenv("NOTION_PROJECTS_ID")
    if not projects_db_id:
        return []
    
    try:
        pages = query_database(projects_db_id)
        return parse_database(pages)  # Reuse existing parser
    except Exception as e:
        print(f"Error querying projects: {e}")
        return []



def parse_projects_with_action_dates() -> list[dict]:

    """Query projects and extract tasks with action dates."""
    projects_db_id = os.getenv("NOTION_PROJECTS_ID")  # 2d4782096a6b805c9557fd1ff74f260f
    if not projects_db_id:
        return []
    
    try:
        projects_pages = query_database(projects_db_id)
        projects_data = parse_database(projects_pages)
        
        result = []
        for project in projects_data:
            project_name = project.get("Name", "Unknown")
            project_status = project.get("Status", "Unknown")
            
            # Extract tasks from the project
            tasks = project.get("Tasks", [])
            
            # If Tasks is a list of dicts (child pages with properties)
            if isinstance(tasks, list):
                for task in tasks:
                    if isinstance(task, dict):
                        task_name = task.get("Name", "Unnamed task")
                        action_date = task.get("Action Date")
                    else:
                        # If it's just a string, use it as the task name
                        task_name = str(task)
                        action_date = None
                    
                    # If no action date, treat as due today
                    parsed_date = _parse_due_date(action_date) if action_date else date.today()
                    days_left = (parsed_date - date.today()).days
                    
                    result.append({
                        "project": project_name,
                        "project_status": project_status,
                        "task_name": task_name,
                        "action_date": parsed_date.isoformat(),
                        "days_left": days_left
                    })
        
        return result
    except Exception as e:
        print(f"Error querying projects with tasks: {e}")
        return []
    """Query projects and extract project tasks with action dates."""
    projects_db_id = os.getenv("NOTION_PROJECTS_ID")  # 2d4782096a6b805c9557fd1ff74f260f
    if not projects_db_id:
        return []
    
    try:
        projects_pages = query_database(projects_db_id)
        projects_data = parse_database(projects_pages)
        
        result = []
        for project in projects_data:
            project_name = project.get("Name", "Unknown")
            project_status = project.get("Status", "Unknown")
            
            # Extract tasks from the project
            # Adjust field name based on your actual column name
            tasks = project.get("Tasks", [])
            
            for task in tasks:
                # If task is a dict with properties
                if isinstance(task, dict):
                    action_date = task.get("Action Date")
                    task_name = task.get("Name", "Unnamed task")
                else:
                    # If it's just a string/ID, we'd need to fetch the task separately
                    action_date = None
                    task_name = str(task)
                
                # If no action date, treat as due today
                if not action_date:
                    action_date = date.today().isoformat()
                else:
                    action_date = _parse_due_date(action_date).isoformat() if _parse_due_date(action_date) else date.today().isoformat()
                
                result.append({
                    "project": project_name,
                    "project_status": project_status,
                    "task_name": task_name,
                    "action_date": action_date,
                    "days_left": (datetime.fromisoformat(action_date).date() - date.today()).days
                })
        
        return result
    except Exception as e:
        print(f"Error querying projects with tasks: {e}")
        return []
    
def ai_recommend_work(overdue: list[dict], week_tasks: list[dict], projects: list[dict]) -> str:
    """Use AI to recommend what to work on based on priorities."""
    from ai.notion_ai import ai_think
    
    prompt = f"""
        Based on these priorities, recommend what I should work on right now:

        **OVERDUE TASKS (Must do first):**
        {json.dumps(overdue, indent=2) if overdue else "None"}

        **TASKS DUE THIS WEEK:**
        {json.dumps(week_tasks, indent=2) if week_tasks else "None"}

        **PROJECT TASKS (with action dates):**
        {json.dumps(projects, indent=2) if projects else "None"}

        Give a brief, actionable recommendation on what to prioritize next. Include:
        1. What should be done immediately (overdue items)
        2. What's important this week
        3. Which project to focus on based on all the above
        """
    
    recommendation = ai_think(prompt)
    return recommendation

def build_important_things_callout(overdue: list[dict], week_tasks: list[dict], projects: list[dict], recommendation: str) -> list[dict]:
    """Build rich text for Important Things Today callout block."""
    rich_text = []
    
    # OVERDUE SECTION (Red - highest priority)
    if overdue:
        rich_text.append({
            "type": "text",
            "text": {"content": "🔴 OVERDUE\n"},
            "annotations": {"bold": True, "color": "red"}
        })
        for task in overdue:
            task_name = task.get("Name", "Unnamed")
            days = task.get("days_left", 0)
            content = f"  • {task_name}"
            if days < 0:
                content += f" ({abs(days)} days overdue)"
            content += "\n"
            rich_text.append({
                "type": "text",
                "text": {"content": content},
                "annotations": {"color": "red"}
            })
    
    # THIS WEEK SECTION (Orange)
    if week_tasks:
        if rich_text:  # Add spacing
            rich_text.append({"type": "text", "text": {"content": "\n"}})
        
        rich_text.append({
            "type": "text",
            "text": {"content": "🟠 THIS WEEK\n"},
            "annotations": {"bold": True, "color": "orange"}
        })
        for task in week_tasks[:5]:  # Limit to top 5
            task_name = task.get("Name", "Unnamed")
            days = task.get("days_left", 0)
            content = f"  • {task_name} ({days}d left)\n"
            rich_text.append({
                "type": "text",
                "text": {"content": content},
                "annotations": {"color": "orange"}
            })
    
    # AI RECOMMENDATION SECTION
    if recommendation:
        if rich_text:
            rich_text.append({"type": "text", "text": {"content": "\n"}})
        
        rich_text.append({
            "type": "text",
            "text": {"content": "💡 AI SAYS: "},
            "annotations": {"bold": True, "color": "blue"}
        })
        rich_text.append({
            "type": "text",
            "text": {"content": recommendation},
            "annotations": {"italic": True, "color": "default"}
        })
    
    # If nothing to show
    if not rich_text:
        rich_text.append({
            "type": "text",
            "text": {"content": "✅ All clear! No overdue tasks or urgent items."},
            "annotations": {"color": "green", "italic": True}
        })
    
    return rich_text

async def update_important_things_text(overdue: list[dict], week_tasks: list[dict], projects: list[dict], recommendation: str, block_id: str | None = None):
    """Update the Important Things Today callout block."""
    if block_id is None:
        block_id = os.getenv("NOTION_IMPORTANT_THINGS_BLOCK_ID")
    
    if not block_id:
        print("Warning: NOTION_IMPORTANT_THINGS_BLOCK_ID not set")
        return
    
    rich_text = build_important_things_callout(overdue, week_tasks, projects, recommendation)
    await change_deadline_text(rich_text, block_id=block_id)

@router.get("/test-important-things")
async def test_important_things():
    """Test endpoint for Important Things Today functionality."""
    try:
        todos = parse_todo(await query_database(os.getenv("NOTION_TODO_ID")))
        overdue = parse_overdue_tasks(todos)
        week_tasks = parse_week_tasks(todos)
        projects = parse_projects_with_action_dates()
        recommendation = ai_recommend_work(overdue, week_tasks, projects)
        
        # Build and update the block
        await update_important_things_text(overdue, week_tasks, projects, recommendation)
        
        return {
            "status": "success",
            "overdue_count": len(overdue),
            "week_count": len(week_tasks),
            "projects_count": len(projects),
            "recommendation": recommendation
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
