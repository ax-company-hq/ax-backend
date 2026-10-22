"""데모·시나리오 실행기: 요청을 hr 앱의 `/a2a`로 보내고 기대 결과와 비교한다.

hr 앱을 그대로 통과시키므로 권한(ACL)·루프·마감·결과 검증이 실제와 같게 동작한다.
시나리오는 여러 단계로 이어질 수 있다(앞 단계 결과를 `${stepN.output.필드}`로 다음 단계에서 쓴다).
시나리오마다 새 `Services`(고정 시계·가상 시드)로 돌려서, 같은 시나리오를 여러 번 돌려도
결과가 같은지(결정성)를 확인할 수 있다.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from hr_agent.config import HrSettings, ManualClock
from hr_agent.main import app as hr_app
from hr_agent.services import Services, build_services, set_services

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"
_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")


def new_services() -> Services:
    """데모·시나리오용 부품: 목 모드, 고정 시계, 가상 시드."""
    return build_services(HrSettings(mock=True), clock=ManualClock())


# ---- 시나리오 읽기 ------------------------------------------------------------


def load_scenarios() -> dict[str, dict[str, Any]]:
    """`scenarios/*.json`을 파일 이름 순서(번호)대로 읽는다. 키는 확장자를 뺀 파일 이름."""
    items: dict[str, dict[str, Any]] = {}
    for path in sorted(SCENARIO_DIR.glob("*.json")):
        data = normalize(json.loads(path.read_text(encoding="utf-8")))
        data["name"] = path.stem
        items[path.stem] = data
    return items


def normalize(scenario: dict[str, Any]) -> dict[str, Any]:
    """옛 형식(단일 `record_case` 요청)을 단계 목록 형식으로 바꾼다."""
    if "steps" in scenario:
        return scenario
    step = {
        "skill": "record_case",
        "requester": scenario["requester"],
        "input": scenario["input"],
        "untrusted": scenario.get("untrusted", {}),
        "expect": scenario.get("expect", {}),
    }
    return {
        **{
            k: v
            for k, v in scenario.items()
            if k not in ("input", "untrusted", "expect", "requester")
        },
        "steps": [step],
    }


# ---- 값 치환·경로 ---------------------------------------------------------------


def dig(data: Any, path: str) -> Any:
    """`a.b.0.c` 같은 점 경로로 값을 꺼낸다. 없으면 None."""
    cur = data
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def resolve(value: Any, steps: list[dict[str, Any]]) -> Any:
    """문자열 안의 `${stepN.경로}`(N은 1부터)를 앞 단계 결과로 바꾼다."""
    if isinstance(value, dict):
        return {k: resolve(v, steps) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, steps) for v in value]
    if not isinstance(value, str):
        return value

    def lookup(expr: str) -> Any:
        head, _, rest = expr.partition(".")
        index = int(head.removeprefix("step")) - 1
        return dig(steps[index]["result"], rest) if rest else steps[index]["result"]

    whole = _PLACEHOLDER.fullmatch(value)
    if whole:
        return lookup(whole.group(1))
    return _PLACEHOLDER.sub(lambda m: str(lookup(m.group(1))), value)


# ---- 요청 보내기 -----------------------------------------------------------------


async def call_skill(
    svc: Services,
    *,
    skill: str,
    requester: str,
    input: dict[str, Any],
    untrusted: dict[str, Any] | None = None,
    context_id: str | None = None,
    key: str | None = None,
    path: list[str] | None = None,
) -> dict[str, Any]:
    """통합 에이전트가 보내는 것과 같은 모양의 A2A 요청을 hr에 보낸다."""
    set_services(svc)
    ctx = context_id or input.get("context_id") or "ctx_demo_1"
    body: dict[str, Any] = {
        "meta": {
            "context_id": ctx,
            "token": f"mock:{requester}",
            "path": path or [],
            "deadline": (svc.clock() + timedelta(seconds=30)).isoformat(),
            "idempotency_key": key or f"{ctx}:{skill}",
        },
        "skill": skill,
        "input": input,
    }
    if untrusted:
        body["untrusted"] = untrusted
    transport = httpx.ASGITransport(app=hr_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://hr") as client:
        response = await client.post("/a2a", json=body)
    return {"request": body, "result": response.json()}


# ---- 기대 결과 확인 -----------------------------------------------------------------


async def check(expect: dict[str, Any], result: dict[str, Any], svc: Services) -> list[str]:
    problems: list[str] = []
    if "status" in expect and result.get("status") != expect["status"]:
        problems.append(f"status {result.get('status')} != {expect['status']}")
    if "outcome" in expect and result.get("outcome") != expect["outcome"]:
        problems.append(f"outcome {result.get('outcome')} != {expect['outcome']}")
    for path, want in expect.get("output", {}).items():
        got = dig(result.get("output", {}), path)
        if got != want:
            problems.append(f"output.{path} = {got!r} (기대 {want!r})")
    for path in expect.get("output_has", []):
        if dig(result.get("output", {}), path) in (None, "", [], {}):
            problems.append(f"output.{path} 없음")
    if "card_count" in expect:
        count = len(await svc.memory.list_cards())
        if count != expect["card_count"]:
            problems.append(f"카드 {count}장 (기대 {expect['card_count']}장)")
    if expect.get("cards_exclude_text"):
        text = json.dumps(
            [c.model_dump(mode="json") for c in await svc.memory.list_cards()], ensure_ascii=False
        )
        for banned in expect["cards_exclude_text"]:
            if banned in text:
                problems.append(f"카드에 '{banned}'가 들어 있다")
    return problems


# ---- 시나리오 실행 -----------------------------------------------------------------


async def run_once(scenario: dict[str, Any]) -> dict[str, Any]:
    """시나리오를 새 부품으로 한 번 실행한다. 단계별 요청·결과·판정을 돌려준다."""
    svc = new_services()
    steps: list[dict[str, Any]] = []
    try:
        for number, raw in enumerate(scenario["steps"], start=1):
            step = resolve(raw, steps)
            action = step.get("action", "call")
            if action == "advance":
                svc.clock.advance(minutes=int(step["minutes"]))  # type: ignore[attr-defined]
                steps.append(
                    {"label": step.get("label", "시간 경과"), "result": {}, "problems": []}
                )
                continue
            if action == "decide_hold":
                decision = svc.gate.decide_hold(step["review_id"], bool(step["approved"]))  # type: ignore[attr-defined]
                result = {"status": "completed", "output": decision.model_dump(mode="json")}
                steps.append(
                    {"label": step.get("label", "보류 결정"), "result": result, "problems": []}
                )
                continue
            sent = await call_skill(
                svc,
                skill=step["skill"],
                requester=step["requester"],
                input=step.get("input", {}),
                untrusted=step.get("untrusted"),
                context_id=step.get("context_id"),
                key=step.get("key")
                or f"{step.get('context_id') or 'ctx_demo'}:{step['skill']}:{number}",
                path=step.get("path"),
            )
            problems = await check(step.get("expect", {}), sent["result"], svc)
            steps.append(
                {
                    "label": step.get("label", step["skill"]),
                    "request": sent["request"],
                    "result": sent["result"],
                    "problems": problems,
                }
            )
        cards = [c.model_dump(mode="json") for c in await svc.memory.list_cards()]
        return {"steps": steps, "cards": cards}
    finally:
        set_services(None)
        svc.store.close()


async def run_scenario(
    name: str, scenarios: dict[str, dict[str, Any]], repeat: int = 2
) -> dict[str, Any]:
    scenario = scenarios[name]
    runs = [await run_once(scenario) for _ in range(repeat)]
    first = runs[0]
    fingerprint = [
        [
            (s["result"].get("status"), s["result"].get("outcome"), s["result"].get("output"))
            for s in r["steps"]
        ]
        for r in runs
    ]
    passed = all(not s["problems"] for s in first["steps"])
    return {
        "name": name,
        "title": scenario["title"],
        "description": scenario.get("description", ""),
        "passed": passed,
        "deterministic": all(f == fingerprint[0] for f in fingerprint),
        "repeat": repeat,
        "steps": first["steps"],
        "cards": first["cards"],
    }
