"""ax-memory 서버 진입점."""

from fastapi import FastAPI

app = FastAPI(title="ax-memory")


@app.get("/health")
def health() -> dict:
    return {"data": {"status": "ok"}, "meta": {}}
