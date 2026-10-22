"""finance skill 핸들러.

| skill | 등급 | 요청자 | 하는 일 |
| --- | --- | --- | --- |
| `verify_amount` | read | safety | B 액션(쿠폰·환불) 금액 재계산·승인/거절 (DEP-FIN-02) |
| `loss_aggregation` | read | orchestrator | data 영향 분석 + 지급 보상 = 손실 리포트 (DEP-FIN-01) |
| `seller_review` | B | orchestrator | 마켓운영: 서류·위험 점수로 판매자 승인 요청 (DEP-FIN-03) |
| `settlement_hold` | B | orchestrator | 마켓운영: 위험 판매자 정산 보류 요청 (DEP-FIN-04) |

- 숫자는 전부 코드로 계산한다. 요청 쪽이 적은 총액·등급은 믿지 않는다.
- 상태 변경(판매자 승인, 정산 보류)은 `execute()`가 `executed`를 돌려준 뒤에만 기록한다.
- 외부 입력(`request.untrusted`, 판매자 소개 등)은 읽지 않는다.
- 저장소는 메모리다. TODO: 정산·심사 기록을 shop-api 쓰기 API(관문 뒤)와 영속 저장소로 옮긴다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from ax_agent_template.config import is_mock
from ax_agent_template.execute import execute
from ax_contracts.schemas import (
    AgentRequest,
    AgentResult,
    EventEnvelope,
    EventType,
    Evidence,
    ExecuteDecision,
    InputReason,
    TaskStatus,
)
from finance_agent.schemas import (
    LossAggregationInput,
    LossReport,
    SellerReviewInput,
    SellerReviewOutput,
    SettlementHoldInput,
    SettlementHoldOutput,
    VerifyAmountInput,
    VerifyAmountOutput,
)
from finance_agent.sources import LiveSources, MockSources, Sources

CLAIMED_TOTAL_FIELDS = ("total_value", "claimed_total")


class Ledger:
    """이벤트로 쌓는 지급·환불 기록과 처리 결과."""

    def __init__(self) -> None:
        self.coupons: dict[str, dict[str, int]] = {}  # context_id → {"count", "total"}
        self.refunds: dict[str, int] = {}  # order_id → 환불 누적
        self.seen_events: set[str] = set()
        self.verified: dict[str, AgentResult] = {}  # execution_id → 판정
        self.verify_log: list[dict[str, Any]] = []  # 대시보드 조회용 판정 기록
        self.done: dict[str, AgentResult] = {}  # 멱등성 키 → 끝난 상태 변경 결과
        self.market_ops: list[dict[str, Any]] = []  # 판매자 심사·정산 보류 결과 기록

    def compensation(self, context_id: str) -> dict[str, int]:
        return self.coupons.get(context_id, {"count": 0, "total": 0})


class State:
    def __init__(self, sources: Sources | None = None) -> None:
        self.sources: Sources = sources or (MockSources() if is_mock("finance") else LiveSources())
        self.ledger = Ledger()


STATE: State | None = None


def state() -> State:
    global STATE
    if STATE is None:
        STATE = State()
    return STATE


def reset_state(sources: Sources | None = None) -> None:
    """테스트 사이에 저장소를 비운다."""
    global STATE
    STATE = State(sources)


# ---------------------------------------------------------------- 공통
def _now() -> datetime:
    return datetime.now(UTC)


def _evidence(queries: list[str], summary: str, ref_ids: list[str] | None = None) -> Evidence:
    return Evidence(
        queries=queries, data_summary=summary, collected_at=_now(), ref_ids=ref_ids or []
    )


def _result(request: AgentRequest, status: TaskStatus, **kwargs: Any) -> AgentResult:
    return AgentResult(meta=request.meta, status=status, **kwargs)


def _parse[T: BaseModel](request: AgentRequest, model: type[T]) -> T | AgentResult:
    try:
        return model.model_validate(request.input)
    except ValidationError as error:
        errors = [f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in error.errors()]
        return _result(
            request, TaskStatus.FAILED, outcome="invalid_input", output={"errors": errors}
        )


def _positive_int(value: Any) -> int | None:
    ok = isinstance(value, int) and not isinstance(value, bool) and value > 0
    return value if ok else None


# ---------------------------------------------------------------- 이벤트 (지급·환불 기록)
async def on_event(envelope: EventEnvelope) -> None:
    """`coupon.issued`(보상 쿠폰)와 `payment.refunded`를 누적한다. 같은 event_id는 한 번만."""
    ledger = state().ledger
    if envelope.event_id in ledger.seen_events:
        return
    ledger.seen_events.add(envelope.event_id)
    data = envelope.data
    if envelope.type == EventType.COUPON_ISSUED and envelope.context_id:
        if data.get("purpose_code") != "compensation":
            return
        count, total = _positive_int(data.get("count")), _positive_int(data.get("total_value"))
        if count is None or total is None:
            return
        row = ledger.coupons.setdefault(envelope.context_id, {"count": 0, "total": 0})
        row["count"] += count
        row["total"] += total
    elif envelope.type == EventType.PAYMENT_REFUNDED:
        order_id, amount = data.get("order_id"), _positive_int(data.get("refund_amount"))
        if isinstance(order_id, str) and amount is not None:
            ledger.refunds[order_id] = ledger.refunds.get(order_id, 0) + amount


SUBSCRIBED_EVENTS = [EventType.COUPON_ISSUED, EventType.PAYMENT_REFUNDED]


# ---------------------------------------------------------------- verify_amount
async def recompute(request: AgentRequest, data: VerifyAmountInput) -> VerifyAmountOutput:
    """원천 데이터로 다시 계산한다. 근거가 없으면 승인하지 않는다."""

    def reject(reason: str, **recomputed: int) -> VerifyAmountOutput:
        return VerifyAmountOutput(
            decision="rejected",
            reason_code=reason,  # type: ignore[arg-type]
            recomputed=recomputed,
            threshold_version=data.threshold_version,
        )

    params, meta = data.params, request.meta
    if data.action == "issue_coupon":
        count, unit = _positive_int(params.get("count")), _positive_int(params.get("unit_value"))
        if count is None or unit is None:
            return reject("invalid_params")
        total = count * unit
        claimed = [params.get(k) for k in CLAIMED_TOTAL_FIELDS if params.get(k) is not None]
        if data.computed_amount is not None:
            claimed.append(data.computed_amount)
        if any(c != total for c in claimed):
            return reject("amount_mismatch", count=count, unit_value=unit, total_value=total)
        if params.get("purpose_code") == "compensation":
            impact = await state().sources.impact(meta, meta.context_id, None)
            if impact is None or not impact.affected_customer_ids:
                return reject("source_unavailable", total_value=total)
            if params.get("buyer_id") not in impact.affected_customer_ids:
                return reject("not_affected", total_value=total)
        return VerifyAmountOutput(
            decision="approved",
            reason_code="recomputed_match",
            recomputed={"count": count, "unit_value": unit, "total_value": total},
            threshold_version=data.threshold_version,
        )

    if data.action == "refund_request":
        amount, order_id = _positive_int(params.get("refund_amount")), params.get("order_id")
        if amount is None or not isinstance(order_id, str):
            return reject("invalid_params")
        if data.computed_amount is not None and data.computed_amount != amount:
            return reject("amount_mismatch", refund_amount=amount)
        paid = await state().sources.paid_amount(meta, order_id)
        if paid is None:
            return reject("source_unavailable", refund_amount=amount)
        already = state().ledger.refunds.get(order_id, 0)
        if already + amount > paid:
            return reject(
                "exceeds_paid", refund_amount=amount, paid_amount=paid, refunded_before=already
            )
        return VerifyAmountOutput(
            decision="approved",
            reason_code="recomputed_match",
            recomputed={"refund_amount": amount, "paid_amount": paid, "refunded_before": already},
            threshold_version=data.threshold_version,
        )

    return reject("unsupported_action")


async def verify_amount(request: AgentRequest) -> AgentResult:
    data = _parse(request, VerifyAmountInput)
    if isinstance(data, AgentResult):
        return data
    ledger = state().ledger
    if data.execution_id in ledger.verified:  # 같은 검증 요청은 이전 판정
        return ledger.verified[data.execution_id].model_copy(update={"meta": request.meta})
    output = await recompute(request, data)
    result = _result(
        request,
        TaskStatus.COMPLETED,
        outcome=output.decision,
        output=output.model_dump(mode="json"),
        evidence=_evidence(
            [f"verify:{data.action}"],
            f"{data.action} 재계산 {output.recomputed} → {output.reason_code}",
            [data.execution_id],
        ),
    )
    ledger.verified[data.execution_id] = result
    ledger.verify_log.append(
        {
            "execution_id": data.execution_id,
            "context_id": request.meta.context_id,
            "action": data.action,
            "requester": data.requester,
            **output.model_dump(mode="json"),
            "at": _now().isoformat(),
        }
    )
    return result


# ---------------------------------------------------------------- loss_aggregation
def _won(value: int) -> str:
    return f"{value:,}원"


async def loss_aggregation(request: AgentRequest) -> AgentResult:
    data = _parse(request, LossAggregationInput)
    if isinstance(data, AgentResult):
        return data
    context_id = data.context_id or request.meta.context_id
    window = {
        "from": data.window_from.isoformat(),
        "to": data.window_to.isoformat(),
        **({"category": data.category} if data.category else {}),
    }
    impact = await state().sources.impact(request.meta, context_id, window)
    comp = state().ledger.compensation(context_id)

    coupon_line = f"보상 쿠폰 {comp['count']}장 {_won(comp['total'])}"
    if impact is None:
        summary = f"영향 수치: 확인할 수 없음(data 영향 분석 응답 없음). {coupon_line}."
        report = LossReport(
            context_id=context_id,
            compensation_coupon_count=comp["count"],
            compensation_total=comp["total"],
            summary=summary,
        )
    else:
        total = impact.lost_amount + comp["total"]  # 계산은 코드로
        summary = (
            f"실패 주문 {impact.failed_orders}건, 영향 고객 {impact.affected_customers}명, "
            f"피해 금액 {_won(impact.lost_amount)}. {coupon_line}. 총 손실 {_won(total)}."
        )
        report = LossReport(
            context_id=context_id,
            failed_orders=impact.failed_orders,
            affected_customers=impact.affected_customers,
            lost_amount=impact.lost_amount,
            compensation_coupon_count=comp["count"],
            compensation_total=comp["total"],
            total_loss=total,
            summary=summary,
        )
    refs = [impact.ref] if impact else []
    return _result(
        request,
        TaskStatus.COMPLETED,
        outcome="partial" if impact is None else "reported",
        output=report.model_dump(mode="json"),
        evidence=_evidence(
            ["data:impact_analysis", f"ledger:coupon.issued:{context_id}"], summary, refs
        ),
    )


# ---------------------------------------------------------------- 마켓운영 (관문 경유)
async def _gated(
    request: AgentRequest, action: str, target: str, params: dict[str, Any], evidence: Evidence
) -> tuple[ExecuteDecision | None, AgentResult | None, str]:
    """`execute()`를 부른다. 이미 끝난 같은 요청이면 (None, 이전 결과, 키)."""
    key = request.meta.idempotency_key or f"{request.meta.context_id}:{action}:{target}"
    previous = state().ledger.done.get(key)
    if previous is not None:
        return None, previous.model_copy(update={"meta": request.meta}), key
    meta = request.meta.model_copy(update={"idempotency_key": key})
    return await execute(action, params, meta, evidence), None, key


def _decision_status(decision: ExecuteDecision) -> dict[str, Any]:
    """판정을 Task 상태로 옮긴다 (공통 구성안 5장, 후속 결정 12)."""
    if decision.status == "executed":
        return {"status": TaskStatus.COMPLETED}
    if decision.status == "held":
        return {
            "status": TaskStatus.INPUT_REQUIRED,
            "reason_code": InputReason.HUMAN_INPUT,
            "outcome": "held",
        }
    if decision.status == "verifying":
        return {"status": TaskStatus.WORKING, "outcome": "verifying"}
    if decision.status == "rejected":
        return {"status": TaskStatus.COMPLETED, "outcome": "rejected_by_review"}
    return {"status": TaskStatus.FAILED, "outcome": "execute_failed"}


def _log_market_op(request: AgentRequest, skill: str, output: dict[str, Any]) -> None:
    state().ledger.market_ops.append(
        {"skill": skill, "context_id": request.meta.context_id, **output, "at": _now().isoformat()}
    )


async def seller_review(request: AgentRequest) -> AgentResult:
    data = _parse(request, SellerReviewInput)
    if isinstance(data, AgentResult):
        return data

    def done(output: SellerReviewOutput, evidence: Evidence, **status: Any) -> AgentResult:
        return _result(
            request,
            output=output.model_dump(mode="json"),
            evidence=evidence,
            **({"status": TaskStatus.COMPLETED} | status),
        )

    if not data.documents_verified:  # 서류 미확인은 승인 요청을 보내지 않는다
        output = SellerReviewOutput(
            seller_id=data.seller_id, decision="rejected", reason_code="documents_missing"
        )
        return done(output, _evidence(["documents"], "제출 서류 미확인"), outcome="auto_rejected")

    score = await state().sources.fraud_score(request.meta, data.seller_id)
    if score is None:  # 위험 점수 없이는 판단하지 않는다
        output = SellerReviewOutput(
            seller_id=data.seller_id, decision="undetermined", reason_code="risk_unavailable"
        )
        return done(
            output,
            _evidence(["data:fraud_score"], "위험 점수: 확인할 수 없음"),
            outcome="undetermined",
        )

    evidence = _evidence(["data:fraud_score"], f"위험 점수 {score}", [data.seller_id])
    params = {
        "seller_id": data.seller_id,
        "risk_score": score,
        "category_codes": data.category_codes,
        "decision": "approve",
    }
    decision, previous, key = await _gated(
        request, "seller_review", data.seller_id, params, evidence
    )
    if previous is not None:
        return previous
    assert decision is not None
    mapped = _decision_status(decision)
    label = {"executed": "approved", "held": "held", "rejected": "rejected"}.get(
        decision.status, "undetermined"
    )
    output = SellerReviewOutput(
        seller_id=data.seller_id,
        decision=label,  # type: ignore[arg-type]
        risk_score=score,
        reason_code=decision.hold_reason.value if decision.hold_reason else "risk_checked",
        execution_id=decision.execution_id,
        review_id=decision.review_id,
    )
    result = done(output, evidence, **mapped)
    if result.status == TaskStatus.COMPLETED:
        state().ledger.done[key] = result
    _log_market_op(request, "seller_review", output.model_dump(mode="json"))
    return result


async def settlement_hold(request: AgentRequest) -> AgentResult:
    data = _parse(request, SettlementHoldInput)
    if isinstance(data, AgentResult):
        return data
    evidence = _evidence(
        ["settlement_hold"], f"{data.seller_id} 정산 보류: {data.reason_code}", [data.seller_id]
    )
    params = data.model_dump(mode="json", exclude_none=True)
    decision, previous, key = await _gated(
        request, "settlement_hold", data.seller_id, params, evidence
    )
    if previous is not None:
        return previous
    assert decision is not None
    mapped = _decision_status(decision)
    status = {"executed": "held", "held": "pending_review"}.get(decision.status, "rejected")
    output = SettlementHoldOutput(
        seller_id=data.seller_id,
        status=status,  # type: ignore[arg-type]
        reason_code=data.reason_code,
        execution_id=decision.execution_id,
        review_id=decision.review_id,
    )
    result = _result(request, output=output.model_dump(mode="json"), evidence=evidence, **mapped)
    if result.status == TaskStatus.COMPLETED:
        state().ledger.done[key] = result
    _log_market_op(request, "settlement_hold", output.model_dump(mode="json"))
    return result


HANDLERS = {
    "verify_amount": verify_amount,
    "loss_aggregation": loss_aggregation,
    "seller_review": seller_review,
    "settlement_hold": settlement_hold,
}
