"""DejaFix로 나가는 고객 개인정보: 필드 암호화, 평문 개인정보 차단, 키가 없으면 보내지 않는다."""

import asyncio
import base64
import copy
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ax_contracts.schemas import AgentRequest, AgentResult, RequestMeta, TaskStatus
from support_agent import handlers, secure_transfer

KEY = os.urandom(32)
CTX = "ctx_inc_0001"
NAME = "홍길동"
MESSAGE = "책이 찢어져서 왔어요. 환불해 주세요"


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(secure_transfer.KEY_ENV, base64.b64encode(KEY).decode())
    monkeypatch.setenv(secure_transfer.KEY_ID_ENV, "k7")


def make_request(
    input: dict[str, Any] | None = None, untrusted: dict[str, Any] | None = None
) -> AgentRequest:
    return AgentRequest(
        meta=RequestMeta(
            context_id=CTX,
            token="mock:support",
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        ),
        skill="diagnose_incident",
        input=input if input is not None else {"order_id": "ord_0001", "customer_name": NAME},
        untrusted=untrusted if untrusted is not None else {"message": MESSAGE},
    )


# ---------------------------------------------------------------- 암호화 기본
def test_a_value_round_trips_and_the_plaintext_is_not_in_the_blob() -> None:
    blob = secure_transfer.encrypt_value(NAME, context_id=CTX, field="customer_name")
    assert blob["__enc__"] == 1 and blob["alg"] == "AES-256-GCM" and blob["kid"] == "k7"
    assert NAME not in str(blob) and secure_transfer.is_encrypted(blob)
    assert (
        secure_transfer.decrypt_value(blob, context_id=CTX, field="customer_name", key=KEY) == NAME
    )


def test_structured_values_round_trip_too() -> None:
    value = {"lines": [{"title": "고양이의 하루", "qty": 1}], "ok": True}
    blob = secure_transfer.encrypt_value(value, context_id=CTX, field="receipt_text")
    assert (
        secure_transfer.decrypt_value(blob, context_id=CTX, field="receipt_text", key=KEY) == value
    )


def test_every_encryption_uses_a_new_nonce() -> None:
    a = secure_transfer.encrypt_value(NAME, context_id=CTX, field="customer_name")
    b = secure_transfer.encrypt_value(NAME, context_id=CTX, field="customer_name")
    assert a["nonce"] != b["nonce"] and a["ct"] != b["ct"]


def test_a_blob_cannot_be_moved_to_another_request_or_field_or_changed() -> None:
    blob = secure_transfer.encrypt_value(NAME, context_id=CTX, field="customer_name")
    decrypt = secure_transfer.decrypt_value
    with pytest.raises(secure_transfer.DecryptError):
        decrypt(blob, context_id="ctx_inc_9999", field="customer_name", key=KEY)  # 다른 사건
    with pytest.raises(secure_transfer.DecryptError):
        decrypt(blob, context_id=CTX, field="email", key=KEY)  # 다른 필드
    with pytest.raises(secure_transfer.DecryptError):
        decrypt(blob, context_id=CTX, field="customer_name", key=os.urandom(32))  # 다른 키
    tampered = copy.deepcopy(blob)
    raw = bytearray(base64.b64decode(tampered["ct"]))
    raw[0] ^= 1
    tampered["ct"] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(secure_transfer.DecryptError):
        decrypt(tampered, context_id=CTX, field="customer_name", key=KEY)  # 바뀐 암호문
    with pytest.raises(secure_transfer.DecryptError):
        decrypt({**blob, "alg": "ROT13"}, context_id=CTX, field="customer_name", key=KEY)


@pytest.mark.parametrize("raw", ["", "not base64!!", base64.b64encode(b"short").decode()])
def test_a_missing_or_malformed_key_is_refused(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(secure_transfer.KEY_ENV, raw)
    with pytest.raises(secure_transfer.KeyMissing):
        secure_transfer.load_key()
    with pytest.raises(secure_transfer.KeyMissing):
        secure_transfer.encrypt_value(NAME, context_id=CTX, field="customer_name")


# ---------------------------------------------------------------- 요청 보호
def decrypt_field(blob: dict[str, Any], field: str) -> Any:
    return secure_transfer.decrypt_value(blob, context_id=CTX, field=field, key=KEY)


def test_customer_fields_and_untrusted_are_encrypted_and_facts_stay_readable() -> None:
    protected = secure_transfer.protect_request(
        make_request(
            {
                "order_id": "ord_0001",
                "buyer_id": "buy_0001",
                "refund_amount": 33000,
                "customer_name": NAME,
            },
            {"message": MESSAGE, "note": "상담원 메모"},
        )
    )
    assert protected.input["order_id"] == "ord_0001"  # 사실은 그대로
    assert protected.input["buyer_id"] == "buy_0001" and protected.input["refund_amount"] == 33000
    assert secure_transfer.is_encrypted(protected.input["customer_name"])
    assert decrypt_field(protected.input["customer_name"], "customer_name") == NAME
    assert all(secure_transfer.is_encrypted(v) for v in protected.untrusted.values())
    assert decrypt_field(protected.untrusted["message"], "message") == MESSAGE
    assert decrypt_field(protected.untrusted["note"], "note") == "상담원 메모"
    text = str(protected.model_dump())
    for leaked in (NAME, MESSAGE, "상담원 메모"):
        assert leaked not in text


def test_nested_customer_fields_are_encrypted() -> None:
    protected = secure_transfer.protect(
        {"items": [{"title": "책", "address": "서울시 어딘가 1-2"}], "meta": {"email": "a@b.co"}},
        context_id=CTX,
    )
    assert protected["items"][0]["title"] == "책"
    assert secure_transfer.is_encrypted(protected["items"][0]["address"])
    assert secure_transfer.is_encrypted(protected["meta"]["email"])


def test_already_encrypted_values_are_not_encrypted_again() -> None:
    blob = secure_transfer.encrypt_value(NAME, context_id=CTX, field="customer_name")
    protected = secure_transfer.protect({"customer_name": blob}, context_id=CTX)
    assert protected["customer_name"] is blob


@pytest.mark.parametrize(
    "plain",
    [
        "연락처는 010-1234-5678 입니다",
        "mail me@example.com",
        "카드 4111 1111 1111 1111",
        "주민번호 900101-1234567",
        "계좌번호: 110-123-456789",
    ],
)
def test_plain_personal_data_outside_encrypted_fields_is_refused(plain: str) -> None:
    with pytest.raises(secure_transfer.PlainPiiError) as caught:
        secure_transfer.protect({"order_id": "ord_0001", "reason": plain}, context_id=CTX)
    assert plain not in str(caught.value)  # 오류에도 값을 싣지 않는다


def test_personal_data_inside_an_encrypted_field_is_fine() -> None:
    protected = secure_transfer.protect(
        {"message": "연락처는 010-1234-5678 입니다"}, context_id=CTX
    )
    assert secure_transfer.is_encrypted(protected["message"])


def test_requests_without_customer_data_need_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(secure_transfer.KEY_ENV, raising=False)
    facts = secure_transfer.protect(
        {"order_id": "ord_0001", "refund_amount": 33000}, context_id=CTX
    )
    assert facts == {"order_id": "ord_0001", "refund_amount": 33000}


# ---------------------------------------------------------------- 전송
class Raw:
    def __init__(self) -> None:
        self.sent: list[tuple[str, AgentRequest]] = []

    async def __call__(self, target: str, request: AgentRequest) -> AgentResult:
        self.sent.append((target, request))
        return AgentResult(meta=request.meta, status=TaskStatus.COMPLETED, output={"ok": True})


@pytest.fixture
def raw(monkeypatch: pytest.MonkeyPatch) -> Raw:
    fake = Raw()
    monkeypatch.setattr(secure_transfer, "_raw_send", fake)
    return fake


@pytest.mark.parametrize("target", ["infra", "dejafix", "infra-dejafix"])
def test_requests_to_dejafix_are_encrypted_under_any_of_its_names(raw: Raw, target: str) -> None:
    result = asyncio.run(secure_transfer.send(target, make_request()))
    assert result.status == TaskStatus.COMPLETED
    (sent_target, sent) = raw.sent[0]
    assert sent_target == target
    assert NAME not in str(sent.model_dump()) and MESSAGE not in str(sent.model_dump())
    assert decrypt_field(sent.input["customer_name"], "customer_name") == NAME
    assert decrypt_field(sent.untrusted["message"], "message") == MESSAGE
    assert sent.input["order_id"] == "ord_0001"


def test_other_agents_are_not_touched(raw: Raw) -> None:
    request = make_request({"inquiry_id": 1, "buyer_id": "buy_0001"}, {})
    asyncio.run(secure_transfer.send("marketing", request))
    assert raw.sent[0][1] is request  # 그대로 전달


def test_without_a_key_customer_data_is_not_sent_at_all(
    raw: Raw, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(secure_transfer.KEY_ENV, raising=False)
    result = asyncio.run(secure_transfer.send("infra", make_request()))
    assert result.status == TaskStatus.FAILED and result.outcome == "secure_transfer_key_missing"
    assert raw.sent == []  # 평문으로 대신 보내지 않는다


def test_plain_personal_data_in_a_fact_field_blocks_the_whole_request(raw: Raw) -> None:
    request = make_request({"order_id": "ord_0001", "reason": "010-1234-5678로 연락"}, {})
    result = asyncio.run(secure_transfer.send("infra", request))
    assert result.status == TaskStatus.FAILED and result.outcome == "secure_transfer_plain_pii"
    assert raw.sent == []
    assert "010-1234-5678" not in str(result.model_dump())


def test_the_only_outbound_path_of_the_agent_is_the_secure_one() -> None:
    assert handlers.SEND is secure_transfer.send
