"""ax-dashboard-api 서버 진입점."""

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ax_dashboard_api.envelope import install_error_handlers
from ax_dashboard_api.routes import router

DEFAULT_CORS_ORIGINS = "http://localhost:3001,http://127.0.0.1:3001"

app = FastAPI(title="ax-dashboard-api")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip()
        for o in (
            os.getenv("DASHBOARD_CORS_ORIGINS") or DEFAULT_CORS_ORIGINS
        ).split(",")
        if o.strip()
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)
install_error_handlers(app)
app.include_router(router)


@app.get("/health")
def health() -> dict:
    return {"data": {"status": "ok"}, "meta": {}}
