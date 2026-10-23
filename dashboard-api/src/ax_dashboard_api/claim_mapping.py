"""support 클레임 skill 출력을 상담원 화면(dashboard-web)이 받는 모델로 바꾼다.

dashboard-api는 support를 import하지 않고 A2A 출력(dict)만 받는다. 화면이 쓰는 상태·검수 값·
규칙 코드·감사 액션 이름은 support와 달라서 여기서 한곳에 모아 바꾼다. 값을 지어내지 않는다:
support에 없는 값은 비우거나 0이다 (`description`, `tracking`, `compensation`).
"""

from __future__ import annotations

from typing import Any

from ax_dashboard_api.envelope import ApiError
from ax_dashboard_api.schemas import (
    Abuse,
    AuditEntry,
    Blocker,
    Claim,
    ClaimDetail,
    ClaimStats,
    DecisionLine,
    Deduction,
    QueueEntry,
    QueueItemLine,
    QueueRun,
    QueueRunResult,
    QueueView,
    Reason,
    Receipt,
    ReceiptItem,
)

# support에는 지급 대기(`approved`)가 있고 화면에는 없다. 화면의 "입고 완료·처리 대기"와 뜻이 같아
# (보상 큐에서 지급을 기다린다) 이 값으로 보낸다.
STATUS_OUT = {"approved": "return_received"}
# 검수 값: 화면은 ok / mismatch / pending, support는 pass / fail / partial / 없음
INSPECTION_OUT = {"pass": "ok", "fail": "mismatch", "partial": "mismatch"}
INSPECTION_IN = {
    "ok": "pass",
    "pass": "pass",
    "mismatch": "fail",
    "fail": "fail",
    "partial": "partial",
}
# 규칙 코드: 화면의 RULE_KO 키에 맞춘다. 대응이 없는 코드는 그대로 둔다(화면이 코드를 보여 준다)
RULE_OUT = {
    "CANCEL_AFTER_SHIPPING": "CANCEL_TOO_LATE",
    "RETURN_WINDOW": "WINDOW_MIND_EXPIRED",
    "NOT_DELIVERED": "NOT_DELIVERED_YET",
    "EVIDENCE_REQUIRED": "NEED_EVIDENCE",
    "ABUSE_FREQUENT": "ABUSE_SUSPECTED",
    "HIGH_AMOUNT": "OVER_AUTO_LIMIT",
    "NOTHING_REFUNDABLE": "ITEM_NOT_IN_ORDER",
    "RETURN_SHIPPING": "RETURN_FEE",
}
# 감사 액션: 화면의 AUDIT_KO 키에 맞춘다
AUDIT_OUT = {
    "decide": "decided",
    "approve": "agent_override",
    "reject": "agent_confirmed",
    "receive": "received",
    "inspection": "inspected",
    "pay": "paid",
}


def inspection_in(value: str) -> str:
    """화면이 보낸 검수 값을 support 값으로 바꾼다. 모르는 값은 422다."""
    try:
        return INSPECTION_IN[value]
    except KeyError:
        raise ApiError(422, "INVALID_REQUEST", f"inspection: 알 수 없는 값 {value}") from None


def _status(value: str) -> str:
    return STATUS_OUT.get(value, value)


def _inspection(value: str | None, *, received: bool) -> str | None:
    if value is None:
        return "pending" if received else None  # 입고했는데 검수 전이면 화면이 "검수 대기"로 본다
    return INSPECTION_OUT.get(value, value)


def _user_id(buyer_id: str) -> int | None:
    """구매자 ID(`usr_<번호>`)에서 화면이 쓰는 숫자 고객 번호를 꺼낸다. 형식이 다르면 None."""
    prefix = "usr_"
    if buyer_id.startswith(prefix) and buyer_id[len(prefix) :].isdecimal():
        return int(buyer_id[len(prefix) :])
    return None


def _manager_approved(audit: list[dict[str, Any]]) -> bool | None:
    for entry in reversed(audit):
        if entry["action"] == "approve":
            return True
        if entry["action"] == "reject":
            return False
    return None


def to_claim(data: dict[str, Any]) -> Claim:
    rejected = data["status"] == "rejected"
    expected = 0 if rejected else data["expected_refund"]  # 거절된 건에 환불 예정액을 보이지 않는다
    return Claim(
        id=data["id"],
        order_id=data["order_id"],
        order_no=data["order_no"],
        type=data["type"],
        reason=data["reason"],
        status=_status(data["status"]),
        created_at=data["created_at"],
        description="",  # 사유 설명 원문은 untrusted라 support가 저장하지 않는다
        unsealed=data["unsealed"],
        evidence_count=data["evidence_count"],
        outcome=data["outcome"],
        resolution=data["resolution"],
        requires_return=data["requires_return"],
        reasons=[
            Reason(
                code=RULE_OUT.get(r["code"], r["code"]),
                level=r["level"],
                message=r["message"],
                rule=r.get("rule"),
            )
            for r in data["reasons"]
        ],
        lines=[DecisionLine(**line) for line in data["lines"]],
        deductions=[
            Deduction(
                code=RULE_OUT.get(d["code"], d["code"]), amount=d["amount"], message=d["message"]
            )
            for d in data["deductions"]
        ],
        expected_refund=expected,
        points_back=0 if rejected else data["points_back"],
        payable=expected,
        return_received_at=data["return_received_at"],
        inspection=_inspection(data["inspection"], received=bool(data["return_received_at"])),
        refund=data["refund_paid"],
        points_returned=data["points_back"] if data["status"] == "completed" else 0,
        processed_at=data["processed_at"],
        conversation_id=data["conversation_id"],
        customer=data["customer"],
        abuse=Abuse(score=data["abuse_score"]),
        decided_by=data["decided_by"],
        agent_note=data["agent_note"],
        manager_approved=_manager_approved(data["audit"]),
        inspection_note=data["inspection_note"],
        user_id=_user_id(data["buyer_id"]),
    )


def to_detail(data: dict[str, Any]) -> ClaimDetail:
    receipt = data.get("receipt")
    if receipt is None:
        raise ApiError(502, "UPSTREAM_ERROR", "support 클레임에 영수증 스냅샷이 없다")
    claim = to_claim(data)
    return ClaimDetail(
        **claim.model_dump(),
        audit=[
            AuditEntry(
                actor=a["actor"],
                action=AUDIT_OUT.get(a["action"], a["action"]),
                detail=a["detail"],
                at=a["at"],
            )
            for a in data["audit"]
        ],
        receipt=Receipt(
            order_no=receipt["order_no"],
            status=receipt["status"],
            items_total=receipt["items_total"],
            shipping_fee=receipt["shipping_fee"],
            points_used=receipt["points_used"],
            paid_amount=receipt["paid_amount"],
            paid_at=receipt["paid_at"],
            delivered_at=receipt["delivered_at"],
            items=[ReceiptItem(**item) for item in receipt["items"]],
        ),
    )


def filter_by_status(claims: list[Claim], status: str | None) -> list[Claim]:
    """화면 상태 값 기준으로 거른다 (`approved`는 `return_received`로 보이므로 변환 뒤에)."""
    return [c for c in claims if status is None or c.status == status]


def to_stats(data: dict[str, Any]) -> ClaimStats:
    by_status: dict[str, int] = {}
    for status, count in data["by_status"].items():
        key = _status(status)
        by_status[key] = by_status.get(key, 0) + count
    rule_hits: dict[str, int] = {}
    for code, count in data["rule_hits"].items():
        key = RULE_OUT.get(code, code)
        rule_hits[key] = rule_hits.get(key, 0) + count
    return ClaimStats(
        total=data["total"],
        by_status=by_status,
        rule_hits=rule_hits,
        agent_reviews=data["agent_reviews"],
        agent_overrides=data["agent_overrides"],
        overridden_rules={},
    )


def _entry(data: dict[str, Any]) -> QueueEntry:
    return QueueEntry(
        position=data["position"],
        claim_id=data["claim_id"],
        order_no=data["order_no"],
        customer=data["customer"],
        type=data["type"],
        reason=data["reason"],
        received_at=data["received_at"] or "",
        inspection=INSPECTION_OUT.get(data["inspection"])
        if data["inspection"]
        else (None if data["ready"] else "pending"),
        amount=data["amount"],
        compensation=0,  # 보상 쿠폰은 클레임이 아니라 request_compensation으로 지급한다
        ready=data["ready"],
        blockers=[Blocker(code=b["code"], message=b["message"]) for b in data["blockers"]],
        items=[QueueItemLine(**i) for i in data["items"]],
    )


def to_queue(data: dict[str, Any]) -> QueueView:
    return QueueView(
        queue=[_entry(e) for e in data["queue"]],
        awaiting_return=[to_claim(c) for c in data["awaiting_return"]],
    )


def to_run(data: dict[str, Any]) -> QueueRun:
    return QueueRun(
        results=[
            QueueRunResult(
                claim_id=r["claim_id"],
                result=r["result"],
                blockers=[Blocker(code=b["code"], message=b["message"]) for b in r["blockers"]],
                refund=r["refund"],
                compensation=0 if r["result"] == "paid" else None,
            )
            for r in data["results"]
        ],
        paid=data["paid"],
        held=data["held"],
        queue=[_entry(e) for e in data["queue"]],
    )
