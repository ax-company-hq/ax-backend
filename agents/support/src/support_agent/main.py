"""ax-agent-support 서버 진입점."""

from fastapi import FastAPI

app = FastAPI(title="ax-agent-support")


@app.get("/health")
def health() -> dict:
    return {"data": {"status": "ok"}, "meta": {}}
