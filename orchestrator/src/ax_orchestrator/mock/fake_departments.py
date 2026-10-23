"""가짜 부서 하네스 (목 모드·테스트 공용).

실제 부서 에이전트 없이 총괄을 끝까지 돌리기 위한 가짜 부서 7곳이다. 모든 값은 지어낸
목 데이터이며 화면에도 목으로 표시된다. 각 (부서, skill)마다 동작을 스크립트로 지정할 수 있다:
`ok` `fail` `timeout`(마감 초과) `forbidden`(소관 아님) `hold`(C 등급 보류) `dept_input`.
스크립트는 호출 순서대로 하나씩 소비되고, 비면 `ok`다.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

from ax_contracts.schemas import (
    AgentRequest,
    AgentResult,
    ErrorCode,
    InputReason,
    TaskStatus,
)

# 부서 → [(skill, 기본 등급, verifier)]. 인프라 카드는 실제로 비어 있어 가짜로 채운다.
CARDS: dict[str, list[tuple[str, str, str | None]]] = {
    "infra": [
        ("diagnose_incident", "read", None),
        ("rollback_deployment", "B", "planning"),
        ("verify_recovery", "read", None),
    ],
    "data": [("anomaly_detection", "read", None), ("impact_analysis", "read", None)],
    "planning": [
        ("find_related_prs", "read", None),
        ("link_incident", "A", None),
        ("verify_rollback_target", "read", None),
    ],
    "marketing": [
        ("draft_notice", "read", None),
        ("publish_notice", "A", None),
        ("issue_coupon", "B", "finance"),
    ],
    "support": [("request_compensation", "A", None), ("send_reply", "A", None)],
    "finance": [("loss_aggregation", "read", None), ("verify_amount", "read", None)],
    "hr": [("record_case", "A", None)],
}
OWNERS = {
    "infra": "예진",
    "data": "재현",
    "planning": "예진",
    "marketing": "재헌",
    "support": "병환",
    "finance": "태윤",
    "hr": "준하",
}


def fake_card(dept: str, *, empty: bool = False) -> dict[str, Any]:
    skills = [] if empty else CARDS[dept]
    return {
        "name": dept,
        "owner": OWNERS[dept],
        "description": f"{dept} (목 카드)",
        "url": f"http://{dept}:0/a2a",
        "skills": [
            {
                "id": skill,
                "description": f"{skill} (목)",
                "input_schema": "mock",
                "output_schema": "mock",
                "grade": grade,
                "allowed_requesters": ["orchestrator"],
                "verifier": verifier,
                "priority": "required",
            }
            for skill, grade, verifier in skills
        ],
    }


def _ok(**output: Any) -> dict[str, Any]:
    return output


IDS = [f"cus_{i:03d}" for i in range(1, 88)]
DEFAULT_OUTPUTS: dict[str, Callable[[AgentRequest], dict[str, Any]]] = {
    "diagnose_incident": lambda r: _ok(
        status="suspected",
        suspects=[
            {
                "service": "payment-api",
                "version": "v2.4.1",
                "previous_version": "v2.4.0",
                "confidence": "mid",
            }
        ],
        impact_scope=["payment"],
    ),
    "impact_analysis": lambda r: _ok(
        failed_order_count=128,
        affected_customer_count=len(IDS),
        affected_customer_ids=IDS,
        damage_amount=5_430_000,
    ),
    "rollback_deployment": lambda r: _ok(rolled_back_to=r.input.get("to_version")),
    "find_related_prs": lambda r: _ok(candidates=[{"number": 412}, {"number": 418}]),
    "link_incident": lambda r: _ok(linked_paras=["PR #412", "PR #418"]),
    "draft_notice": lambda r: _ok(notice_id="ntc_mock01"),
    "publish_notice": lambda r: _ok(notice_id=r.input.get("notice_id") or "ntc_mock01"),
    "request_compensation": lambda r: _ok(
        eligible_customer_ids=r.input.get("customer_ids", []), amount_per_customer=5_000
    ),
    "issue_coupon": lambda r: _ok(
        issued=len(r.input.get("customer_ids", [])),
        total_amount=len(r.input.get("customer_ids", [])) * 5_000,
    ),
    "loss_aggregation": lambda r: _ok(
        damage_amount=r.input.get("damage_amount"),
        compensation_paid=r.input.get("compensation_paid"),
    ),
    "verify_recovery": lambda r: _ok(recovered=True, infra_metrics_ok=True),
    "record_case": lambda r: _ok(card_id="card_mock01"),
    "verify_amount": lambda r: _ok(match=True),
}


class FakeDepartments:
    """`call_agent`와 같은 모양의 호출기. 총괄 `Engine(caller=...)`에 그대로 넣는다."""

    def __init__(self, *, latency: float = 0.0) -> None:
        self.latency = latency
        self.calls: list[tuple[str, AgentRequest]] = []
        self.down: set[str] = set()
        self.empty_cards: set[str] = set()
        self._scripts: dict[tuple[str, str], deque[str]] = defaultdict(deque)
        self._seen_keys: dict[str, AgentResult] = {}

    def script(self, dept: str, skill: str, *behaviors: str) -> None:
        self._scripts[(dept, skill)].extend(behaviors)

    async def fetch_card(self, dept: str) -> dict[str, Any] | None:
        if dept in self.down:
            return None
        return fake_card(dept, empty=dept in self.empty_cards)

    async def __call__(self, dept: str, req: AgentRequest) -> AgentResult:
        self.calls.append((dept, req))
        if self.latency:
            await asyncio.sleep(self.latency)
        key = req.meta.idempotency_key
        script = self._scripts[(dept, req.skill)]
        behavior = script.popleft() if script else "ok"
        if behavior == "ok" and key and req.skill in {"issue_coupon", "publish_notice"}:
            cached = self._seen_keys.get(f"{dept}:{key}")
            if cached is not None:  # 멱등: 같은 키는 같은 결과
                return cached
        result = self._respond(dept, req, behavior)
        if behavior == "ok" and key and req.skill in {"issue_coupon", "publish_notice"}:
            self._seen_keys[f"{dept}:{key}"] = result
        return result

    def _respond(self, dept: str, req: AgentRequest, behavior: str) -> AgentResult:
        meta = req.meta
        if behavior == "fail":
            return AgentResult(meta=meta, status=TaskStatus.FAILED, outcome="upstream_error")
        if behavior == "timeout":
            return AgentResult(meta=meta, status=TaskStatus.CANCELED, outcome=ErrorCode.TIMEOUT)
        if behavior == "forbidden":
            return AgentResult(
                meta=meta, status=TaskStatus.REJECTED, outcome=ErrorCode.FORBIDDEN_SKILL
            )
        if behavior == "hold" and req.input.get("review_decision") != "approved":
            return AgentResult(
                meta=meta,
                status=TaskStatus.INPUT_REQUIRED,
                reason_code=InputReason.HUMAN_INPUT,
                outcome="held",
                output={"hold_reason": "over_threshold"},
            )
        if behavior == "dept_input" and "answers" not in req.input:
            return AgentResult(
                meta=meta,
                status=TaskStatus.INPUT_REQUIRED,
                reason_code=InputReason.DEPT_INPUT,
                output={
                    "question": {"skill": "verify_amount", "input": {"amount": 1}},
                },
            )
        build = DEFAULT_OUTPUTS.get(req.skill, lambda r: {})
        return AgentResult(meta=meta, status=TaskStatus.COMPLETED, output=build(req))
