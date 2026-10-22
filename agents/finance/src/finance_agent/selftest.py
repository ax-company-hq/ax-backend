"""재무 AI 자체 점검 (대시보드 "재무 AI 테스트" 버튼).

담당자가 아침마다 눌러서 재무 AI가 제대로 판단하고, 안전 장치·원천 데이터와 연결돼 있는지 본다.

- 판단 점검은 **테스트 전용 가짜 자료**(`SELFTEST_SOURCES`)로 돌린다. 실제 판정 기록, 환불 누적,
  AI 판단 기록에는 아무것도 남기지 않는다. 숫자는 실제 판단과 같은 코드가 계산한다.
- 연결 점검은 실제 주소(`agent_url`)에 `GET`만 보낸다. 상태를 바꾸지 않는다.
- 결과 수준: `fail`(판단이 틀리거나 안전 장치가 끊김 → 바로 확인 필요), `warn`(원천 데이터에 닿지
  않음 → 실제 검증이 사람 확인으로 넘어갈 수 있음).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx

from ax_agent_template.config import agent_url
from ax_contracts.schemas import AgentRequest, RequestMeta
from finance_agent.review import ReviewPolicy, decide
from finance_agent.schemas import ReviewHoldInput, VerifyAmountInput
from finance_agent.sources import MockSources

CTX = "ctx_selftest"
CALL_SECONDS = 3.0

# 테스트 전용 가짜 자료: 장애 피해 고객 2명(피해 100,000원), 결제 30,000원 주문 하나
SELFTEST_SOURCES = MockSources(
    {
        "impacts": {
            CTX: {
                "failed_order_count": 2,
                "affected_customer_count": 2,
                "damage_amount": 100000,
                "affected_customer_ids": ["test_buyer_1", "test_buyer_2"],
            }
        },
        "payments": {"test_order_1": 30000},
        "fraud_scores": {},
    }
)
EMPTY_SOURCES = MockSources({"impacts": {}, "payments": {}, "fraud_scores": {}})

Level = Literal["fail", "warn"]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    level: Level = "fail"

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "level": self.level}


def _meta() -> RequestMeta:
    return RequestMeta(
        context_id=CTX,
        token="selftest",
        deadline=datetime.now(UTC) + timedelta(seconds=10),
    )


def _verify_input(action: str, params: dict[str, Any], computed: int | None) -> VerifyAmountInput:
    return VerifyAmountInput(
        execution_id="exe_selftest",
        action=action,
        requester="selftest",
        params=params,
        computed_amount=computed,
        threshold_version=1,
    )


def _coupon(buyer: str, count: int, unit: int, total: int | None = None) -> dict[str, Any]:
    return {
        "purpose_code": "compensation",
        "buyer_id": buyer,
        "count": count,
        "unit_value": unit,
        "total_value": count * unit if total is None else total,
    }


async def _judgement_checks(recompute: Any, policy: ReviewPolicy) -> list[Check]:
    """판단 점검. 실제 판단 함수(`recompute`, `decide`)를 가짜 자료로 부른다."""
    meta = _meta()
    request = AgentRequest(meta=meta, skill="verify_amount")
    cases = [
        (
            "쿠폰 금액 다시 계산",
            "issue_coupon",
            _coupon("test_buyer_1", 2, 5000),
            10000,
            SELFTEST_SOURCES,
            ("approved", "recomputed_match"),
            "2장 × 5,000원 = 10,000원을 통과시킴",
        ),
        (
            "금액을 틀리게 적은 쿠폰 막기",
            "issue_coupon",
            _coupon("test_buyer_1", 3, 5000, 5000),
            15000,
            SELFTEST_SOURCES,
            ("rejected", "amount_mismatch"),
            "15,000원인데 5,000원이라 적은 요청을 막음",
        ),
        (
            "피해 고객이 아닌 사람 막기",
            "issue_coupon",
            _coupon("someone_else", 1, 5000),
            5000,
            SELFTEST_SOURCES,
            ("rejected", "not_affected"),
            "피해 고객 목록에 없는 사람의 보상을 막음",
        ),
        (
            "결제보다 큰 환불 막기",
            "refund_request",
            {"order_id": "test_order_1", "refund_amount": 40000},
            40000,
            SELFTEST_SOURCES,
            ("rejected", "exceeds_paid"),
            "결제 30,000원 주문에 40,000원 환불을 막음",
        ),
        (
            "자료가 없으면 통과시키지 않기",
            "issue_coupon",
            _coupon("test_buyer_1", 1, 5000),
            5000,
            EMPTY_SOURCES,
            ("rejected", "source_unavailable"),
            "피해 자료가 없을 때 승인하지 않음",
        ),
    ]
    checks: list[Check] = []
    for name, action, params, computed, sources, expected, good in cases:
        try:
            out = await recompute(request, _verify_input(action, params, computed), sources, {})
            got = (out.decision, out.reason_code)
            checks.append(
                Check(
                    name,
                    got == expected,
                    good if got == expected else f"예상 {expected}, 실제 {got}",
                )
            )
        except Exception as exc:  # 점검 자체가 깨져도 결과로 보여 준다
            checks.append(Check(name, False, f"점검 중 오류: {type(exc).__name__}"))

    reviews = [
        (
            "AI 판단: 재량 한도 안 보상은 승인",
            _coupon("test_buyer_1", 3, 20000),
            60000,
            {"daily_total": 60000, "context_total": 60000, "customer_total": 60000},
            "approve",
        ),
        (
            "AI 판단: 아주 큰 금액은 사람에게",
            _coupon("test_buyer_1", 25, 20000),
            500000,
            {"daily_total": 500000, "context_total": 500000, "customer_total": 500000},
            "escalate",
        ),
        (
            "AI 판단: 피해 고객이 아니면 거절",
            _coupon("someone_else", 3, 20000),
            60000,
            {"daily_total": 60000, "context_total": 60000, "customer_total": 60000},
            "reject",
        ),
    ]
    for name, params, amount, totals, expected in reviews:
        data = ReviewHoldInput(
            review_id="rev_selftest",
            execution_id="exe_selftest",
            action="issue_coupon",
            requester="selftest",
            params=params,
            computed_amount=amount,
            hold_reason="over_threshold",
            totals=totals,
        )
        try:
            out = await decide(data, meta, SELFTEST_SOURCES, 0, policy)
            ok = out.decision == expected
            checks.append(
                Check(name, ok, out.explanation if ok else f"예상 {expected}, 실제 {out.decision}")
            )
        except Exception as exc:
            checks.append(Check(name, False, f"점검 중 오류: {type(exc).__name__}"))

    checks.append(
        Check(
            "AI 판단 기준표 읽기",
            policy.coupon.max_total > 0 and policy.refund.max_amount > 0,
            f"쿠폰 재량 한도 {policy.coupon.max_total:,}원, "
            f"환불 재량 한도 {policy.refund.max_amount:,}원",
        )
    )
    return checks


async def _get_json(client: httpx.AsyncClient, url: str) -> tuple[int | None, Any]:
    try:
        response = await client.get(url, timeout=CALL_SECONDS)
    except httpx.HTTPError:
        return None, None
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


async def _connection_checks(client: httpx.AsyncClient) -> list[Check]:
    checks: list[Check] = []
    safety = agent_url("safety")
    status, _ = await _get_json(client, f"{safety}/health")
    checks.append(
        Check(
            "안전 장치 연결",
            status == 200,
            "안전 장치가 응답함"
            if status == 200
            else "안전 장치에 닿지 않음. 돈이 나가는 일이 모두 멈춥니다",
        )
    )

    status, body = await _get_json(client, f"{safety}/api/safety/registry")
    actions = (
        {a.get("action"): a for a in (body or {}).get("data", {}).get("actions", [])}
        if status == 200
        else {}
    )
    routed = all(
        actions.get(a, {}).get("verifier") == "finance" for a in ("issue_coupon", "refund_request")
    )
    checks.append(
        Check(
            "쿠폰·환불 검증을 재무 AI가 맡는지",
            routed,
            "안전 장치가 쿠폰·환불 검증을 재무 AI에 보냄"
            if routed
            else "안전 장치 설정에서 쿠폰·환불 검증자가 재무 AI가 아님",
        )
    )

    status, body = await _get_json(client, f"{safety}/api/safety/thresholds")
    auto = (body or {}).get("data", {}).get("auto_review", {}) if status == 200 else {}
    on = bool(auto.get("enabled")) and auto.get("reviewer") == "finance"
    checks.append(
        Check(
            "AI 1차 판단 켜짐",
            on,
            "기준을 조금 넘는 건은 재무 AI가 먼저 판단함"
            if on
            else "AI 1차 판단이 꺼져 있어 기준을 넘는 건은 모두 사람에게 갑니다",
            level="warn",
        )
    )

    for name, target, why in (
        (
            "쇼핑몰 연결",
            "shop-api",
            "환불 검증에 필요한 결제 금액을 못 받아 사람 확인으로 넘어갈 수 있습니다",
        ),
        (
            "데이터 AI 연결",
            "data",
            "보상 검증에 필요한 피해 고객 목록을 못 받아 사람 확인으로 넘어갈 수 있습니다",
        ),
    ):
        status, _ = await _get_json(client, f"{agent_url(target)}/health")
        checks.append(
            Check(
                name,
                status == 200,
                "응답함" if status == 200 else f"닿지 않음. {why}",
                level="warn",
            )
        )
    return checks


async def run_self_test(
    recompute: Any, policy: ReviewPolicy, http_client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    started = time.perf_counter()
    checks = await _judgement_checks(recompute, policy)
    client = http_client or httpx.AsyncClient()
    try:
        checks += await _connection_checks(client)
    finally:
        if http_client is None:
            await client.aclose()
    failed = [c for c in checks if not c.ok and c.level == "fail"]
    warned = [c for c in checks if not c.ok and c.level == "warn"]
    result = "fail" if failed else "warn" if warned else "pass"
    summary = {
        "pass": f"전부 정상입니다 ({len(checks)}개 항목).",
        "warn": f"판단은 정상이지만 {len(warned)}개 연결을 확인해 주세요.",
        "fail": f"{len(failed)}개 항목이 실패했습니다. 재무 담당자에게 알려 주세요.",
    }[result]
    return {
        "result": result,
        "summary": summary,
        "checks": [c.as_dict() for c in checks],
        "passed": sum(c.ok for c in checks),
        "total": len(checks),
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "ran_at": datetime.now(UTC).isoformat(),
    }
