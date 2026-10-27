"""영수증 사진 분석기: LLM은 옮겨 적기만 하고, 코드가 형식을 다시 검사해 대조에 넘긴다."""

import asyncio
import base64
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ax_agent_template.llm import LLMInputError, MockLLMClient
from support_agent import claim_handlers, handlers, receipt_vision, receipts
from support_agent.claims import ClaimStore
from support_agent.main import app
from support_agent.receipt_vision import VisionItem, VisionReceipt
from support_agent.schemas import Analysis
from support_agent.store import InquiryStore

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n-not-a-real-image-").decode("ascii")

# 시드 주문 101과 맞는 영수증 (글 영수증 테스트와 같은 값)
MATCHING = VisionReceipt(
    readable=True,
    total=33000,
    paid_on="2026-10-10",
    order_no="ord-2026-0101",
    shipping_fee=3000,
    items=[
        VisionItem(title="고양이의 하루", qty=1, amount=15000),
        VisionItem(title="탁상 달력", qty=1, amount=15000),
    ],
)


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


def inquiry(store: InquiryStore) -> int:
    return store.create(
        context_id="ctx_inq_0001",
        tenant_id="shop-demo",
        user_id=1,
        customer_name="김하늘",
        subject="문의",
        order_id="101",
        order_no=None,
        first_message="영수증 사진을 올립니다",
        analysis=Analysis(),
    ).id


def call(input: dict[str, Any], untrusted: dict[str, Any]) -> dict[str, Any]:
    body = {
        "meta": {
            "context_id": "ctx_ui_0001",
            "token": "mock:dashboard-api",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        },
        "skill": "analyze_receipt",
        "input": input,
        "untrusted": untrusted,
    }
    return TestClient(app).post("/a2a", json=body).json()


def fake_llm(read: VisionReceipt) -> MockLLMClient:
    return MockLLMClient(responses={VisionReceipt: read})


def photo(ref: str = "img_1") -> dict[str, str]:
    return {"ref": ref, "data_base64": PNG_B64, "media_type": "image/png"}


# ================================================================ 코드가 다시 검사한다
def test_values_the_model_copied_become_facts() -> None:
    facts = receipt_vision.to_facts(MATCHING)
    assert (facts.total, facts.paid_on, facts.order_no, facts.shipping_fee) == (
        33000,
        date(2026, 10, 10),
        "ORD-2026-0101",
        3000,
    )
    assert [(i.title, i.qty, i.amount) for i in facts.items] == [
        ("고양이의 하루", 1, 15000),
        ("탁상 달력", 1, 15000),
    ]
    assert not facts.ambiguous_total


def test_unreliable_values_are_dropped_not_repaired() -> None:
    read = VisionReceipt(
        readable=True,
        total=-5,
        paid_on="2026-13-45",
        order_no="주문 번호 없음",
        shipping_fee=10**12,
        items=[
            VisionItem(title="수량 모름", qty=None, amount=5000),
            VisionItem(title="금액 모름", qty=1, amount=None),
            VisionItem(title="전화 010-1234-5678", qty=1, amount=1000),
            VisionItem(title="", qty=1, amount=1000),
            VisionItem(title="정상 품목", qty=2, amount=7000),
        ],
    )
    facts = receipt_vision.to_facts(read)
    assert (facts.total, facts.paid_on, facts.order_no, facts.shipping_fee) == (
        None,
        None,
        None,
        None,
    )
    assert [i.title for i in facts.items] == ["정상 품목"]


def test_an_unreadable_photo_gives_no_facts() -> None:
    read = VisionReceipt(readable=False, total=33000, order_no="ORD-2026-0101")
    # 읽을 수 없다는 답이면 값도 쓰지 않는다
    assert not receipt_vision.to_facts(read).found_anything()


# ================================================================ 분석기
def test_the_extractor_sends_the_photo_and_the_copy_only_prompt() -> None:
    llm = fake_llm(MATCHING)
    extract = receipt_vision.make_extractor(lambda: llm)
    facts = asyncio.run(extract([receipts.EvidenceImage("img_1", "image/png", PNG_B64)]))
    assert facts.total == 33000
    assert len(llm.image_calls) == 1 and llm.image_calls[0][0].mime_type == "image/png"
    assert llm.image_calls[0][0].data.startswith(b"\x89PNG")
    prompt = llm.calls[0]
    assert "추측하지 않는다" in prompt and "환불해도" in prompt  # 옮겨 적기만, 판단은 하지 않는다


def test_photos_that_cannot_be_read_raise_before_any_llm_call() -> None:
    llm = fake_llm(MATCHING)
    extract = receipt_vision.make_extractor(lambda: llm)
    with pytest.raises(ValueError):
        asyncio.run(extract([receipts.EvidenceImage("img_ref_only", "image/png", None)]))
    with pytest.raises(ValueError):
        asyncio.run(extract([receipts.EvidenceImage("img_bad", "image/png", "%%%not-base64%%%")]))
    with pytest.raises(LLMInputError):
        asyncio.run(extract([receipts.EvidenceImage("img_pdf", "application/pdf", PNG_B64)]))
    assert llm.calls == []  # 잘못된 입력은 LLM에 보내지 않는다


def test_amounts_and_items_are_not_written_to_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    extract = receipt_vision.make_extractor(lambda: fake_llm(MATCHING))
    with caplog.at_level(logging.INFO, logger="support_agent.receipt_vision"):
        asyncio.run(extract([receipts.EvidenceImage("img_1", "image/png", PNG_B64)]))
    text = " ".join(record.getMessage() for record in caplog.records)
    assert "33000" not in text and "고양이의 하루" not in text and "ORD-2026" not in text


# ================================================================ skill까지 이어 붙이기
def test_a_photo_only_receipt_is_read_and_checked_against_the_order(_fresh: InquiryStore) -> None:
    receipts.set_vision_extractor(receipt_vision.make_extractor(lambda: fake_llm(MATCHING)))
    result = call({"inquiry_id": inquiry(_fresh)}, {"images": [photo()]})
    out = result["output"]
    assert out["images_status"] == "read" and result["outcome"] == "receipt_consistent"
    assert out["facts"]["total"] == 33000 and out["facts"]["order_no"] == "ORD-2026-0101"


def test_a_photo_that_does_not_match_is_reported_not_judged(_fresh: InquiryStore) -> None:
    wrong = MATCHING.model_copy(update={"total": 50000})
    receipts.set_vision_extractor(receipt_vision.make_extractor(lambda: fake_llm(wrong)))
    result = call({"inquiry_id": inquiry(_fresh)}, {"images": [photo()]})
    assert result["outcome"] == "receipt_mismatch"
    assert "RECEIPT_AMOUNT_MISMATCH" in result["output"]["check"]["mismatches"]
    assert "refund" not in str(result["output"]).lower()  # 환불 판정은 하지 않는다


def test_a_photo_the_model_cannot_read_is_insufficient_not_invented(_fresh: InquiryStore) -> None:
    blank = VisionReceipt(readable=False)
    receipts.set_vision_extractor(receipt_vision.make_extractor(lambda: fake_llm(blank)))
    result = call({"inquiry_id": inquiry(_fresh)}, {"images": [photo()]})
    assert result["outcome"] == "receipt_insufficient"
    assert result["output"]["facts"]["total"] is None


def test_a_broken_model_is_reported_and_the_text_is_still_checked(_fresh: InquiryStore) -> None:
    broken = MockLLMClient(responses={})  # 응답이 없으면 KeyError가 난다
    receipts.set_vision_extractor(receipt_vision.make_extractor(lambda: broken))
    result = call(
        {"inquiry_id": inquiry(_fresh)},
        {"receipt_text": "총 결제금액 33,000원", "images": [photo()]},
    )
    assert result["output"]["images_status"] == "failed"
    assert result["output"]["facts"]["total"] == 33000


# ================================================================ 연결
def test_nothing_is_connected_in_mock_mode() -> None:
    assert receipt_vision.connect() is False and receipts.get_vision_extractor() is None


def test_connected_only_when_a_model_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    assert receipt_vision.connect() is False  # 모델이 없으면 꽂지 않는다

    monkeypatch.setenv("SUPPORT_LLM_MODEL", "gemini-test")
    monkeypatch.setenv("SUPPORT_LLM_BASE_URL", "http://llm.invalid/v1")
    assert receipt_vision.connect() is True and receipts.get_vision_extractor() is not None

    monkeypatch.setenv("SUPPORT_VISION", "off")  # 끄는 스위치
    assert receipt_vision.connect() is False and receipts.get_vision_extractor() is None
