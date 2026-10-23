"""memory skill 핸들러 (card_write, card_search, card_get, card_link).

- 상태를 바꾸는 일(카드 저장·연결)은 `execute()`가 `executed`를 돌려준 뒤에만 한다.
- 카드 문장은 쓰는 부서의 책임이다. memory는 title·body를 바꾸지 않는다.
- 외부 입력(`request.untrusted`)은 카드 본문에 섞지 않고 `extra["untrusted"]`에만 보관한다.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from ax_agent_template.execute import execute
from ax_agent_template.logging import log_event
from ax_contracts.schemas import (
    AgentRequest,
    AgentResult,
    Evidence,
    ExecuteDecision,
    HoldReason,
    InputReason,
    TaskStatus,
)
from ax_contracts.schemas.record import (
    CardGetInput,
    CardGetOutput,
    CardHit,
    CardLinkInput,
    CardLinkOutput,
    CardSearchInput,
    CardSearchOutput,
    CardWriteInput,
    CardWriteOutput,
)
from ax_memory.embed import Embedder, get_embedder
from ax_memory.search import InMemoryVectorIndex, VectorIndex, index_card, search_cards
from ax_memory.store import CardStore, InMemoryCardStore


class State:
    """저장소·벡터 인덱스·임베더. `MEMORY_PG_DSN`이 있으면 PostgreSQL을 쓴다."""

    def __init__(self) -> None:
        self.store: CardStore
        self.index: VectorIndex
        dsn = os.getenv("MEMORY_PG_DSN")
        if dsn:
            from ax_memory.pg_store import PgCardStore, PgVectorIndex, connect, ensure_schema

            conn = connect(dsn)
            ensure_schema(conn)
            self.store = PgCardStore(conn)
            self.index = PgVectorIndex(conn)
        else:
            self.store = InMemoryCardStore()
            self.index = InMemoryVectorIndex()
        self.embedder: Embedder = get_embedder()


_STATE: State | None = None


def get_state() -> State:
    global _STATE
    if _STATE is None:
        _STATE = State()
    return _STATE


def reset_state(state: State | None = None) -> None:
    """테스트 사이에 저장소를 비운다. `state`를 주면 그것으로 바꾼다."""
    global _STATE
    _STATE = state


def _now() -> datetime:
    return datetime.now(UTC)


def _evidence(queries: list[str], summary: str, ref_ids: list[str] | None = None) -> Evidence:
    return Evidence(
        queries=queries, data_summary=summary, collected_at=_now(), ref_ids=ref_ids or []
    )


def _result(request: AgentRequest, status: TaskStatus, **kwargs: Any) -> AgentResult:
    return AgentResult(meta=request.meta, status=status, **kwargs)


def _invalid(request: AgentRequest, detail: list[str]) -> AgentResult:
    return _result(request, TaskStatus.FAILED, outcome="invalid_input", output={"errors": detail})


def _parse[T: BaseModel](request: AgentRequest, model: type[T]) -> T | AgentResult:
    try:
        return model.model_validate(request.input)
    except ValidationError as error:
        return _invalid(
            request, [f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in error.errors()]
        )


def _not_found(request: AgentRequest, card_id: str) -> AgentResult:
    return _result(
        request,
        TaskStatus.FAILED,  # rejected의 outcome은 ErrorCode만 허용하므로 failed로 돌려준다
        outcome="card_not_found",
        output={"card_id": card_id},
        evidence=_evidence([request.skill], "카드를 찾을 수 없음(확인할 수 없음)", [card_id]),
    )


def _from_decision(
    request: AgentRequest, decision: ExecuteDecision, evidence: Evidence
) -> AgentResult | None:
    """`executed`가 아니면 결과 Task를 만들어 돌려준다. `executed`면 None이다."""
    if decision.status == "executed":
        return None
    detail = {"execution_id": decision.execution_id, "grade": decision.grade}
    if decision.status == "verifying":
        return _result(request, TaskStatus.WORKING, outcome="verifying", output=detail)
    if decision.status == "held":
        detail |= {"review_id": decision.review_id, "hold_reason": decision.hold_reason}
        return _result(
            request,
            TaskStatus.INPUT_REQUIRED,
            reason_code=InputReason.HUMAN_INPUT,
            outcome="held",
            output=detail,
            evidence=evidence,
        )
    if decision.hold_reason == HoldReason.VERIFY_REJECTED:
        return _result(
            request,
            TaskStatus.COMPLETED,
            outcome="rejected_by_review",
            output=detail,
            evidence=evidence,
        )
    if decision.hold_reason == HoldReason.VERIFY_TIMEOUT:
        return _result(
            request, TaskStatus.CANCELED, outcome="TIMEOUT", output=detail, evidence=evidence
        )
    outcome = "execute_failed" if decision.status == "failed" else "execute_rejected"
    return _result(request, TaskStatus.FAILED, outcome=outcome, output=detail, evidence=evidence)


def _missing_key(request: AgentRequest) -> AgentResult | None:
    if request.meta.idempotency_key:
        return None
    return _invalid(request, ["meta.idempotency_key: required"])


async def card_write(request: AgentRequest) -> AgentResult:
    if (blocked := _missing_key(request)) is not None:
        return blocked
    data = _parse(request, CardWriteInput)
    if isinstance(data, AgentResult):
        return data
    draft = data.card
    if draft.tenant_id != request.meta.tenant_id:
        return _invalid(request, ["card.tenant_id: mismatch"])

    # 신뢰 경계: 외부 입력은 본문이 아니라 extra["untrusted"]에만 둔다.
    extra = {k: v for k, v in draft.extra.items() if k != "untrusted"}
    if request.untrusted:
        extra["untrusted"] = request.untrusted
    draft = draft.model_copy(update={"extra": extra})

    evidence = _evidence(
        ["card_write"], f"{draft.kind} 카드 저장 요청: {draft.title}", [draft.context_id]
    )
    decision = await execute(
        "memory.card_write",
        {"kind": draft.kind, "context_id": draft.context_id, "title": draft.title},
        request.meta,
        evidence,
    )
    blocked = _from_decision(request, decision, evidence)
    if blocked:
        return blocked

    state = get_state()
    written = state.store.write(draft)
    if written.created:
        await index_card(state.index, state.embedder, written.card)
    out = CardWriteOutput(card_id=written.card.card_id, created=written.created)
    return _result(
        request,
        TaskStatus.COMPLETED,
        output=out.model_dump(mode="json"),
        evidence=_evidence(
            ["card_write"],
            "카드 저장" if written.created else "같은 멱등 키의 기존 카드를 돌려줌",
            [out.card_id, decision.execution_id],
        ),
    )


async def card_get(request: AgentRequest) -> AgentResult:
    data = _parse(request, CardGetInput)
    if isinstance(data, AgentResult):
        return data
    card = get_state().store.get(data.card_id)
    if card is None or card.tenant_id != request.meta.tenant_id:
        return _not_found(request, data.card_id)
    return _result(
        request,
        TaskStatus.COMPLETED,
        output=CardGetOutput(card=card).model_dump(mode="json"),
        evidence=_evidence(["card_get"], "카드 조회", [card.card_id]),
    )


async def card_search(request: AgentRequest) -> AgentResult:
    data = _parse(request, CardSearchInput)
    if isinstance(data, AgentResult):
        return data
    state = get_state()
    hits = await search_cards(
        state.store,
        state.index,
        state.embedder,
        data.query,
        kind=list(data.kind) or None,
        status=data.status,
        since=data.since,
        top_k=data.top_k,
        tenant_id=request.meta.tenant_id,
    )
    out = CardSearchOutput(
        hits=[CardHit(card=h.card, score=h.score, low_confidence=h.low_confidence) for h in hits]
    )
    ref_ids = [h.card.card_id for h in hits]
    # SCN-L의 "기억을 참조했다"는 근거 로그
    log_event(
        request.meta.context_id,
        request.meta.tenant_id,
        "memory_referenced",
        {"requester": request.meta.requester, "card_ids": ref_ids, "hit_count": len(ref_ids)},
    )
    summary = (
        f"카드 {len(ref_ids)}건 검색" if ref_ids else "관련 카드를 찾을 수 없음(확인할 수 없음)"
    )
    return _result(
        request,
        TaskStatus.COMPLETED,
        output=out.model_dump(mode="json"),
        evidence=_evidence(["card_search"], summary, ref_ids),
    )


async def card_link(request: AgentRequest) -> AgentResult:
    if (blocked := _missing_key(request)) is not None:
        return blocked
    data = _parse(request, CardLinkInput)
    if isinstance(data, AgentResult):
        return data
    if data.old_card_id == data.new_card_id:
        return _invalid(request, ["new_card_id: same_as_old"])

    state = get_state()
    tenant = request.meta.tenant_id
    for card_id in (data.old_card_id, data.new_card_id):
        card = state.store.get(card_id)
        if card is None or card.tenant_id != tenant:
            return _not_found(request, card_id)

    evidence = _evidence(
        ["card_link"],
        f"{data.old_card_id} -> {data.new_card_id} ({data.relation})",
        [data.old_card_id, data.new_card_id],
    )
    decision = await execute(
        "memory.card_link",
        data.model_dump(mode="json"),
        request.meta,
        evidence,
    )
    blocked = _from_decision(request, decision, evidence)
    if blocked:
        return blocked

    superseded_by = data.new_card_id if data.relation == "superseded" else None
    old = state.store.update_status(data.old_card_id, data.relation, superseded_by)
    return _result(
        request,
        TaskStatus.COMPLETED,
        output=CardLinkOutput(old_card=old).model_dump(mode="json"),
        evidence=_evidence(
            ["card_link"], "카드 연결", [data.old_card_id, data.new_card_id, decision.execution_id]
        ),
    )


HANDLERS = {
    "card_write": card_write,
    "card_search": card_search,
    "card_get": card_get,
    "card_link": card_link,
}
