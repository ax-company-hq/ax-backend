"""ax-safety 서버 진입점."""

from fastapi import FastAPI

app = FastAPI(title="ax-safety")


@app.get("/health")
def health() -> dict:
    return {"data": {"status": "ok"}, "meta": {}}
