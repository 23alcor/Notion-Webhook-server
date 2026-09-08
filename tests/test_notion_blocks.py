"""Coverage for the block-tree walkers in routes.notion:
- get_page_blocks (mocked with respx)
- parse_blocks_readable (pure)"""
import httpx
import pytest
import respx

from routes import notion

NOTION = "https://api.notion.com/v1"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "tkn")


# ----- get_page_blocks -----

def test_get_page_blocks_missing_id_raises():
    with pytest.raises(ValueError):
        notion.get_page_blocks({})


@respx.mock
def test_get_page_blocks_paginates_top_level():
    respx.get(f"{NOTION}/blocks/pg1/children").mock(
        side_effect=[
            httpx.Response(200, json={"results": [{"id": "b1"}], "has_more": True, "next_cursor": "c1"}),
            httpx.Response(200, json={"results": [{"id": "b2"}], "has_more": False}),
        ]
    )
    blocks = notion.get_page_blocks("pg1", max_depth=0)
    assert [b["id"] for b in blocks] == ["b1", "b2"]


@respx.mock
def test_get_page_blocks_recurses_into_children():
    # parent has one child block that itself has_children=True.
    respx.get(f"{NOTION}/blocks/pg1/children").mock(
        return_value=httpx.Response(200, json={
            "results": [{"id": "b1", "has_children": True}],
            "has_more": False,
        })
    )
    respx.get(f"{NOTION}/blocks/b1/children").mock(
        return_value=httpx.Response(200, json={
            "results": [{"id": "b1a"}],
            "has_more": False,
        })
    )
    tree = notion.get_page_blocks({"id": "pg1"})
    assert tree[0]["children"][0]["id"] == "b1a"


@respx.mock
def test_get_page_blocks_max_depth_stops_recursion():
    respx.get(f"{NOTION}/blocks/pg1/children").mock(
        return_value=httpx.Response(200, json={
            "results": [{"id": "b1", "has_children": True}],
            "has_more": False,
        })
    )
    # If recursion happened it would need /blocks/b1/children — no route.
    tree = notion.get_page_blocks("pg1", max_depth=0)
    assert "children" not in tree[0]


@respx.mock
def test_get_page_blocks_uses_injected_client():
    respx.get(f"{NOTION}/blocks/pg1/children").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "b1"}], "has_more": False})
    )
    client = httpx.Client(timeout=5.0)
    try:
        blocks = notion.get_page_blocks("pg1", client=client)
        assert blocks[0]["id"] == "b1"
    finally:
        client.close()


# ----- parse_blocks_readable -----

def test_parse_blocks_readable_heading_and_paragraph():
    blocks = [
        {"type": "heading_1", "heading_1": {"rich_text": [{"plain_text": "H"}]},
         "parent": {"page_id": "pg"}},
        {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "body"}]},
         "parent": {"page_id": "pg"}},
    ]
    out = notion.parse_blocks_readable(blocks)
    assert out[0]["text"] == "H" and out[0]["type"] == "heading_1"
    assert out[1]["text"] == "body"


def test_parse_blocks_readable_equation_callout_image_column():
    blocks = [
        {"type": "equation", "equation": {"expression": "x^2"}, "parent": {}},
        {"type": "callout", "callout": {"rich_text": [{"plain_text": "note"}], "color": "yellow"}, "parent": {}},
        {"type": "image", "image": {"caption": [{"plain_text": "cap"}], "file": {"url": "https://x/i.png"}}, "parent": {}},
        {"type": "image", "image": {"caption": [], "external": {"url": "https://y/i.png"}}, "parent": {}},
        {"type": "child_database", "child_database": {"title": "DB"}, "parent": {}},
        {"type": "column", "column": {"width_ratio": 0.5}, "parent": {}},
        {"type": "unsupported", "parent": {}},
    ]
    out = notion.parse_blocks_readable(blocks)
    assert out[0]["expression"] == "x^2"
    assert out[1]["text"] == "note" and out[1]["color"] == "yellow"
    assert out[2]["url"] == "https://x/i.png" and out[2]["caption"] == "cap"
    assert out[3]["url"] == "https://y/i.png"
    assert out[4]["title"] == "DB"
    assert out[5]["width_ratio"] == 0.5
    assert out[6]["type"] == "unsupported"


def test_parse_blocks_readable_walks_children():
    blocks = [{
        "type": "paragraph",
        "paragraph": {"rich_text": [{"plain_text": "parent"}]},
        "parent": {"page_id": "pg"},
        "children": [
            {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "child"}]}, "parent": {}},
        ],
    }]
    out = notion.parse_blocks_readable(blocks)
    assert out[0]["children"][0]["text"] == "child"


def test_parse_blocks_readable_none_input_returns_empty_list():
    assert notion.parse_blocks_readable(None) == []
