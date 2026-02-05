from ai.openai_client import summarize


def ai_think(prompt: str) -> str:
    return summarize(prompt)

