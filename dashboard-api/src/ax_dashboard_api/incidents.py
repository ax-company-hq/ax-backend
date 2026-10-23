"""AI 대화방용 읽기 중계: 총괄의 사건 기록과 안전 관문의 실행 기록.

브라우저가 총괄(8000)·안전 관문(8001)을 직접 부르지 않고 dashboard-api만 부르게 한다 (읽기 전용).
- 사건(`/incidents`)은 총괄 `GET /api/incidents`의 `data`를 그대로 중계한다.
- 실행 기록(`/executions`)은 안전 관문 `GET /api/safety/executions`에서 코드가 만든 필드만 고른다.
  요청 `params` 원문·`evidence`·`customer`·`report`는 넘기지 않고 금액은 `computed_amount`만 쓴다.
  결과 값(`outcome`)은 소통 탭(`communications`)과 같은 5가지다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel

from ax_agent_template import config
from ax_dashboard_api.communications import CommunicationOutcome
from ax_dashboard_api.envelope import ApiError, is_mock

TIMEOUT_SECONDS = 10.0

_OUTCOME: dict[str, CommunicationOutcome] = {
    "executed": "completed",
    "verifying": "in_progress",
    "held": "held",
    "rejected": "rejected",
    "failed": "rejected",
}


class ExecutionLine(BaseModel):
    """AI 대화방 실행 기록 한 줄. `execution_id`로 소통 탭·보류 큐 결정과 잇는다."""

    execution_id: str
    created_at: datetime
    context_id: str
    action: str
    requester: str
    grade: str
    status: str
    outcome: CommunicationOutcome
    computed_amount: int | None = None
    threshold_version: int | None = None
    hold_reason: str | None = None
    review_id: str | None = None


async def _get(target: str, path: str, params: dict[str, str] | None = None) -> Any:
    """상대 서비스의 읽기 API를 부르고 `data`를 돌려준다. 테스트에서 바꿀 수 있다."""
    url = f"{config.agent_url(target)}{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            res = await client.get(url, params=params)
    except httpx.TimeoutException as exc:
        raise ApiError(504, "UPSTREAM_TIMEOUT", f"{target}가 제때 응답하지 않았다") from exc
    except httpx.HTTPError as exc:
        raise ApiError(502, "UPSTREAM_ERROR", f"{target}에 연결하지 못했다") from exc
    if res.status_code == 404:
        raise ApiError(404, "NOT_FOUND", "찾을 수 없다")
    if res.status_code != 200:
        raise ApiError(502, "UPSTREAM_ERROR", f"{target} 응답 오류 ({res.status_code})")
    try:
        return res.json()["data"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError(502, "UPSTREAM_ERROR", f"{target} 응답 형식이 다르다") from exc


# ---------------------------------------------------------------- 목 모드 샘플
def _mock_incident() -> dict[str, Any]:
    return {
        "context_id": "ctx_inc_mock1",
        "kind": "shop_degraded",
        "title": "결제 오류율 상승",
        "component": "payment",
        "status": "waiting_human",
        "opened_at": "2026-10-22T09:00:00Z",
        "closed_at": None,
        "steps": [
            {
                "step_id": "triage",
                "skill": "get_claim",
                "label": "원인 분석",
                "dept": "support",
                "status": "completed",
                "attempts": 1,
                "note": None,
                "grade": "A",
                "started_at": "2026-10-22T09:00:00Z",
                "finished_at": "2026-10-22T09:01:00Z",
                "transferred_to": None,
                "reason_code": None,
                "output": {},
            },
            {
                "step_id": "hold_settlement",
                "skill": "hold_settlement",
                "label": "정산 보류",
                "dept": "finance",
                "status": "waiting_human",
                "attempts": 1,
                "note": None,
                "grade": "C",
                "started_at": "2026-10-22T09:02:00Z",
                "finished_at": None,
                "transferred_to": None,
                "reason_code": None,
                "output": {},
            },
        ],
        "events": [],
        "summary": None,
        "metrics": {},
        "pending_holds": [
            {"step_id": "hold_settlement", "skill": "hold_settlement", "label": "정산 보류"}
        ],
    }


def _mock_executions() -> list[ExecutionLine]:
    return [
        ExecutionLine(
            execution_id="exe_mock1",
            created_at=datetime(2026, 10, 22, 9, 2, tzinfo=UTC),
            context_id="ctx_inc_mock1",
            action="hold_settlement",
            requester="finance",
            grade="C",
            status="held",
            outcome="held",
            computed_amount=400_000,
            threshold_version=1,
            hold_reason="over_threshold",
            review_id="rev_mock1",
        ),
        ExecutionLine(
            execution_id="exe_mock2",
            created_at=datetime(2026, 10, 22, 9, 3, tzinfo=UTC),
            context_id="ctx_inc_mock1",
            action="issue_coupon",
            requester="marketing",
            grade="B",
            status="executed",
            outcome="completed",
            computed_amount=20_000,
            threshold_version=1,
        ),
    ]


# ---------------------------------------------------------------- 읽기
async def list_incidents() -> list[dict[str, Any]]:
    if is_mock():
        return [_mock_incident()]
    data = await _get("orchestrator", "/api/incidents")
    if not isinstance(data, list):
        raise ApiError(502, "UPSTREAM_ERROR", "orchestrator 응답 형식이 다르다")
    return data


async def get_incident(context_id: str) -> dict[str, Any]:
    if is_mock():
        found = _mock_incident()
        if found["context_id"] != context_id:
            raise ApiError(404, "NOT_FOUND", f"사건 {context_id}를 찾을 수 없다")
        return found
    data = await _get("orchestrator", f"/api/incidents/{context_id}")
    if not isinstance(data, dict):
        raise ApiError(502, "UPSTREAM_ERROR", "orchestrator 응답 형식이 다르다")
    return data


def _execution_line(record: dict[str, Any]) -> ExecutionLine:
    request, decision = record["request"], record["decision"]
    hold_reason = decision.get("hold_reason")
    return ExecutionLine(
        execution_id=decision["execution_id"],
        created_at=record["created_at"],
        context_id=request["context_id"],
        action=request["action"],
        requester=request["requester"],
        grade=decision["grade"],
        status=decision["status"],
        outcome=_OUTCOME.get(decision["status"], "in_progress"),
        computed_amount=decision.get("computed_amount"),
        threshold_version=decision.get("threshold_version"),
        hold_reason=str(hold_reason) if hold_reason else None,
        review_id=decision.get("review_id"),
    )


async def list_executions(context_id: str | None) -> list[ExecutionLine]:
    if is_mock():
        lines = _mock_executions()
        return [x for x in lines if context_id in (None, x.context_id)]
    data = await _get(
        "safety", "/api/safety/executions", {"context_id": context_id} if context_id else None
    )
    if not isinstance(data, list):
        raise ApiError(502, "UPSTREAM_ERROR", "safety 응답 형식이 다르다")
    try:
        return [_execution_line(r) for r in data]
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError(502, "UPSTREAM_ERROR", "safety 응답 형식이 다르다") from exc
