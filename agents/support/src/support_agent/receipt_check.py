"""문의 접수 때 붙은 영수증 사진을 읽어 주문과 대조한 **요약만** 남긴다.

- 사진은 읽기만 하고 저장하지 않는다. 남는 것은 `ReceiptCheckRecord`(읽은 결과 상태, 대조 판정,
  불일치 코드, 장수)뿐이다. 금액·품목 같은 영수증 값과 사진 바이트는 담지 않는다.
- 읽기에 실패해도 문의 접수는 막지 않는다. 실패·미연결·제한 초과는 상태로만 남기고 상담원이 직접
  확인한다 (사진을 저장하지 않으므로 고객에게 다시 요청해야 할 수 있다).
- 판정은 `receipts.compare`가 코드로 한다. 환불 판정은 하지 않는다.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from support_agent import receipts, shop_client
from support_agent.analysis_schemas import MAX_IMAGE_BASE64, MAX_IMAGES, EvidenceImageIn
from support_agent.schemas import ReceiptCheckRecord
from support_agent.store import now_iso

logger = logging.getLogger(__name__)

NOT_STORED = "사진은 저장하지 않는다"
UNAVAILABLE_NOTE = f"이미지 분석이 연결되지 않아 읽지 못했다. 상담원이 직접 확인한다 ({NOT_STORED})"
FAILED_NOTE = f"사진을 읽다가 실패했다. 상담원이 직접 확인한다 ({NOT_STORED})"


def read_images(raw: Any) -> tuple[list[EvidenceImageIn], str | None]:
    """`untrusted.images`를 검사한다. 개수·크기를 넘거나 형식이 다르면 `(빈 목록, 이유)`다."""
    if not raw:
        return [], None
    if not isinstance(raw, list) or len(raw) > MAX_IMAGES:
        return [], f"사진은 최대 {MAX_IMAGES}장까지만 읽는다"
    try:
        images = [EvidenceImageIn.model_validate(item) for item in raw]
    except ValidationError:
        return [], "사진 형식이 맞지 않아 읽지 않았다 (jpeg·png·webp, 한 장 약 4.5MB 이하)"
    if sum(len(i.data_base64 or "") for i in images) > MAX_IMAGE_BASE64 * 2:
        return [], "사진 크기가 커서 읽지 않았다"
    return images, None


def merge_facts(
    base: receipts.ReceiptFacts, extra: receipts.ReceiptFacts, notes: list[str]
) -> None:
    """글에서 못 찾은 값만 사진에서 채운다. 둘이 다르면 글을 따르고 알린다."""
    for name in ("total", "paid_on", "order_no", "shipping_fee"):
        mine, other = getattr(base, name), getattr(extra, name)
        if mine is None and other is not None:
            setattr(base, name, other)
        elif mine is not None and other is not None and mine != other:
            notes.append(f"글과 사진의 {name} 값이 달라 글 값을 썼다")
    if not base.items and extra.items:
        base.items = extra.items


def _record(count: int, status: str, **fields: Any) -> ReceiptCheckRecord:
    return ReceiptCheckRecord(
        image_count=count,
        images_status=status,  # type: ignore[arg-type]
        checked_at=now_iso(),
        **fields,
    )


async def check_attached_images(raw: Any, order_id: str | None) -> ReceiptCheckRecord | None:
    """붙은 사진이 없으면 `None`. 있으면 읽어 본 결과 요약을 돌려준다. 예외는 밖으로 내지 않는다."""
    if not raw:
        return None
    count = len(raw) if isinstance(raw, list) else 0
    try:
        images, problem = read_images(raw)
        if problem:
            return _record(count, "skipped", note=problem)
        extractor = receipts.get_vision_extractor()
        if extractor is None:
            return _record(count, "unavailable", note=UNAVAILABLE_NOTE)
        if not order_id:
            return _record(count, "failed", note=f"문의에 주문이 없어 대조 못 했다 ({NOT_STORED})")
        order = await shop_client.load_order(order_id)
        if order is None:
            return _record(count, "failed", note=f"주문을 찾지 못해 대조하지 못했다 ({NOT_STORED})")
        extra = await extractor(
            [receipts.EvidenceImage(i.ref, i.media_type, i.data_base64) for i in images]
        )
        notes: list[str] = []
        facts = receipts.ReceiptFacts()
        merge_facts(facts, extra, notes)
        check = receipts.compare(facts, order)
        if facts.ambiguous_total:
            notes.append("총 결제 금액 후보가 서로 달라 금액은 대조하지 않았다")
        return _record(
            count,
            "read",
            verdict=check.verdict,
            mismatches=list(check.mismatches),
            note=", ".join(notes) or None,
        )
    except Exception:  # 접수를 막지 않는다. 사진·영수증 값은 로그에 남기지 않는다
        logger.warning("영수증 사진 대조 실패", exc_info=False)
        return _record(count, "failed", note=FAILED_NOTE)
