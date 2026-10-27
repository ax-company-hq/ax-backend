"""finance skill 핸들러.

| skill | 등급 | 요청자 | 하는 일 |
| --- | --- | --- | --- |
| `seller_review` | B | orchestrator | 마켓운영: 서류·위험 점수로 판매자 승인 요청 (DEP-FIN-03) |
| `settlement_hold` | B | orchestrator | 마켓운영: 위험 판매자 정산 보류 요청 (DEP-FIN-04) |
| `closing_report` | read | orchestrator, data | ① 기간 손익 요약 (`closing.py`) |
| `audit_report` | read | orchestrator, data | ⑦ 사후 감사 결과 (`audit.py`) |

`verify_amount`·`review_hold`는 재무 재설계(2026-10-23)로, `loss_aggregation`은 2026-10-27에
없앴다 (재무는 장애 대응 흐름의 단계가 아니다 — 요구사항 DEP-FIN-01, 공통 구성안 결정 22).
장애 비용은 평상시 장부로 들어가 `closing_report`·`audit_report`에서 드러난다. 금액 다시 계산 규칙은
`recheck.py`에 남아 사후 감사가 쓴다. 캣북스 클레임 건별 확인과 `review_threshold_change`(AI가 기준
변경을 판단)도 재무 일이 아니라 없앴다. 한도는 제안만 하고(`policy.py`) 사람이 기준표 PR로 바꾼다.

- 숫자는 전부 코드로 계산한다. 요청 쪽이 적은 총액·등급은 믿지 않는다.
- 상태 변경(판매자 승인, 정산 보류)은 `execute()`가 `executed`를 돌려준 뒤에만 기록한다.
- 외부 입력(`request.untrusted`, 판매자 소개 등)은 읽지 않는다.
- 저장소는 메모리다. TODO: 정산·심사 기록을 shop-api 쓰기 API(관문 뒤)와 영속 저장소로 옮긴다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
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
    RequestMeta,
    TaskStatus,
)
from finance_agent import audit, closing
from finance_agent import books as bk
from finance_agent import finance_config as fc
from finance_agent import ledger as lg
from finance_agent.books import Books, load_books
from finance_agent.recheck import recompute
from finance_agent.schemas import (
    AuditReportInput,
    AuditReportOutput,
    ClosingReportInput,
    ClosingReportOutput,
    SellerReviewInput,
    SellerReviewOutput,
    SettlementHoldInput,
    SettlementHoldOutput,
)
from finance_agent.sources import Impact, LiveSources, MockSources, Sources, order_key

__all__ = ["HANDLERS", "SUBSCRIBED_EVENTS", "on_event", "recompute", "reset_state", "state"]


class Ledger:
    """이벤트로 쌓는 지급·환불 기록과 처리 결과."""

    def __init__(self) -> None:
        self.coupons: dict[str, dict[str, int]] = {}  # context_id → {"count", "total"}
        self.refunds: dict[str, int] = {}  # order_id → 환불 누적
        self.seen_events: set[str] = set()
        self.done: dict[str, AgentResult] = {}  # 멱등성 키 → 끝난 상태 변경 결과
        self.market_ops: list[dict[str, Any]] = []  # 판매자 심사·정산 보류 결과 기록
        self.coupon_events: dict[str, list[dict[str, Any]]] = {}  # context_id → 보상 쿠폰 이벤트

    def compensation(self, context_id: str) -> dict[str, int]:
        return self.coupons.get(context_id, {"count": 0, "total": 0})


class State:
    def __init__(self, sources: Sources | None = None) -> None:
        self.sources: Sources = sources or (MockSources() if is_mock("finance") else LiveSources())
        self.ledger = Ledger()
        self.last_self_test: dict[str, Any] | None = None  # 마지막 "재무 AI 테스트" 결과
        self.records = FinanceRecords()
        self.books_loader: Callable[[], Awaitable[Books]] = load_books  # 테스트에서 바꾼다
        # 사건별 쿠폰 장부(⑨)의 실행 기록과 기준표. 테스트에서 바꾼다
        self.executions_loader: Callable[[str | None], Awaitable[lg.ExecutionFeed]] = (
            lg.load_executions
        )
        self.thresholds_loader: Callable[[], Awaitable[dict[str, Any] | None]] = lg.load_thresholds

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
        ledger.coupon_events.setdefault(envelope.context_id, []).append(
            lg.coupon_from_event(envelope.context_id, data, envelope.occurred_at)
        )
    elif envelope.type == EventType.PAYMENT_REFUNDED:
        order_id, amount = order_key(data.get("order_id")), _positive_int(data.get("refund_amount"))
        if order_id is not None and amount is not None:
            ledger.refunds[order_id] = ledger.refunds.get(order_id, 0) + amount


SUBSCRIBED_EVENTS = [EventType.COUPON_ISSUED, EventType.PAYMENT_REFUNDED]


# ---------------------------------------------------------------- 사건 보상 쿠폰 공용
async def compensation_ledger(
    context_id: str, feed: lg.ExecutionFeed | None = None
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """한 사건의 보상 쿠폰 장부 줄과 출처 (`ledger.merge`)."""
    if feed is None:
        feed = await state().executions_loader(context_id)
    event_rows = state().ledger.coupon_events.get(context_id, [])
    return lg.merge(context_id, feed, event_rows, lg.events_connected())


def incident_meta(context_id: str) -> dict[str, Any]:
    """사건의 장애 구간·부품 중 재무가 혼자 아는 것. (목 모드) 시드 → 모름.

    실제 모드에서는 모름이다. 구간을 알려 주던 총괄의 `loss_aggregation` 요청을 2026-10-27에
    없앴다(재무는 장애 대응 흐름의 단계가 아니다). 실제 모드 구간은 `incident_info`가
    총괄 사건 목록을 읽어서 채운다.
    """
    if is_mock("finance"):
        seeded = bk.seed_incidents().get(context_id)
        if seeded:
            return seeded | {"category": None}
    return {"component": None, "window_from": None, "window_to": None, "category": None}


async def incident_info(context_id: str) -> dict[str, Any]:
    """사건의 장애 구간·부품. 목 모드는 시드, 실제 모드는 총괄 사건 목록(읽기 전용).

    총괄에게 사건 **중**에 묻는 것이 아니다 — 평상시 사후 감사·사건 조회에서만 읽는다
    ([협업] #417 제안 A, 요청 #418, 계약 `data_access/orchestrator_incidents.py`).
    못 받으면 모름(None)이고 추정값을 만들지 않는다(fail-closed).
    """
    info = incident_meta(context_id)
    if info["window_from"] and info["window_to"]:
        return info
    found = await state().sources.incident_window(context_id)
    if not found:
        return info
    return info | {k: v for k, v in found.items() if v is not None}


async def incident_impact(
    context_id: str, meta: RequestMeta | None = None, info: dict[str, Any] | None = None
) -> Impact | None:
    """data `impact_analysis` 결과 그대로(다시 계산하지 않음). 구간을 모르면 None.

    `info`를 받으면 사건 구간을 다시 조회하지 않는다 (`incident_view`가 한 번만 읽게).
    """
    if info is None:
        info = await incident_info(context_id)
    window = None
    if info["window_from"] and info["window_to"]:
        window = {
            "window_from": fc.iso(info["window_from"]),
            "window_to": fc.iso(info["window_to"]),
            **({"category": info["category"]} if info.get("category") else {}),
        }
    base = meta or RequestMeta(
        context_id=context_id, token="finance-dashboard", deadline=_now() + timedelta(seconds=15)
    )
    return await state().sources.impact(
        base.model_copy(update={"context_id": context_id}), context_id, window
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

    async def lookup(ctx: str) -> Impact | None:
        return await incident_impact(ctx, request.meta)

    report, missing = await audit.audit_report(
        books, data.days, impact_lookup=lookup, context_id=data.context_id
    )
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
    "seller_review": seller_review,
    "settlement_hold": settlement_hold,
    "closing_report": closing_report,
    "audit_report": audit_report,
}
