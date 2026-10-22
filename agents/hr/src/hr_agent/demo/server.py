"""데모 서버. 시나리오를 hr 앱의 `/a2a`로 보내고(프로세스 안에서) 결과를 모아 보여준다.

hr 앱을 그대로 통과시키므로 권한(ACL)·루프·마감·결과 검증이 실제와 같게 동작한다.
목 모드(`AGENT_TEMPLATE_MOCK=1`)로 돌아 토큰은 `mock:<요청자>`다. 운영 설정과 섞이지 않는다.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

os.environ.setdefault("AGENT_TEMPLATE_MOCK", "1")
os.environ.setdefault("HR_MOCK", "1")

from hr_agent.main import app as hr_app  # noqa: E402  (목 모드 환경변수를 먼저 정한다)

HERE = Path(__file__).resolve().parent
SCENARIO_DIR = HERE / "scenarios"
PAGE = HERE / "page.html"

demo_app = FastAPI(title="hr demo", docs_url=None, redoc_url=None)


def load_scenarios() -> dict[str, dict[str, Any]]:
    """`scenarios/*.json`을 파일 이름 순서(번호)대로 읽는다. 키는 확장자를 뺀 파일 이름."""
    items: dict[str, dict[str, Any]] = {}
    for path in sorted(SCENARIO_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["name"] = path.stem
        items[path.stem] = data
    return items


def build_request(name: str, scenario: dict[str, Any]) -> dict[str, Any]:
    """통합 에이전트가 보내는 것과 같은 모양의 A2A 요청 본문."""
    context_id = scenario["input"]["context_id"]
    body: dict[str, Any] = {
        "meta": {
            "context_id": context_id,
            "token": f"mock:{scenario['requester']}",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": f"{context_id}:record_case:{name}",
        },
        "skill": "record_case",
        "input": scenario["input"],
    }
    if scenario.get("untrusted"):
        body["untrusted"] = scenario["untrusted"]
    return body


async def send(body: dict[str, Any]) -> dict[str, Any]:
    transport = httpx.ASGITransport(app=hr_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://hr") as client:
        response = await client.post("/a2a", json=body)
    return response.json()


def judge(scenario: dict[str, Any], result: dict[str, Any]) -> bool:
    """시나리오의 `expect`(status, outcome)와 실제 결과가 맞는지."""
    expect = scenario.get("expect", {})
    if result.get("status") != expect.get("status"):
        return False
    return "outcome" not in expect or result.get("outcome") == expect["outcome"]


async def run_scenario(name: str, repeat: int = 2) -> dict[str, Any]:
    scenarios = load_scenarios()
    if name not in scenarios:
        raise HTTPException(404, f"없는 시나리오: {name}")
    scenario = scenarios[name]
    results: list[dict[str, Any]] = []
    started = time.perf_counter()
    for _ in range(repeat):
        results.append(await send(build_request(name, scenario)))
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    outputs = [r.get("output") for r in results]
    return {
        "name": name,
        "title": scenario["title"],
        "requester": scenario["requester"],
        "expect": scenario.get("expect", {}),
        "passed": all(judge(scenario, r) for r in results),
        "deterministic": all(o == outputs[0] for o in outputs),
        "repeat": repeat,
        "elapsed_ms": elapsed_ms,
        "request": build_request(name, scenario),
        "result": results[0],
    }


class RunRequest(BaseModel):
    name: str
    repeat: int = 2


@demo_app.get("/", response_class=HTMLResponse)
async def page() -> str:
    return PAGE.read_text(encoding="utf-8")


@demo_app.get("/api/scenarios")
async def scenarios() -> list[dict[str, Any]]:
    return [
        {k: v for k, v in s.items() if k in ("name", "title", "description", "requester", "expect")}
        for s in load_scenarios().values()
    ]


@demo_app.post("/api/run")
async def run(req: RunRequest) -> dict[str, Any]:
    return await run_scenario(req.name, max(1, min(req.repeat, 5)))


@demo_app.post("/api/run-all")
async def run_all() -> dict[str, Any]:
    runs = [await run_scenario(name) for name in load_scenarios()]
    return {
        "passed": sum(1 for r in runs if r["passed"] and r["deterministic"]),
        "total": len(runs),
        "runs": runs,
    }
