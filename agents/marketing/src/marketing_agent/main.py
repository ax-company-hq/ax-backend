"""ax-agent-marketing 서버 진입점."""

from fastapi import FastAPI

app = FastAPI(title="ax-agent-marketing")


@app.get("/health")
def health() -> dict:
    return {"data": {"status": "ok"}, "meta": {}}
