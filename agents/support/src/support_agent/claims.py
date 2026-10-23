"""클레임 저장소 (프로세스 메모리). 재시작하면 사라진다. DB 연결은 이후 단계다."""

from __future__ import annotations

import itertools
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from support_agent.claim_rules import Decision
from support_agent.claim_schemas import (
    Claim,
    ClaimAudit,
    ClaimDeduction,
    ClaimLine,
    ClaimQueue,
    ClaimReasonHit,
    ClaimReceipt,
    ClaimReceiptItem,
    ClaimStats,
    QueueEntry,
    QueueItemLine,
)
from support_agent.orders import Order

STATUS_BY_OUTCOME = {
    "auto_rejected": "rejected",
    "needs_info": "needs_info",
    "needs_review": "needs_review",
    "approved_pending_return": "awaiting_return",
    "auto_approved": "approved",
}
ACTOR = "support"


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ClaimStore:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self._claims: dict[int, Claim] = {}

    # ---- 읽기
    def get(self, claim_id: int) -> Claim | None:
        return self._claims.get(claim_id)

    def list(self, status: str | None = None) -> list[Claim]:
        items = [c for c in self._claims.values() if status is None or c.status == status]
        return sorted(items, key=lambda c: c.id, reverse=True)

    def count_for_buyer(self, buyer_id: str) -> int:
        return sum(1 for c in self._claims.values() if c.buyer_id == buyer_id)

    def has_open_claim(self, order_id: str, order_item_ids: set[str]) -> bool:
        """같은 주문 품목에 아직 끝나지 않은 클레임이 있으면 중복 접수로 본다."""
        for claim in self._claims.values():
            if claim.order_id != order_id or claim.status in ("rejected", "completed"):
                continue
            if order_item_ids & {line.order_item_id for line in claim.lines}:
                return True
        return False

    def stats(self) -> ClaimStats:
        claims = list(self._claims.values())
        hits = Counter(r.code for c in claims for r in c.reasons)
        return ClaimStats(
            total=len(claims),
            by_status=dict(Counter(c.status for c in claims)),
            rule_hits=dict(hits),
            agent_reviews=sum(1 for c in claims if c.decided_by),
        )

    def queue(self) -> ClaimQueue:
        """보상 큐: 환불할 금액이 있고 (반품 없는 승인 또는 반품 입고)인 클레임."""
        entries: list[QueueEntry] = []
        for claim in sorted(self._claims.values(), key=lambda c: c.id):
            if claim.status not in ("approved", "return_received") or claim.expected_refund <= 0:
                continue
            blockers: list[ClaimReasonHit] = []
            if claim.status == "return_received" and claim.inspection != "pass":
                # 화면(BLOCKER_KO)과 같은 코드: NOT_INSPECTED 검수 대기, ON_HOLD 상담원 확인
                if claim.inspection is None:
                    blockers.append(
                        ClaimReasonHit(
                            code="NOT_INSPECTED", level="block", message="검수 전이라 보류"
                        )
                    )
                else:
                    blockers.append(
                        ClaimReasonHit(
                            code="ON_HOLD",
                            level="block",
                            message=f"검수 {claim.inspection}이라 보류",
                        )
                    )
            entries.append(
                QueueEntry(
                    position=len(entries) + 1,
                    claim_id=claim.id,
                    order_no=claim.order_no,
                    customer=claim.customer,
                    type=claim.type,
                    reason=claim.reason,
                    received_at=claim.return_received_at or claim.updated_at,
                    inspection=claim.inspection,
                    amount=claim.expected_refund,
                    ready=not blockers,
                    blockers=blockers,
                    items=[QueueItemLine(title=line.title, qty=line.qty) for line in claim.lines],
                )
            )
        return ClaimQueue(queue=entries, awaiting_return=self.list("awaiting_return"))

    # ---- 쓰기 (호출하는 쪽이 execute()를 거친 뒤에만 부른다)
    def create(
        self,
        *,
        context_id: str,
        tenant_id: str,
        order: Order,
        customer: str,
        claim_type: str,
        reason: str,
        decision: Decision,
        evidence_count: int,
        unsealed: bool,
        conversation_id: int | None,
    ) -> Claim:
        stamp = now_iso()
        claim = Claim(
            id=next(self._ids),
            tenant_id=tenant_id,
            context_id=context_id,
            order_id=order.id,
            order_no=order.order_no,
            buyer_id=order.buyer_id,
            customer=customer,
            type=claim_type,  # type: ignore[arg-type]
            reason=reason,  # type: ignore[arg-type]
            status=STATUS_BY_OUTCOME[decision.outcome],  # type: ignore[arg-type]
            created_at=stamp,
            updated_at=stamp,
            outcome=decision.outcome,
            resolution=decision.resolution,
            requires_return=decision.requires_return,
            evidence_count=evidence_count,
            unsealed=unsealed,
            conversation_id=conversation_id,
            reasons=[ClaimReasonHit(**_fields(h)) for h in decision.reasons],
            lines=[ClaimLine(**_fields(line)) for line in decision.lines],
            deductions=[
                ClaimDeduction(code=c, amount=a, message=m) for c, a, m in decision.deductions
            ],
            shipping_refund=decision.shipping_refund,
            expected_refund=decision.expected_refund,
            points_back=decision.points_back,
            abuse_score=decision.abuse_score,
            receipt=_receipt(order),
        )
        self._audit(claim, "decide", outcome=decision.outcome, abuse_score=decision.abuse_score)
        self._claims[claim.id] = claim
        return claim

    def set_status(self, claim: Claim, status: str, action: str, **detail: Any) -> Claim:
        claim.status = status  # type: ignore[assignment]
        claim.updated_at = now_iso()
        self._audit(claim, action, **detail)
        return claim

    @staticmethod
    def _audit(claim: Claim, action: str, **detail: Any) -> None:
        claim.audit.append(ClaimAudit(actor=ACTOR, action=action, detail=detail, at=now_iso()))


def _receipt(order: Order) -> ClaimReceipt:
    """판정에 쓴 주문 사실을 그대로 적어 둔다. 금액은 주문 값에서 코드가 계산한다."""
    items_total = sum(i.paid_amount + i.points_share for i in order.items)
    return ClaimReceipt(
        order_no=order.order_no,
        status=order.status,
        items_total=items_total,
        shipping_fee=order.shipping_fee,
        points_used=order.points_used,
        paid_amount=items_total + order.shipping_fee - order.points_used,
        paid_at=order.paid_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        delivered_at=order.delivered_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        if order.delivered_at
        else None,
        items=[
            ClaimReceiptItem(
                id=i.id,
                title=i.title,
                qty=i.qty,
                paid_amount=i.paid_amount,
                points_share=i.points_share,
                refunded_qty=i.refunded_qty,
            )
            for i in order.items
        ],
    )


def _fields(obj: Any) -> dict[str, Any]:
    return dict(vars(obj))
