"""DejaFix로 나가는 고객 개인정보를 필드 단위로 암호화한다. 개인정보는 캣북스 쪽에만 둔다.

규칙
- DejaFix(에이전트 이름 `infra`)로 보내는 요청에서 **고객 개인정보 필드**(`CUSTOMER_FIELDS`)와
  `untrusted`(고객이 쓴 글) 전체는 AES-256-GCM으로 암호화해서 보낸다. 나머지 사실(주문,
  구매자 가명 ID, 금액, 건수 등)은 그대로 둔다.
- 암호화하지 않은 개인정보(전화번호, 이메일, 카드번호, 주민번호, 계좌)가 섞여 있으면
  **보내지 않는다**(fail-closed). 키가 없을 때 고객 필드가 있으면 역시 보내지 않는다.
  평문으로 대신 보내지 않는다.
- 암호문은 사건 번호(`context_id`)와 필드 이름에 묶인다(AAD). 다른 요청에 옮겨 붙이거나 바꾸면
  복호화에 실패한다.
- 키는 환경변수 `SUPPORT_DEJAFIX_KEY`(base64, 32바이트)와 `SUPPORT_DEJAFIX_KEY_ID`다.
  코드·레포에 두지 않고, DejaFix 담당과 따로 안전하게 나눈다. DejaFix는 복호화한 값을
  저장하지 않는다.

암호문 모양(필드 하나):
`{"__enc__": 1, "alg": "AES-256-GCM", "kid": "<키 ID>", "nonce": "<b64>", "ct": "<b64>"}`
복호화는 `decrypt_value`가 기준 구현이다 (DejaFix 쪽이 같은 규칙으로 구현한다).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ax_agent_template.client import call_agent
from ax_agent_template.server import error_result
from ax_contracts.schemas import AgentRequest, AgentResult, TaskStatus
from support_agent import guard

KEY_ENV = "SUPPORT_DEJAFIX_KEY"
KEY_ID_ENV = "SUPPORT_DEJAFIX_KEY_ID"
ALG = "AES-256-GCM"
FORMAT_VERSION = 1
NONCE_BYTES = 12

# 팀 기준 이름(`dejafix`, `infra-dejafix`)과 에이전트 이름(`infra`)을 모두 DejaFix로 본다
DEJAFIX_TARGETS = frozenset({"infra", "dejafix", "infra-dejafix"})

# 이 이름의 필드는 값이 무엇이든 암호화한다 (이름·연락처·주소·고객이 쓴 글·영수증)
CUSTOMER_FIELDS = frozenset(
    {
        "customer_name",
        "name",
        "email",
        "phone",
        "address",
        "message",
        "text",
        "subject",
        "description",
        "receipt_text",
        "images",
    }
)


class SecureTransferError(Exception):
    """개인정보를 안전하게 보낼 수 없다. 메시지에 값은 담지 않는다."""

    code = "secure_transfer_blocked"


class KeyMissing(SecureTransferError):
    code = "secure_transfer_key_missing"


class PlainPiiError(SecureTransferError):
    code = "secure_transfer_plain_pii"


class DecryptError(SecureTransferError):
    code = "secure_transfer_decrypt_failed"


def load_key() -> tuple[bytes, str]:
    """환경변수에서 `(키, 키 ID)`를 읽는다. 없거나 길이가 맞지 않으면 `KeyMissing`이다."""
    raw = os.getenv(KEY_ENV, "").strip()
    if not raw:
        raise KeyMissing(f"{KEY_ENV}가 설정되지 않았다")
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KeyMissing(f"{KEY_ENV}가 base64가 아니다") from exc
    if len(key) != 32:
        raise KeyMissing(f"{KEY_ENV}는 32바이트여야 한다")
    return key, os.getenv(KEY_ID_ENV, "k1").strip() or "k1"


def _aad(context_id: str, field: str) -> bytes:
    return f"{FORMAT_VERSION}|{context_id}|{field}".encode()


def is_encrypted(value: Any) -> bool:
    return isinstance(value, dict) and value.get("__enc__") == FORMAT_VERSION and "ct" in value


def encrypt_value(
    value: Any, *, context_id: str, field: str, key: bytes | None = None, key_id: str | None = None
) -> dict[str, Any]:
    if key is None:
        key, key_id = load_key()
    plaintext = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _aad(context_id, field))
    return {
        "__enc__": FORMAT_VERSION,
        "alg": ALG,
        "kid": key_id or "k1",
        "nonce": base64.b64encode(nonce).decode(),
        "ct": base64.b64encode(ciphertext).decode(),
    }


def decrypt_value(blob: dict[str, Any], *, context_id: str, field: str, key: bytes) -> Any:
    """기준 복호화. 사건 번호·필드 이름·키가 다르거나 암호문이 바뀌었으면 `DecryptError`다."""
    if not is_encrypted(blob) or blob.get("alg") != ALG:
        raise DecryptError("지원하지 않는 암호문 형식이다")
    try:
        nonce = base64.b64decode(blob["nonce"], validate=True)
        ciphertext = base64.b64decode(blob["ct"], validate=True)
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _aad(context_id, field))
    except (KeyError, binascii.Error, ValueError, InvalidTag) as exc:
        raise DecryptError("복호화에 실패했다") from exc
    return json.loads(plaintext)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def assert_no_plain_pii(value: Any) -> None:
    """암호화되지 않은 곳에 개인정보 패턴이 있으면 `PlainPiiError`다 (값은 오류에 싣지 않는다)."""
    if is_encrypted(value):
        return
    if isinstance(value, dict):
        for item in value.values():
            assert_no_plain_pii(item)
    elif isinstance(value, list):
        for item in value:
            assert_no_plain_pii(item)
    elif isinstance(value, str) and guard.find_pii(value):
        raise PlainPiiError("암호화되지 않은 개인정보가 있어 보내지 않는다")


def protect(data: dict[str, Any], *, context_id: str, whole: bool = False) -> dict[str, Any]:
    """고객 필드를 암호화하고 남은 평문에 개인정보가 없는지 검사한다.

    `whole=True`면 모든 최상위 값을 암호화한다 (`untrusted`처럼 전부 고객 입력인 곳).
    """
    key: tuple[bytes, str] | None = None
    out: dict[str, Any] = {}

    def encrypt(name: str, value: Any) -> dict[str, Any]:
        nonlocal key
        if key is None:
            key = load_key()  # 고객 필드가 있을 때만 키가 필요하다
        return encrypt_value(value, context_id=context_id, field=name, key=key[0], key_id=key[1])

    def walk(name: str, value: Any, top: bool) -> Any:
        if is_encrypted(value):
            return value
        if (top and whole) or name in CUSTOMER_FIELDS:
            return encrypt(name, value)
        if isinstance(value, dict):
            return {k: walk(k, v, False) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(name, v, False) for v in value]
        return value

    for name, value in data.items():
        out[name] = walk(name, value, True)
    assert_no_plain_pii(out)
    return out


def protect_request(request: AgentRequest) -> AgentRequest:
    """DejaFix로 보낼 요청을 만든다. 입력은 고객 필드만, `untrusted`는 전부 암호화한다."""
    context_id = request.meta.context_id
    return request.model_copy(
        update={
            "input": protect(request.input, context_id=context_id),
            "untrusted": protect(request.untrusted, context_id=context_id, whole=True),
        }
    )


SendFn = Callable[[str, AgentRequest], Awaitable[AgentResult]]
_raw_send: SendFn = call_agent  # 테스트에서 바꿀 수 있다


async def send(target: str, request: AgentRequest) -> AgentResult:
    """에이전트에 요청을 보낸다. DejaFix면 개인정보를 암호화하고, 그럴 수 없으면 보내지 않는다."""
    if target not in DEJAFIX_TARGETS:
        return await _raw_send(target, request)
    try:
        protected = protect_request(request)
    except SecureTransferError as exc:
        return error_result(request.meta, TaskStatus.FAILED, exc.code, str(exc))
    return await _raw_send(target, protected)
