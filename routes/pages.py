"""
Static pages for api.alcoberlabs.xyz, needed so Google's OAuth consent
screen can be published to Production with the calendar.readonly scope.

Add to the Pi project as routes/pages.py, then in your main app file:

    from routes.pages import router as pages_router
    app.include_router(pages_router)

(Same pattern you already use for the notion router.)
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
router = APIRouter()


_HOMEPAGE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>webhook-server</title></head>
<body style="font-family: sans-serif; max-width: 640px; margin: 40px auto; line-height: 1.5;">
  <h1>webhook-server</h1>
  <p>
    This is a personal automation service that syncs Ralph's Notion
    workspace with Google Calendar and OpenAI to keep a few status
    blocks up to date. It is not a public product and has no signup.
  </p>
  <p><a href="/privacy">Privacy Policy</a></p>
</body>
</html>
"""

_PRIVACY_HTML = """
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Privacy Policy — webhook-server</title></head>
<body style="font-family: sans-serif; max-width: 640px; margin: 40px auto; line-height: 1.5;">
  <h1>Privacy Policy</h1>
  <p>Last updated: 2026-06-19</p>

  <p>
    webhook-server is a personal, single-user automation tool built and
    operated by Ralph Alcober. It is not distributed to or used by the
    public.
  </p>

  <h2>What data is accessed</h2>
  <p>
    This service requests read-only access to one Google Calendar
    account (<code>calendar.readonly</code> scope) belonging solely to
    the developer. It is used only to read event start times and
    titles in order to display them inside the developer's own Notion
    workspace.
  </p>

  <h2>How data is used</h2>
  <p>
    Calendar event data is read on demand, summarized, and written
    into Notion callout blocks owned by the developer. It is not
    shared with any third party, sold, or used for advertising.
  </p>

  <h2>Data retention</h2>
  <p>
    No calendar data is stored persistently by this service beyond
    what is needed to render the current Notion block. OAuth tokens
    are stored locally on the developer's own server and used only to
    refresh access to the developer's own calendar.
  </p>

  <h2>Contact</h2>
  <p>
    Questions about this service can be directed to
    alcoberralphael@gmail.com.
  </p>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse)
async def homepage():
    return _HOMEPAGE_HTML


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_policy():
    return _PRIVACY_HTML