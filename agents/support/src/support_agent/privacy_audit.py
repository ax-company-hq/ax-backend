"""개인정보 감사 기록을 만든다 (설계 #442 1단계). 프레임워크에 의존하지 않는 순수 코드다 (LLM 없음).

- 기록은 추가만 한다. 고치거나 지우는 코드는 없다.
- 원문, 마스킹한 글, 값의 해시는 어디에도 넣지 않는다. 종류별 건수와 ID·코드·시각만 쓴다.
- 기존 쓰기 skill이 처리 중에 부르는 부수 효과라 새 쓰기 skill이 필요 없다. 멱등 키가 같은
  재요청은 `_write`가 처음 결과를 돌려주므로 기록이 두 번 생기지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from ax_contracts.schemas import AgentRequest
from support_agent import guard
from support_agent.audit_schemas import (
    PII_KINDS,
    MaskingAction,
    MaskingAuditRecord,
    PrivacyAudit,
    ProcessingActivity,
    ReplyOutcome,
    ReplyProvenance,
    SourceRef,
    Stage,
)
from support_agent.schemas import Analysis, Conversation, Validation
from support_agent.store import InquiryStore, now_iso

MODEL_ENV = "SUPPORT_LLM_MODEL"

NOTES = [
    "탐지한 개인정보의 증빙이다. 이름·주소·일반전화는 규칙이 가리지 못해 여기에 나오지 않으므로 "
    "문의에 개인정보가 없었다는 증빙이 아니다",
    "원문을 지운 뒤에는 건수 기록만 남는다. 보유기간 삭제는 아직 구현되지 않았다",
    "법적 판단이 아니다. 규제 관련 문구는 법무 확인이 필요하다",
]

PROCESSING_ACTIVITY = ProcessingActivity(
    categories=[
        "문의 원문",
        "가명 구매자 ID",
        *(f"마스킹 대상: {k}" for k in PII_KINDS),
        "영수증 글",
    ],
    purposes=["문의 응대", "클레임 판정", "보상 요청"],
    recipients=[
        "LLM 공급자(마스킹한 글만)",
        "DejaFix(필드 암호화, 평문 개인정보는 차단)",
    ],
    retention="문의 원문 90일, 처리 중 5년은 시드(제안)이며 삭제 코드는 아직 없다",
    cross_border="LLM 공급자 위치는 배포 설정에 따른다. 법무 확인 필요",
)


def _rules_version() -> str:
    """마스킹 규칙(정규식 목록)이 바뀌면 달라지는 짧은 버전 문자열."""
    patterns = [p.pattern for p in (guard.PHONE, guard.EMAIL, guard.RRN, guard.CARD, guard.ACCOUNT)]
    return hashlib.sha256(json.dumps(patterns).encode()).hexdigest()[:12]


RULES_VERSION = _rules_version()


def count_pii(text: str) -> dict[str, int]:
    """종류별 개인정보 건수. 값은 돌려주지 않는다."""
    return {
        "phone": len(guard.PHONE.findall(text)),
        "email": len(guard.EMAIL.findall(text)),
        "rrn": len(guard.RRN.findall(text)),
        "card": len(guard._card_spans(text)),
        "account": len(guard.ACCOUNT.findall(text)),
    }


def _merge(counts: list[dict[str, int]]) -> dict[str, int]:
    return {k: sum(c[k] for c in counts) for k in PII_KINDS}


def _append(store: InquiryStore, record: dict[str, Any]) -> None:
    store.privacy_audit.append({"audit_id": store.next_audit_id(), **record})


def _masking(
    store: InquiryStore,
    *,
    tenant_id: str,
    context_id: str,
    inquiry_id: int | None,
    message_id: int | None,
    seq: int | None,
    stage: Stage,
    texts: list[str],
    action: MaskingAction | None = None,
    reason_code: str | None = None,
) -> None:
    detected = _merge([count_pii(t) for t in texts])
    if action is None:
        action = "masked" if sum(detected.values()) else "none"
    _append(
        store,
        MaskingAuditRecord(
            audit_id=0,
            tenant_id=tenant_id,
            context_id=context_id,
            inquiry_id=inquiry_id,
            message_id=message_id,
            seq=seq,
            stage=stage,
            detected=detected,
            action=action,
            reason_code=reason_code,
            rules_version=RULES_VERSION,
            occurred_at=now_iso(),
        ).model_dump(mode="json", exclude={"audit_id"}),
    )


def _model_name() -> str | None:
    return os.getenv(MODEL_ENV) or None


def record_message(store: InquiryStore, conversation: Conversation, fallback: bool) -> None:
    """고객 메시지를 분석·이벤트로 넘긴 직후 부른다. 단계별 마스킹 기록과 초안 출처를 남긴다."""
    seq, message = next(
        (
            (i, m)
            for i, m in reversed(list(enumerate(conversation.messages, 1)))
            if m.role == "customer"
        ),
        (None, None),
    )
    if message is None:
        return
    common = {
        "tenant_id": conversation.tenant_id,
        "context_id": conversation.context_id,
        "inquiry_id": conversation.id,
        "message_id": message.id,
        "seq": seq,
    }
    _masking(store, **common, stage="ingest", texts=[message.text])
    _masking(store, **common, stage="llm_input", texts=[conversation.subject, message.text])
    _masking(store, **common, stage="event_untrusted", texts=[conversation.subject, message.text])
    analysis = message.analysis
    for draft in (analysis.drafts or []) if analysis else []:
        _provenance(
            store,
            conversation,
            message.id,
            analysis,
            style=draft.style,
            outcome="generated",
            actor="agent",
            validation=draft.validation,
            fallback=fallback,
        )


def _provenance(
    store: InquiryStore,
    conversation: Conversation,
    message_id: int | None,
    analysis: Analysis | None,
    *,
    style: str,
    outcome: ReplyOutcome,
    actor: str,
    validation: Validation | None,
    fallback: bool,
) -> None:
    _append(
        store,
        ReplyProvenance(
            audit_id=0,
            tenant_id=conversation.tenant_id,
            context_id=conversation.context_id,
            inquiry_id=conversation.id,
            message_id=message_id,
            draft_style=style,
            source_ids=[SourceRef(id=s.id, score=s.score) for s in (analysis.sources or [])]
            if analysis
            else [],
            path="template_fallback" if fallback else "llm",
            model_name=_model_name(),
            guard_codes=[f.code for f in validation.findings] if validation else [],
            outcome=outcome,
            actor=actor,  # type: ignore[arg-type]
            occurred_at=now_iso(),
        ).model_dump(mode="json", exclude={"audit_id"}),
    )


def record_decision(
    store: InquiryStore,
    conversation: Conversation,
    style: str,
    outcome: ReplyOutcome,
    validation: Validation | None = None,
) -> None:
    """초안 결정 기록: 그대로(accepted)·수정(edited)·거절(rejected)·가드 차단(blocked)."""
    last = next(
        (m for m in reversed(conversation.messages) if m.role == "customer" and m.analysis), None
    )
    analysis = last.analysis if last else None
    if validation is None and analysis:
        draft = next((d for d in analysis.drafts or [] if d.style == style), None)
        validation = draft.validation if draft else None
    # 결정 시점에는 어느 경로로 만든 초안인지 다시 알 수 없어 만든 기록의 경로를 잇는다
    made = next(
        (
            r
            for r in reversed(store.privacy_audit)
            if r.get("record_type") == "provenance"
            and r.get("inquiry_id") == conversation.id
            and r.get("draft_style") == style
            and r.get("outcome") == "generated"
        ),
        None,
    )
    _provenance(
        store,
        conversation,
        last.id if last else None,
        analysis,
        style=style,
        outcome=outcome,
        actor="human",
        validation=validation,
        fallback=bool(made and made.get("path") == "template_fallback"),
    )


def record_transfer_block(
    store: InquiryStore, request: AgentRequest, strings: list[str], reason_code: str
) -> None:
    """DejaFix 전송을 막은 사실. 이유 코드와 요청 안 개인정보의 종류별 건수만 남긴다."""
    raw_inquiry = request.input.get("inquiry_id")
    _masking(
        store,
        tenant_id=request.meta.tenant_id,
        context_id=request.meta.context_id,
        inquiry_id=raw_inquiry if isinstance(raw_inquiry, int) else None,
        message_id=None,
        seq=None,
        stage="transfer",
        texts=strings,
        action="blocked",
        reason_code=reason_code,
    )


def build_audit(
    store: InquiryStore, inquiry_id: int, kinds: list[str] | None = None
) -> PrivacyAudit:
    wanted = set(kinds or ("masking", "provenance"))
    rows = [r for r in store.privacy_audit if r.get("inquiry_id") == inquiry_id]
    return PrivacyAudit(
        inquiry_id=inquiry_id,
        masking=[
            MaskingAuditRecord.model_validate(r)
            for r in rows
            if r["record_type"] == "masking" and "masking" in wanted
        ],
        provenance=[
            ReplyProvenance.model_validate(r)
            for r in rows
            if r["record_type"] == "provenance" and "provenance" in wanted
        ],
        rules_version=RULES_VERSION,
        processing_activity=PROCESSING_ACTIVITY,
        notes=NOTES,
    )
