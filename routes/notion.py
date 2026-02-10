import os
import time
import requests

from fastapi import APIRouter, Request, HTTPException

from ai.notion_ai import ai_think
from ai.openai_client import build_deadlines_callout

router = APIRouter()

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
    timeout = (5, 30)
    max_retries = 3

    while True:
        page_payload = dict(payload)
        if next_cursor:
            page_payload["start_cursor"] = next_cursor

        data = None
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.post(
                    url,
                    headers=_notion_headers(),
                    json=page_payload,
                    timeout=timeout,
                )
                response.raise_for_status()
                data = response.json()
                break
            except requests.exceptions.ReadTimeout:
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
        response = requests.get(url, headers=_notion_headers(), timeout=10)

        # Raise exception if status code is not 200
        response.raise_for_status()

        return response.json()

    except requests.exceptions.HTTPError as http_err:
        print(f"[HTTP ERROR] {response.status_code} - {response.text}")
        raise http_err

    except requests.exceptions.RequestException as err:
        print(f"[REQUEST ERROR] {err}")
        raise err


def update_page_text(page_id, text):
    url = f"https://api.notion.com/v1/pages/{page_id}"

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

    r = requests.patch(url, headers=_notion_headers(), json=body)
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

    r = requests.patch(url, headers=_notion_headers(), json=body)
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

# endregion

def get_page_blocks(page, max_depth=None):
    page_id = page.get("id") if isinstance(page, dict) else page
    if not page_id:
        raise ValueError("Missing Notion page id.")

    session = requests.Session()

    def fetch_children(block_id):
        url = f"https://api.notion.com/v1/blocks/{block_id}/children"
        blocks = []
        start_cursor = None
        while True:
            params = {"start_cursor": start_cursor} if start_cursor else None
            response = session.get(url, headers=_notion_headers(), params=params, timeout=10)
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
    data = await request.json()
    page_id = data["entity"]["id"]
    load = read_page(page_id)
    
    # This section updates the deadline
    update_deadline_text()

    print(data)

    event_type = data["type"]
    if event_type == "page.created":
        title_items = load.get("properties", {}).get("Name", {}).get("title", [])
        title = title_items[0].get("plain_text", "") if title_items else ""
        if not title:
            title = "Untitled"
        print("New page created.")

        review = ai_think(title)

        update_page_text(page_id, review)
        print("Updated the page.")
    else:
        print("Page edited but not created, skipping any actions.")

    return {"status": "ok"}


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
