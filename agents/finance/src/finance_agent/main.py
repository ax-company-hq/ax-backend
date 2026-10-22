"""ax-agent-finance 서버 진입점."""

from fastapi import FastAPI

app = FastAPI(title="ax-agent-finance")


@app.get("/health")
def health() -> dict:
    return {"data": {"status": "ok"}, "meta": {}}
