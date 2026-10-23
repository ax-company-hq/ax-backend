"""AI 대화방 읽기 경로: 총괄 사건과 안전 관문 실행 기록을 dashboard-api가 읽어 준다 (이슈 #255).

브라우저가 총괄(8000)·안전 관문(8001)을 직접 부르지 않게 한다. 운영 빌드에는 개발 서버 프록시가 없고
대시보드는 dashboard-api를 거치는 것이 규약이다. 읽기 전용이라 상태를 바꾸지 않는다. 사람 결정은
여기서 만들지 않고 안전 관문 보류 큐 결정 경로를 쓴다.

- 총괄 사건(`GET /api/incidents`)은 그대로 중계하되 `untrusted` 키는 걸러 낸다.
- 안전 관문 기록은 화면에 필요한 필드만 남긴다. 요청의 `params`·`evidence`·멱등 키·`report`·고객
  정보는 넘기지 않고, 금액은 안전 레이어가 계산한 `decision.computed_amount`만 보여 준다.
- 주소는 `AX_ORCHESTRATOR_URL`·`AX_SAFETY_URL`(없으면 `http://<이름>:<포트>`, 템플릿 `agent_url`).
"""

from __future__ import annotations

from typing import Any

import httpx

from ax_agent_template import config
from ax_dashboard_api.envelope import ApiError

TIMEOUT_SECONDS = 10.0
_client: httpx.AsyncClient | None = None  # 테스트가 `use_client`로 바꿔 끼운다

DECISION_FIELDS = (
    "execution_id",
    "grade",
    "status",
    "computed_amount",
    "threshold_version",
    "verifier",
    "hold_reason",
    "review_id",
    "result_ref",
)


def use_client(client: httpx.AsyncClient | None) -> None:
    global _client
    _client = client


def scrub(value: Any) -> Any:
    """고객·외부 입력이 담기는 `untrusted` 키를 중첩된 곳까지 지운다."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k != "untrusted"}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


async def _get(target: str, path: str, params: dict[str, str] | None = None) -> Any:
    client = _client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
    try:
        response = await client.get(f"{config.agent_url(target)}{path}", params=params)
    except httpx.TimeoutException as exc:
        raise ApiError(504, "UPSTREAM_TIMEOUT", f"{target}가 제때 응답하지 않았다") from exc
    except httpx.HTTPError as exc:
        raise ApiError(502, "UPSTREAM_ERROR", f"{target}에 연결할 수 없다") from exc
    finally:
        if _client is None:
            await client.aclose()
    if response.status_code == 404:
        raise ApiError(404, "NOT_FOUND", "찾을 수 없다")
    if response.status_code != 200:
        raise ApiError(502, "UPSTREAM_ERROR", f"{target} 응답 오류 ({response.status_code})")
    try:
        return response.json()["data"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError(502, "UPSTREAM_ERROR", f"{target} 응답 형식이 다르다") from exc


async def incidents() -> list[dict[str, Any]]:
    data = await _get("orchestrator", "/api/incidents")
    if not isinstance(data, list):
        raise ApiError(502, "UPSTREAM_ERROR", "orchestrator 응답 형식이 다르다")
    return scrub(data)


async def incident(context_id: str) -> dict[str, Any]:
    data = await _get("orchestrator", f"/api/incidents/{context_id}")
    if not isinstance(data, dict):
        raise ApiError(502, "UPSTREAM_ERROR", "orchestrator 응답 형식이 다르다")
    return scrub(data)


def project_execution(record: dict[str, Any]) -> dict[str, Any]:
    """안전 관문 실행 기록에서 화면에 필요한 필드만 남긴다."""
    request = record.get("request") or {}
    decision = record.get("decision") or {}
    return {
        "execution_id": record.get("execution_id"),
        "created_at": record.get("created_at"),
        "request": {
            "action": request.get("action"),
            "requester": request.get("requester"),
            "context_id": request.get("context_id"),
        },
        "decision": {field: decision.get(field) for field in DECISION_FIELDS},
    }


async def executions(context_id: str | None = None) -> list[dict[str, Any]]:
    params = {"context_id": context_id} if context_id else None
    data = await _get("safety", "/api/safety/executions", params)
    if not isinstance(data, list):
        raise ApiError(502, "UPSTREAM_ERROR", "safety 응답 형식이 다르다")
    return [project_execution(r) for r in data if isinstance(r, dict)]


# ---------------------------------------------------------------- 목 모드 샘플
def mock_incidents() -> list[dict[str, Any]]:
    def step(step_id: str, skill: str, label: str, dept: str, status: str, **fields: Any) -> dict:
        return {
            "step_id": step_id,
            "skill": skill,
            "label": label,
            "dept": dept,
            "status": status,
            "attempts": 1,
            "note": None,
            "grade": "A",
            "started_at": "2026-10-22T09:00:00Z",
            "finished_at": None if status in ("running", "held") else "2026-10-22T09:00:05Z",
            "transferred_to": None,
            "reason_code": None,
            "output": {},
            **fields,
        }

    return [
        {
            "context_id": "ctx_inc_mock0001",
            "kind": "payment_outage",
            "title": "결제 장애 — 서버 오류(5xx) 급증",
            "component": "payment",
            "status": "open",
            "opened_at": "2026-10-22T09:00:00Z",
            "closed_at": None,
            "steps": [
                step(
                    "s1", "impact_analysis", "영향 분석", "data", "completed", output={"orders": 42}
                ),
                step("s2", "publish_notice", "점검 공지 게시", "marketing", "completed"),
                step("s3", "issue_coupon", "보상 쿠폰 지급", "marketing", "held", grade="C"),
            ],
            "events": [{"at": "2026-10-22T09:00:00Z", "message": "사건 열림"}],
            "summary": [],
            "metrics": {},
            "pending_holds": [
                {"step_id": "s3", "skill": "issue_coupon", "label": "보상 쿠폰 지급"},
            ],
        }
    ]


def mock_executions() -> list[dict[str, Any]]:
    def record(n: int, action: str, requester: str, **decision: Any) -> dict[str, Any]:
        return project_execution(
            {
                "execution_id": f"exe_mock{n}",
                "created_at": f"2026-10-22T09:0{n}:00Z",
                "request": {
                    "action": action,
                    "requester": requester,
                    "context_id": "ctx_inc_mock0001",
                },
                "decision": {"execution_id": f"exe_mock{n}", **decision},
            }
        )

    return [
        record(1, "publish_notice", "marketing", grade="A", status="executed"),
        record(
            2,
            "issue_coupon",
            "marketing",
            grade="C",
            status="held",
            computed_amount=435000,
            hold_reason="over_threshold",
            review_id="rev_mock1",
            verifier="finance",
        ),
    ]
