"""Phone-to-Pi capture relay; D1 remains the application record of truth."""
import json
import os
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from routes.notion import _require_test_token

router = APIRouter()


@router.post("/jobs", dependencies=[Depends(_require_test_token)])
async def capture_job(request: Request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 32 * 1024:
            raise HTTPException(413, "Request body is too large.")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid JSON payload.") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "Body must be a JSON object.")

    url = data.get("url")
    if not isinstance(url, str) or not url.strip() or len(url) > 4000:
        raise HTTPException(400, "A job URL up to 4000 characters is required.")
    url = url.strip()
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(400, "Use a complete http or https job URL without credentials.")

    payload = {"url": url}
    for name, limit in (("company", 200), ("role", 300), ("location", 300), ("notes", 10000)):
        value = data.get(name, data.get("name", "") if name == "role" else "")
        if not isinstance(value, str) or len(value) > limit:
            raise HTTPException(400, f"{name} must be text up to {limit} characters.")
        payload[name] = value.strip()

    origin = os.getenv("COMMAND_CENTER_URL", "").rstrip("/")
    key = os.getenv("COMMAND_CENTER_CAPTURE_TOKEN", "")
    try:
        endpoint = urlsplit(origin)
        valid_origin = endpoint.scheme == "https" and endpoint.hostname and endpoint.path in ("", "/") and not endpoint.username and not endpoint.password and not endpoint.query and not endpoint.fragment
    except ValueError:
        valid_origin = False
    if not valid_origin or len(key) < 32:
        raise HTTPException(503, "Command Center capture is not configured.")

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
            response = await client.post(
                f"{origin}/api/jobs",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
            )
        if response.status_code not in (200, 201):
            raise HTTPException(502, "Command Center did not accept the job. Retry the same link; duplicates are preserved as one record.")
        result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not isinstance(result.get("duplicate"), bool):
            raise ValueError("Invalid capture response")
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "Could not confirm the save. Retry the same link; Command Center deduplicates it.") from None

    return JSONResponse(
        status_code=response.status_code,
        content={
            "status": "exists" if result["duplicate"] else "created",
            "id": result["id"],
            "duplicate": result["duplicate"],
            "url": origin,
            "title": payload["role"] or url,
        },
    )
