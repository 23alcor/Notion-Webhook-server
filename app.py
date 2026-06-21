from dotenv import load_dotenv
load_dotenv()

import os
print("OPENAI_API_KEY present:", bool(os.getenv("OPENAI_API_KEY")))

from fastapi import FastAPI, Request
from pydantic import BaseModel

app = FastAPI()

from routes.pages import router as pages_router
app.include_router(pages_router)

from routes.notion import router as notion_router
app.include_router(notion_router)

@app.get("/health")
def health():
    return {"status": "ok"}

class WebhookPayload(BaseModel):
    item: str

@app.post("/webhook")
async def webhook(request: Request):
    payload = await request.json()
    print("Received payload:")
    print(payload)
    return {"status": payload}
