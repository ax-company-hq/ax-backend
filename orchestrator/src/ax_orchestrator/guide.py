# ruff: noqa: E501  (문서 본문 문자열)
"""부서별 연동 안내서 생성. plan과 가짜 부서를 실제로 돌려 호출 입력 예시를 뽑는다.

문서는 `docs/notes/orchestrator/department-guide.md`에 있고, 테스트가 이 생성 결과와 같은지
확인한다. plan이 바뀌면 `python orchestrator/scripts/make_department_guide.py`로 다시 만든다.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime

from ax_orchestrator.engine import Engine
from ax_orchestrator.mock.fake_departments import FakeDepartments
from ax_orchestrator.models import Incident
from ax_orchestrator.plans.scenario_a import scenario_a
from ax_orchestrator.registry import Registry

DEPTS = ["infra", "data", "planning", "marketing", "support", "finance", "hr"]
FIXED_AT = datetime(2026, 10, 16, 1, 0, 0, tzinfo=UTC)

HEADER = """# 부서별 연동 안내서 (총괄이 내 skill을 부르는 방식)

> 이 문서는 `orchestrator/scripts/make_department_guide.py`가 plan을 실제로 돌려 만든다. 직접 고치지 않는다.
> 입력 예시는 가짜 부서로 돌린 값이다(실제 값은 앞 단계의 실제 결과로 채워진다).

## 내 에이전트를 총괄 목업에 붙여 보는 법
1. 내 에이전트를 로컬에서 띄운다(예: 재무 `8105`).
2. 총괄을 띄운다. 빈 포트를 찾아 목 모드로 뜨고 브라우저가 열린다(8000번을 다른 서비스가 써도 안전).
```
uv run python -m ax_orchestrator
```
   내 부서만 실제로 붙이려면(나머지는 가짜) 부서를 지정한다.
```
ORCHESTRATOR_REAL_DEPTS=finance uv run python -m ax_orchestrator
```
   - 터미널에 나온 `http://localhost:<포트>/demo`를 연다. 포트를 직접 정하려면 `--port 8002`, 브라우저를 안 열려면 `--no-browser`.
   - 주소는 `AX_<부서>_URL`(없으면 기본 포트 표), 여러 부서는 쉼표로(`hr,finance`).
   - 내 에이전트도 목 모드(`AGENT_TEMPLATE_MOCK=1`)면 토큰 검증이 맞는다.
3. `/demo`에서 장애를 주입한다. 부서 카드에 `실제`/`목` 표시가 나오고, 내 skill 호출이 사건 흐름에 보인다.
4. `GET /api/registry`로 내 카드가 제대로 읽혔는지 확인한다(`unavailable`에 내 부서가 있으면 카드 수집 실패).

## 이벤트를 총괄에 보내 보는 법 (발행하는 부서용)
총괄이 구독하는 이벤트와 판정은 아래와 같다. 실제 전달은 Redis Streams(agent-template TODO)로 바뀌지만 판정 규칙은 같다. 지금은 개발용 입구 `POST /api/events`에 `EventEnvelope` JSON을 보내 확인한다.

| 이벤트 | 총괄의 판정 |
|---|---|
| `shop.degraded`, `infra.anomaly_detected` | 사건 열기. 같은 `component`의 열린 사건이 있으면 합침 |
| `metric.anomaly` | `data.severity`가 high·critical이면 사건, 아니면 기록만 |
| `shop.recovered`, `support.inquiry_received`, `support.inquiry_surge` | 사건 아님. 열린 사건이 있으면 근거로 기록 |
| 그 밖의 이벤트 | 무시 |

```
curl -X POST localhost:<포트>/api/events -H 'Content-Type: application/json' -d '{
  "event_id": "evt_demo01", "type": "shop.degraded", "occurred_at": "2026-10-16T01:00:00Z",
  "source": "shop-api", "idempotency_key": "evt_demo01",
  "data": {"component": "payment", "metric_code": "error_rate_5xx", "value": 0.42}}'
```
응답 `data.outcome`: `opened`(새 사건) · `merged`(열린 사건에 합침) · `noted`(기록만) · `ignored`(소관 아님) · `duplicate`(같은 `event_id`). 사건은 `GET /api/incidents`, `/api/incidents/{context_id}`와 `/demo`에서 본다.

## 총괄의 공통 규칙
- 모든 요청 `meta.path`는 `["orchestrator"]`로 시작, `idempotency_key = <context_id>:<skill>:<단계 id>`, `deadline`은 호출 시각 + 30초.
- 실패(`failed`)는 2번, 마감 초과(`canceled`)는 1번 재시도한다. `rejected`(`FORBIDDEN_SKILL`·`HOP_LIMIT`·소관 아님)는 같은 skill을 가진 다른 부서로 이관한다.
- `input-required`: `dept_input`이면 `output.question = {skill, input}`을 총괄이 해당 부서에 물어 답을 `input.answers`로 넣어 다시 부른다. `human_input`이면 사람 승인을 기다리고(총괄은 승인하지 않음), 마감이 지나면 자동 거절로 처리한다.
- 카드에 없는 skill, 총괄이 요청할 수 없는 skill(`allowed_requesters`)은 "확인할 수 없음"으로 건너뛴다.
- 보상·쿠폰은 총괄이 부르지 않는다(#228 결정 A). 고객지원이 문의 접수 때 처리하고 마케팅에 직접 요청한다.

"""


NL = "\n"
NOTES = {
    "infra": (
        "> 확인 필요: `rollback_deployment`의 `to_version`은 진단 결과(`SuspectDeployment`)에 이전 버전이 "
        "없어 출처가 정해지지 않았다. 지금 목은 `previous_version`을 가정한다. infra가 어디서 정할지 알려 주세요."
    ),
    "support": (
        "총괄이 부르는 skill이 없다. 문의 접수 때 자동응답·보상 판정·마케팅 쿠폰 요청은 고객지원이 직접 "
        "한다(#228 결정 A). 총괄은 `support.inquiry_received`·`support.inquiry_surge` 이벤트를 구독만 한다."
    ),
    "finance": (
        "> 재무는 영향 숫자를 data에서 직접 조회하고 보상액은 자기 장부에서 센다. 총괄은 구간과 "
        "`context_id`만 준다. `verify_amount`는 총괄이 부르지 않는다(요청 가능 부서가 safety뿐)."
    ),
}


async def _run() -> tuple[Incident, FakeDepartments, list]:
    fakes = FakeDepartments()
    registry = Registry()
    await registry.load(DEPTS, fakes.fetch_card)
    engine = Engine(registry, fakes, hold_timeout=0.1)
    plan = scenario_a()
    inc = Incident(
        "ctx_inc_guide", "scenario_a", "payment 장애 감지", "payment", opened_at=FIXED_AT
    )
    await engine.run(inc, plan)
    return inc, fakes, plan.steps


def _example(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)[:260]


async def render() -> str:
    import ax_orchestrator.plans.scenario_a as scenario

    original_now = scenario.now
    scenario.now = lambda: FIXED_AT  # 정상화 공지의 resolved_at을 고정
    try:
        inc, fakes, steps = await _run()
    finally:
        scenario.now = original_now
    key_to_step = {s.id: s for s in steps}
    sections: dict[str, list[str]] = defaultdict(list)
    for step_id, run in inc.steps.items():
        step = key_to_step[step_id]
        if run.dept is None:
            continue
        req = next(
            r
            for d, r in fakes.calls
            if d == run.dept and r.meta.idempotency_key.endswith(f":{step_id}")
        )
        after = (
            ", ".join((*step.after, *(f"{a}(끝나면)" for a in step.also_after)))
            or "없음 (사건 시작 즉시)"
        )
        row = (
            f"| `{run.skill}` | {run.grade} | {step.label} | {after} | "
            f"{'예' if step.required else '아니오'} | `{_example(req.input)}` |"
        )
        sections[run.dept].append(row)
    out = [HEADER]
    for dept in DEPTS:
        rows = sections.get(dept)
        if not rows:
            note = NOTES.get(dept, "총괄이 부르는 skill이 없다.")
            out.append(f"## {dept}{NL}{NL}{note}{NL}")
            continue
        out.append(f"## {dept}\n")
        out.append("| skill | 등급 | 단계 | 선행 단계 | 필수 | 입력 예시 |")
        out.append("|---|---|---|---|---|---|")
        out.extend(rows)
        if dept in NOTES:
            out.append(f"{NL}{NOTES[dept]}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
