"""`diagnose_payment`(읽기): 문의의 주문에서 결제가 어디서 막혔는지 주문·결제 사실로 알려 준다.

고객이 쓴 문장은 읽지 않는다. 주문(shop-api)의 상태와 결제 시도 기록으로 코드가 상황과 다음 할 일을
정한다. 아무것도 바꾸지 않는다: 재결제·취소·환불은 고객이나 담당자가 따로 한다.
결제 서버 장애(`PG_UNAVAILABLE`)로 보이면 원인을 그렇게 알려 주되,
장애 보상은 이 skill이 정하지 않는다.
"""

from __future__ import annotations

from typing import Any

from ax_agent_template.server import error_result
from ax_contracts.schemas import AgentRequest, AgentResult, Evidence, TaskStatus
from support_agent import buyer_ids, shop_client
from support_agent import handlers as h
from support_agent.payment_schemas import (
    DiagnosePaymentInput,
    PaymentAttemptOut,
    PaymentDiagnosis,
    PaymentFacts,
)

SERVICE_FAULT_CODES = frozenset({"PG_UNAVAILABLE"})
DECLINED_CODES = frozenset({"PG_DECLINED"})


PAYMENT_FAILURE_REASON = "payment_failure"  # 보상 사유 코드 (`compensation.UNIT_VALUE_BY_REASON`)


def server_fault_attempts(data: dict[str, Any]) -> list[dict[str, Any]]:
    """주문 기록에서 결제 서버 문제 코드(`PG_UNAVAILABLE`)로 실패한 결제 시도들.

    카드 거절(`PG_DECLINED`) 같은 고객 쪽 실패는 서버 장애 보상 대상이 아니라서 넣지 않는다.
    """
    return [
        p
        for p in data.get("payments", [])
        if p.get("status") == "failed" and p.get("failure_code") in SERVICE_FAULT_CODES
    ]


def _attempts(data: dict[str, Any]) -> list[PaymentAttemptOut]:
    return [
        PaymentAttemptOut(
            payment_id=p["payment_id"],
            amount=p.get("amount", 0),
            method_code=p.get("method_code"),
            status=p.get("status", ""),
            failure_code=p.get("failure_code"),
            created_at=p.get("created_at"),
        )
        for p in data.get("payments", [])
    ]


def _cause(code: str | None) -> str:
    if code in SERVICE_FAULT_CODES:
        return "payment_service_fault"
    if code in DECLINED_CODES:
        return "payment_declined"
    return "unknown"


def diagnose(data: dict[str, Any]) -> PaymentDiagnosis:
    """주문 원본으로 상황을 정한다 (순수 함수, 고객 원문을 쓰지 않는다)."""
    attempts = _attempts(data)
    succeeded = [a for a in attempts if a.status == "succeeded"]
    failed = [a for a in attempts if a.status == "failed"]
    status = data.get("status", "")
    total = data.get("total_amount", 0)
    facts = PaymentFacts(
        order_id=data["order_id"],
        order_status=status,
        total_amount=total,
        attempts=attempts,
        succeeded_count=len(succeeded),
        failed_count=len(failed),
        last_failure_code=failed[-1].failure_code if failed else None,
    )

    if len(succeeded) > 1:
        extra = sum(a.amount for a in succeeded[1:])
        return PaymentDiagnosis(
            situation="duplicate_payment",
            next_action="review_refund",
            message=f"성공한 결제가 {len(succeeded)}건이다. 중복으로 보이는 금액 {extra}원",
            duplicate_amount=extra,
            facts=facts,
            notes=["담당자가 결제 기록을 확인한 뒤 환불을 검토한다 (자동으로 환불하지 않는다)"],
        )
    if status == "paid":
        if len(succeeded) == 1 and succeeded[0].amount == total:
            notes = [f"앞서 결제가 {len(failed)}번 실패했다"] if failed else []
            return PaymentDiagnosis(
                situation="paid",
                next_action="none",
                message="결제가 정상으로 끝났다",
                facts=facts,
                notes=notes,
            )
        return PaymentDiagnosis(
            situation="inconsistent",
            next_action="handoff_review",
            message="주문은 결제 완료인데 결제 기록의 금액이나 건수가 맞지 않는다",
            facts=facts,
        )
    if status == "payment_failed":
        code = facts.last_failure_code
        cause = _cause(code)
        if cause == "payment_service_fault":
            return PaymentDiagnosis(
                situation="payment_failed",
                next_action="wait_and_retry",
                message=f"결제가 {len(failed)}번 실패했다. 결제 서버 문제로 보인다 ({code})",
                likely_cause="payment_service_fault",
                facts=facts,
                notes=["잠시 뒤 같은 주문으로 다시 결제할 수 있다 (주문은 유지된다)"],
            )
        return PaymentDiagnosis(
            situation="payment_failed",
            next_action="retry_payment",
            message=f"결제가 {len(failed)}번 실패했다 ({code or '코드 없음'})",
            likely_cause=cause,  # type: ignore[arg-type]
            facts=facts,
            notes=["같은 주문으로 다른 결제 수단을 골라 다시 결제할 수 있다"],
        )
    if status == "pending_payment":
        return PaymentDiagnosis(
            situation="not_attempted",
            next_action="complete_payment",
            message="주문만 만들어졌고 결제 시도가 없다",
            facts=facts,
        )
    if status == "cancelled":
        notes = (
            ["성공한 결제가 있었다면 환불이 처리됐는지 환불 기록으로 확인한다"] if succeeded else []
        )
        return PaymentDiagnosis(
            situation="cancelled",
            next_action="none",
            message="주문이 취소됐다",
            facts=facts,
            notes=notes,
        )
    return PaymentDiagnosis(
        situation="other",
        next_action="handoff_review",
        message=f"주문 상태 '{status}'는 결제 문제로 진단하지 않는다",
        facts=facts,
    )


async def diagnose_payment(request: AgentRequest) -> AgentResult:
    parsed = h._parse(request, DiagnosePaymentInput)
    if isinstance(parsed, AgentResult):
        return parsed
    inquiry = h.STORE.get(parsed.inquiry_id)
    if inquiry is None:
        return h._not_found(request, parsed.inquiry_id)
    if not inquiry.order_id:
        none = PaymentDiagnosis(
            situation="other", next_action="handoff_review", message="문의에 주문이 없다"
        )
        return h._done(request, none.model_dump(mode="json"), outcome="no_order")
    if shop_client.order_source() == "seed":
        seed = PaymentDiagnosis(
            situation="other",
            next_action="handoff_review",
            message="주문 출처가 시드라 결제 기록이 없다",
        )
        return h._done(request, seed.model_dump(mode="json"), outcome="order_source_seed")
    try:
        data = await shop_client.fetch_order_facts(inquiry.order_id)
    except shop_client.ShopError as exc:
        return error_result(request.meta, TaskStatus.FAILED, "shop_unavailable", str(exc))
    buyer_id = buyer_ids.from_number(inquiry.user_id)
    if data is None or data.get("buyer_id") != buyer_id:  # 남의 주문 결제 기록은 보여주지 않는다
        return h._not_found(request, parsed.inquiry_id)

    result = diagnose(data)
    attempts = result.facts.attempts if result.facts else []
    evidence = Evidence(
        queries=["shop_order"],
        ref_ids=[f"inquiry:{inquiry.id}", f"order:{data['order_id']}"]
        + [f"payment:{a.payment_id}" for a in attempts],
        data_summary=f"주문 {data['order_id']}: {result.situation} → {result.next_action}",
    )
    return h._done(
        request, result.model_dump(mode="json"), outcome=result.situation, evidence=evidence
    )


PAYMENT_HANDLERS = {"diagnose_payment": diagnose_payment}
