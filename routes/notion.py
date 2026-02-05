import os
import requests

from fastapi import APIRouter, Request, HTTPException

from ai.notion_ai import ai_think

router = APIRouter()

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


def query_database(database_id):
    url = f"https://api.notion.com/v1/databases/{database_id}/query"

    payload = {
        "filter": {
            "and": [
                {"property": "Done", "checkbox": {"equals": False}},
                {"property": "Status", "status": {"equals": "Inbox"}},
            ]
        }
    }

    response = requests.post(url, headers=_notion_headers(), json=payload)
    response.raise_for_status()
    return response.json().get("results", [])


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


def read_page(page_id: str):
    """
    Fetch a Notion page by ID.
    Returns parsed JSON if successful.
    Raises detailed error if request fails.
    """

    url = f"https://api.notion.com/v1/blocks/{page_id}/children"

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


def change_deadline_text(text, block_id: str | None = None):
    block_id = block_id or _require_env("NOTION_DEADLINE_BLOCK_ID")
    url = f"https://api.notion.com/v1/blocks/{block_id}"

    body = {
        "callout": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {"content": f"{text}"},
                }
            ]
        }
    }

    r = requests.patch(url, headers=_notion_headers(), json=body)
    r.raise_for_status()
    return r.json()


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

    print(data)

    event_type = data["type"]
    if event_type == "page.created":
        title = load["properties"]["Name"]["title"][0]["plain_text"]
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
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    raw_data = query_database(database_id)
    clean_data = parse_database(raw_data)
    home_page = read_page(home_id)
    blocks = get_page_blocks(home_id)
    parsed_blocks = parse_blocks_readable(blocks)

    change_deadline_text("this text has been diddy'd AGAIN")

    return blocks
