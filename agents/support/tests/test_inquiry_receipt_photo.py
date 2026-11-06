"""문의 접수(`create_inquiry`) 때 붙은 영수증 사진은 대조 요약만 남기고 저장하지 않는다."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from support_agent import claim_handlers, guard, handlers, receipts
from support_agent.claims import ClaimStore
from support_agent.main import app
from support_agent.schemas import Conversation
from support_agent.store import InquiryStore

RECEIPT = """CATBOOKS 영수증
주문번호: ORD-2026-0101
결제일시 2026-10-10 12:30
고양이의 하루 1 15,000원
탁상 달력 1 15,000원
상품합계 30,000원
배송비 3,000원
총 결제금액 33,000원
"""
SECRET_BYTES = "U0VDUkVUX1JFQ0VJUFRfUEhPVE8="  # 사진 바이트가 어디에도 남으면 안 된다


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> InquiryStore:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.delenv("SUPPORT_MOCK", raising=False)
    monkeypatch.delenv("SUPPORT_ORDER_SOURCE", raising=False)
    store = InquiryStore()
    monkeypatch.setattr(handlers, "STORE", store)
    monkeypatch.setattr(claim_handlers, "CLAIMS", ClaimStore())
    receipts.set_vision_extractor(None)
    yield store
    receipts.set_vision_extractor(None)


def call(
    skill: str,
    input: dict[str, Any],
    untrusted: dict[str, Any] | None = None,
    requester: str = "shop-api",
) -> dict[str, Any]:
    body = {
        "meta": {
            "context_id": "ctx_ui_0001",
            "token": f"mock:{requester}",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        },
        "skill": skill,
        "input": input,
        "untrusted": untrusted or {},
    }
    return TestClient(app).post("/a2a", json=body).json()


def submit(images: Any = None, order_id: str | None = "101") -> dict[str, Any]:
    untrusted: dict[str, Any] = {"subject": "영수증 확인", "message": "영수증을 첨부합니다"}
    if images is not None:
        untrusted["images"] = images
    return call(
        "create_inquiry",
        {"user_id": 1, "customer_name": "김하늘", "order_id": order_id},
        untrusted,
    )


def photo() -> list[dict[str, str]]:
    return [{"ref": "img_1", "data_base64": SECRET_BYTES, "media_type": "image/jpeg"}]


def stored(result: dict[str, Any]) -> Conversation:
    conversation = handlers.STORE.get(result["output"]["id"])
    assert conversation is not None
    return conversation


def test_a_mismatching_photo_leaves_only_a_summary(_fresh: InquiryStore) -> None:
    async def extractor(images: list[Any]) -> receipts.ReceiptFacts:
        return receipts.ReceiptFacts(total=99999, order_no="ORD-2026-0101")

    receipts.set_vision_extractor(extractor)
    result = submit(photo())
    assert result["status"] == "completed"
    check = stored(result).receipt_check
    assert check is not None
    assert (check.images_status, check.verdict, check.image_count) == ("read", "mismatch", 1)
    assert "RECEIPT_AMOUNT_MISMATCH" in check.mismatches
    dumped = stored(result).model_dump_json()
    assert SECRET_BYTES not in dumped and "99999" not in dumped  # 사진·영수증 값은 남기지 않는다


def test_a_matching_photo_is_consistent(_fresh: InquiryStore) -> None:
    async def extractor(images: list[Any]) -> receipts.ReceiptFacts:
        return receipts.parse_receipt_text(guard.mask_pii(RECEIPT)[0])

    receipts.set_vision_extractor(extractor)
    check = stored(submit(photo())).receipt_check
    assert check is not None and check.verdict == "consistent" and check.mismatches == []


def test_without_an_extractor_the_inquiry_is_still_received(_fresh: InquiryStore) -> None:
    result = submit(photo())
    assert result["status"] == "completed"
    check = stored(result).receipt_check
    assert check is not None and check.images_status == "unavailable" and check.verdict is None
    assert "직접 확인" in (check.note or "")


def test_a_broken_extractor_does_not_block_the_inquiry(_fresh: InquiryStore) -> None:
    async def broken(images: list[Any]) -> receipts.ReceiptFacts:
        raise RuntimeError("vision down")

    receipts.set_vision_extractor(broken)
    result = submit(photo())
    assert result["status"] == "completed"
    check = stored(result).receipt_check
    assert check is not None and check.images_status == "failed"
    assert "vision down" not in (check.note or "")


def test_too_many_or_wrong_type_photos_are_skipped(_fresh: InquiryStore) -> None:
    many = [{"ref": f"img_{i}", "media_type": "image/png"} for i in range(5)]
    assert stored(submit(many)).receipt_check.images_status == "skipped"  # type: ignore[union-attr]
    pdf = [{"ref": "x", "media_type": "application/pdf"}]
    assert stored(submit(pdf)).receipt_check.images_status == "skipped"  # type: ignore[union-attr]


def test_an_inquiry_without_photos_has_no_receipt_check(_fresh: InquiryStore) -> None:
    assert stored(submit()).receipt_check is None
    assert stored(submit([])).receipt_check is None


def test_the_customer_response_never_carries_the_check(_fresh: InquiryStore) -> None:
    async def extractor(images: list[Any]) -> receipts.ReceiptFacts:
        return receipts.ReceiptFacts(total=99999)

    receipts.set_vision_extractor(extractor)
    output = submit(photo())["output"]
    assert "receipt_check" not in output and "mismatch" not in str(output)


def test_staff_can_read_the_check_from_get_inquiry(_fresh: InquiryStore) -> None:
    result = submit(photo())
    got = call("get_inquiry", {"inquiry_id": result["output"]["id"]}, requester="dashboard-api")
    assert got["output"]["receipt_check"]["images_status"] == "unavailable"


def test_old_documents_without_the_field_still_load_and_the_check_round_trips() -> None:
    result = submit(photo())
    doc = stored(result).model_dump(mode="json")
    assert Conversation.model_validate(doc).receipt_check == stored(result).receipt_check
    doc.pop("receipt_check")
    assert Conversation.model_validate(doc).receipt_check is None  # 옛 저장 문서
