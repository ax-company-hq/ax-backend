"""finance skill 핸들러.

| skill | 등급 | 요청자 | 하는 일 |
| --- | --- | --- | --- |
| `loss_aggregation` | read | orchestrator | data 영향 분석 + 지급 보상 = 손실 리포트 (DEP-FIN-01) |
| `seller_review` | B | orchestrator | 마켓운영: 서류·위험 점수로 판매자 승인 요청 (DEP-FIN-03) |
| `settlement_hold` | B | orchestrator | 마켓운영: 위험 판매자 정산 보류 요청 (DEP-FIN-04) |
| `closing_report` | read | orchestrator, data | ① 기간 손익 요약 (`closing.py`) |
| `audit_report` | read | orchestrator, data | ⑦ 사후 감사 결과 (`audit.py`) |
| `review_threshold_change` | read | safety | 기준 바꾸기 판단 (`threshold_review.py`) |

`verify_amount`·`review_hold`는 재무 재설계(2026-10-23)로 없앴다. 금액 다시 계산 규칙은
`recheck.py`에 남아 캣북스 확인과 사후 감사가 쓴다.

- 숫자는 전부 코드로 계산한다. 요청 쪽이 적은 총액·등급은 믿지 않는다.
- 상태 변경(판매자 승인, 정산 보류)은 `execute()`가 `executed`를 돌려준 뒤에만 기록한다.
- 외부 입력(`request.untrusted`, 판매자 소개 등)은 읽지 않는다.
- 저장소는 메모리다. TODO: 정산·심사 기록을 shop-api 쓰기 API(관문 뒤)와 영속 저장소로 옮긴다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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
from finance_agent import audit, closing
from finance_agent import finance_config as fc
from finance_agent.books import Books, load_books
from finance_agent.recheck import recompute
from finance_agent.review import ReviewPolicy, load_policy
from finance_agent.schemas import (
    AuditReportInput,
    AuditReportOutput,
    ClosingReportInput,
    ClosingReportOutput,
    LossAggregationInput,
    LossReport,
    SellerReviewInput,
    SellerReviewOutput,
    SettlementHoldInput,
    SettlementHoldOutput,
    ThresholdReviewInput,
)
from finance_agent.sources import LiveSources, MockSources, Sources, order_key
from finance_agent.threshold_review import decide_threshold

__all__ = ["HANDLERS", "SUBSCRIBED_EVENTS", "on_event", "recompute", "reset_state", "state"]


class Ledger:
    """이벤트로 쌓는 지급·환불 기록과 처리 결과."""

    def __init__(self) -> None:
        self.coupons: dict[str, dict[str, int]] = {}  # context_id → {"count", "total"}
        self.refunds: dict[str, int] = {}  # order_id → 환불 누적
        self.seen_events: set[str] = set()
        self.done: dict[str, AgentResult] = {}  # 멱등성 키 → 끝난 상태 변경 결과
        self.market_ops: list[dict[str, Any]] = []  # 판매자 심사·정산 보류 결과 기록
        self.loss_reports: dict[str, dict[str, Any]] = {}  # context_id → 마지막 손실 리포트
        self.catbooks_items: dict[str, dict[str, Any]] = {}  # 캣북스 클레임 확인 결과
        self.catbooks_sync: dict[str, Any] | None = None  # 마지막 캣북스 가져오기 요약
        self.threshold_reviewed: dict[str, AgentResult] = {}  # change_id → 기준 바꾸기 판단
        self.threshold_reviews: list[dict[str, Any]] = []  # 대시보드 조회용 기준 바꾸기 판단

    def compensation(self, context_id: str) -> dict[str, int]:
        return self.coupons.get(context_id, {"count": 0, "total": 0})


class State:
    def __init__(self, sources: Sources | None = None) -> None:
        self.sources: Sources = sources or (MockSources() if is_mock("finance") else LiveSources())
        self.ledger = Ledger()
        self.policy: ReviewPolicy = load_policy()
        self.last_self_test: dict[str, Any] | None = None  # 마지막 "재무 AI 테스트" 결과
        self.records = FinanceRecords()
        self.books_loader: Callable[[], Awaitable[Books]] = load_books  # 테스트에서 바꾼다

    async def books(self) -> Books:
        return await self.books_loader()

    def held_sellers(self) -> set[str]:
        """정산 보류(`settlement_hold`)가 걸린 판매자."""
        return {
            op["seller_id"]
            for op in self.ledger.market_ops
            if op.get("skill") == "settlement_hold" and op.get("status") == "held"
        }


class FinanceRecords:
    """사람의 결정 기록 (메모리, 데모용). TODO: 영속 저장소로 옮긴다."""

    def __init__(self) -> None:
        self.closings: dict[tuple[str, str], dict[str, Any]] = {}  # (period, date) → {by, at}
        self.recon_notes: dict[str, str] = {}  # payment_id → 처리 메모
        self.recon_note_log: list[dict[str, Any]] = []
        self.batches: dict[str, dict[str, Any]] = {}  # 정산 기간 → 묶음 요청 상태


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
        order_id, amount = order_key(data.get("order_id")), _positive_int(data.get("refund_amount"))
        if order_id is not None and amount is not None:
            ledger.refunds[order_id] = ledger.refunds.get(order_id, 0) + amount


SUBSCRIBED_EVENTS = [EventType.COUPON_ISSUED, EventType.PAYMENT_REFUNDED]


# ---------------------------------------------------------------- loss_aggregation
def _won(value: int) -> str:
    return f"{value:,}원"


async def loss_aggregation(request: AgentRequest) -> AgentResult:
    data = _parse(request, LossAggregationInput)
    if isinstance(data, AgentResult):
        return data
    context_id = data.context_id or request.meta.context_id
    window = {
        "window_from": data.window_from.isoformat(),
        "window_to": data.window_to.isoformat(),
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
    state().ledger.loss_reports[context_id] = report.model_dump(mode="json") | {
        "at": _now().isoformat()
    }
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
        _log_market_op(request, "seller_review", output.model_dump(mode="json"))
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


# ---------------------------------------------------------------- review_threshold_change
async def review_threshold_change(request: AgentRequest) -> AgentResult:
    """safety의 기준 바꾸기 요청 판단. 요청 이유(`request.untrusted`)는 읽지 않는다."""
    data = _parse(request, ThresholdReviewInput)
    if isinstance(data, AgentResult):
        return data
    ledger = state().ledger
    if data.change_id in ledger.threshold_reviewed:  # 같은 요청은 같은 판단
        return ledger.threshold_reviewed[data.change_id].model_copy(update={"meta": request.meta})
    output = decide_threshold(data, state().policy.threshold_change)
    result = _result(
        request,
        TaskStatus.COMPLETED,
        outcome=output.decision,
        output=output.model_dump(mode="json"),
        evidence=_evidence(
            ["ai_review_policy:threshold_change", f"safety:stats:{data.key}"],
            output.explanation,
            [data.change_id],
        ),
    )
    ledger.threshold_reviewed[data.change_id] = result
    ledger.threshold_reviews.append(
        {
            "change_id": data.change_id,
            "context_id": request.meta.context_id,
            "key": data.key,
            "current_value": data.current_value,
            "new_value": data.new_value,
            "baseline": data.baseline,
            "stats": data.stats,
            **output.model_dump(mode="json"),
            "at": _now().isoformat(),
        }
    )
    return result


# ------------------------------------------------------- closing_report·audit_report (읽기)
BOOK_QUERIES = [
    "shop-api:/api/shop/orders",
    "shop-api:/api/shop/coupons",
    "safety:/api/safety/executions",
]


def closing_key(period: str, d: Any) -> tuple[str, str]:
    """결산 확정 기록의 키. 월은 그 달 1일로 맞춘다."""
    start, _ = closing.period_range(period, d)  # type: ignore[arg-type]
    return period, (start if period == "month" else d).isoformat()


async def closing_report(request: AgentRequest) -> AgentResult:
    """① 기간 손익 요약. 대시보드 `GET /api/finance/closing`과 같은 계산이다."""
    data = _parse(request, ClosingReportInput)
    if isinstance(data, AgentResult):
        return data
    books = await state().books()
    d = data.date or fc.today()
    key = closing_key(data.period, d)
    report = await closing.closing_report(books, data.period, d, state().records.closings.get(key))
    output = ClosingReportOutput(
        **{k: v for k, v in report.items() if k not in ("from", "to")},
        window_from=report["from"],
        window_to=report["to"],
        unavailable=books.meta()["unavailable"],
    )
    return _result(
        request,
        TaskStatus.COMPLETED,
        outcome="partial" if output.unavailable else "reported",
        output=output.model_dump(mode="json"),
        evidence=_evidence(
            [*BOOK_QUERIES[:2], f"closing:{data.period}:{key[1]}"], report["commentary"]
        ),
    )


async def audit_report(request: AgentRequest) -> AgentResult:
    """⑦ 사후 감사 결과. 대시보드 `GET /api/finance/audit`과 같은 계산이다."""
    data = _parse(request, AuditReportInput)
    if isinstance(data, AgentResult):
        return data
    books = await state().books()
    report, missing = await audit.audit_report(books, data.days)
    output = AuditReportOutput(
        window_from=report["from"],
        window_to=report["to"],
        stats=report["stats"],
        findings=report["findings"],
        unavailable=books.meta(missing)["unavailable"],
    )
    codes = sorted({f.code for f in output.findings})
    summary = (
        f"최근 {data.days}일 감사 의심 {len(output.findings)}건 ({', '.join(codes) or '없음'})"
    )
    refs = [r for f in output.findings for r in f.refs][:20]
    return _result(
        request,
        TaskStatus.COMPLETED,
        outcome="partial" if output.unavailable else "reported",
        output=output.model_dump(mode="json"),
        evidence=_evidence([*BOOK_QUERIES, f"audit:{data.days}d"], summary, refs),
    )


HANDLERS = {
    "loss_aggregation": loss_aggregation,
    "seller_review": seller_review,
    "settlement_hold": settlement_hold,
    "closing_report": closing_report,
    "audit_report": audit_report,
    "review_threshold_change": review_threshold_change,
}
