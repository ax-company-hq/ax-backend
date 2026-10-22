"""ax-shop-api 서버 진입점."""

from fastapi import FastAPI

app = FastAPI(title="ax-shop-api")


@app.get("/health")
def health() -> dict:
    return {"data": {"status": "ok"}, "meta": {}}
