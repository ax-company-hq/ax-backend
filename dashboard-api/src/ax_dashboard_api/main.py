"""ax-dashboard-api 서버 진입점."""

from fastapi import FastAPI

from ax_dashboard_api.envelope import install_error_handlers
from ax_dashboard_api.routes import router

app = FastAPI(title="ax-dashboard-api")
install_error_handlers(app)
app.include_router(router)


@app.get("/health")
def health() -> dict:
    return {"data": {"status": "ok"}, "meta": {}}
