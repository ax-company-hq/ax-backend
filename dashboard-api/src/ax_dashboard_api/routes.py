"""상담원 화면 읽기 API. 경로는 `/api/dashboard/<복수 명사>`.

dashboard-web 옛 경로와의 대응은 README "엔드포인트" 표를 본다. 지금은 목 모드(`DASHBOARD_MOCK=1`)만
응답한다. 3단계부터 support 에이전트 skill(A2A), shop-api·memory 읽기 API로 바꾼다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ax_contracts.schemas.hr import HandoverAnswerRequest
from ax_dashboard_api import claim_mapping, mock_actions, mock_data, mock_handover, support_client
from ax_dashboard_api.envelope import ApiError, is_mock, ok, require_mock
from ax_dashboard_api.schemas import (
    AgentMessageBody,
    Conversation,
    ConversationCreate,
    ConversationListItem,
    DraftStyleBody,
    InspectionBody,
    Kpi,
    NoteBody,
    QueueProcessBody,
    ReviewStatusBody,
    StatusBody,
    Summary,
    TextBody,
    Validation,
)

router = APIRouter(prefix="/api/dashboard")
# 목 데이터만 있는 경로 (클레임·보상 큐·후기·집계 등). 실제 연결은 이후 단계에서 바꾼다
mock_only = APIRouter(dependencies=[Depends(require_mock)])


@mock_only.get("/health")
def health() -> dict:
    return ok(mock_data.health())


@mock_only.get("/users")
def users() -> dict:
    return ok(mock_data.USERS, total=len(mock_data.USERS))


@mock_only.get("/metrics")
def metrics(days: int = Query(14, ge=1, le=90)) -> dict:
    return ok(mock_data.dashboard(days))


@mock_only.get("/kb-chunks")
def kb_chunks() -> dict:
    return ok(mock_data.KB_CHUNKS, total=len(mock_data.KB_CHUNKS))


@mock_only.get("/orders")
def orders(user_id: int) -> dict:
    items = mock_data.ORDERS_BY_USER.get(user_id, [])
    return ok(items, total=len(items))


@mock_only.get("/reviews")
def reviews() -> dict:
    return ok(mock_data.REVIEWS, total=len(mock_data.REVIEWS))


# ---------------------------------------- 클레임·보상 큐·KPI (목 모드 또는 support A2A)
# 목 모드는 메모리 데이터, 아니면 support skill을 부르고 `claim_mapping`으로 화면 모델에 맞춘다.
@router.get("/kpis")
async def kpis() -> dict:
    if is_mock():
        return ok(mock_data.KPI)
    out = await support_client.call("inquiry_overview")
    return ok(Kpi.model_validate(out["kpis"]))


@router.get("/claims")
async def claims(status: str | None = None) -> dict:
    if is_mock():
        items = mock_data.list_claims(status)
    else:
        out = await support_client.call("list_claims")
        items = claim_mapping.filter_by_status(
            [claim_mapping.to_claim(c) for c in out["items"]], status
        )
    return ok(items, total=len(items))


# 고정 경로가 `{claim_id}`보다 먼저 와야 한다
@router.get("/claims/stats")
async def claim_stats() -> dict:
    if is_mock():
        return ok(mock_data.claim_stats())
    return ok(claim_mapping.to_stats(await support_client.call("claim_stats")))


@router.get("/claims/{claim_id}")
async def claim(claim_id: int) -> dict:
    if is_mock():
        found = mock_data.get_claim(claim_id)
        if found is None:
            raise ApiError(404, "NOT_FOUND", f"클레임 {claim_id}를 찾을 수 없다")
        return ok(found)
    out = await support_client.call("get_claim", {"claim_id": claim_id})
    return ok(claim_mapping.to_detail(out))


@router.get("/queue-entries")
async def queue_entries() -> dict:
    if is_mock():
        view = mock_data.queue_view()
    else:
        view = claim_mapping.to_queue(await support_client.call("claim_queue"))
    return ok(view, total=len(view.queue))


@router.post("/claims/{claim_id}/approve")
async def approve_claim(claim_id: int, body: NoteBody) -> dict:
    return await _decide(claim_id, "approve", body.note)


@router.post("/claims/{claim_id}/reject")
async def reject_claim(claim_id: int, body: NoteBody) -> dict:
    return await _decide(claim_id, "reject", body.note)


async def _decide(claim_id: int, decision: str, note: str) -> dict:
    if is_mock():
        action = mock_actions.approve_claim if decision == "approve" else mock_actions.reject_claim
        return ok(action(claim_id, note))
    out = await support_client.call(
        "decide_claim",
        {"claim_id": claim_id, "decision": decision},
        {"note": note},
        target=f"claim:{claim_id}:{decision}",
    )
    return ok(claim_mapping.to_detail(out))


@router.post("/claims/{claim_id}/receive")
async def receive_claim(claim_id: int, body: InspectionBody) -> dict:
    if is_mock():
        return ok(mock_actions.receive_claim(claim_id, body.inspection, body.note))
    out = await support_client.call(
        "receive_return",
        {"claim_id": claim_id, "inspection": claim_mapping.inspection_in(body.inspection)},
        {"note": body.note or ""},
        target=f"claim:{claim_id}",
    )
    return ok(claim_mapping.to_detail(out))


@router.post("/claims/{claim_id}/inspection")
async def inspect_claim(claim_id: int, body: InspectionBody) -> dict:
    if is_mock():
        return ok(mock_actions.inspect_claim(claim_id, body.inspection, body.note))
    inspection = claim_mapping.inspection_in(body.inspection)
    out = await support_client.call(
        "inspect_return",
        {"claim_id": claim_id, "inspection": inspection},
        {"note": body.note or ""},
        target=f"claim:{claim_id}:{inspection}",
    )
    return ok(claim_mapping.to_detail(out))


@router.post("/queue-entries/process")
async def process_queue(body: QueueProcessBody | None = None) -> dict:
    body = body or QueueProcessBody()
    if is_mock():
        return ok(mock_actions.process_queue(body.limit, body.claim_ids))
    out = await support_client.call(
        "process_claim_queue", {"limit": body.limit, "claim_ids": body.claim_ids}
    )
    return ok(claim_mapping.to_run(out))


# ---------------------------------------------------------------- 문의 (목 모드 또는 support A2A)
# 목 모드(`DASHBOARD_MOCK=1`)는 메모리 데이터, 아니면 support 에이전트 skill을 부른다.
@router.get("/conversations")
async def conversations() -> dict:
    if is_mock():
        items = mock_data.list_conversations()
    else:
        out = await support_client.call("list_inquiries")
        items = [ConversationListItem.model_validate(i) for i in out.get("items", [])]
    return ok(items, total=len(items))


@router.get("/conversations/{conversation_id}")
async def conversation(conversation_id: int) -> dict:
    if is_mock():
        found = mock_data.get_conversation(conversation_id)
        if found is None:
            raise ApiError(404, "NOT_FOUND", f"문의 {conversation_id}를 찾을 수 없다")
        return ok(found)
    out = await support_client.call("get_inquiry", {"inquiry_id": conversation_id})
    return ok(Conversation.model_validate(out))


@router.post("/conversations", status_code=201)
async def create_conversation(body: ConversationCreate) -> dict:
    if is_mock():
        return ok(
            mock_actions.create_conversation(
                body.user_id, body.subject, body.message, body.order_id
            )
        )
    out = await support_client.call(
        "create_inquiry",
        {
            "user_id": body.user_id,
            "customer_name": body.customer_name or f"고객 {body.user_id}",
            "order_id": body.order_id,
        },
        {"subject": body.subject, "message": body.message},
        target=f"user:{body.user_id}",
    )
    return ok(Conversation.model_validate(out))


@router.post("/conversations/{conversation_id}/customer-messages", status_code=201)
async def customer_message(conversation_id: int, body: TextBody) -> dict:
    if is_mock():
        return ok(mock_actions.add_customer_message(conversation_id, body.text))
    out = await support_client.call(
        "add_customer_message",
        {"inquiry_id": conversation_id},
        {"message": body.text},
        target=f"inquiry:{conversation_id}",
    )
    return ok(Conversation.model_validate(out))


@router.post("/conversations/{conversation_id}/check")
async def check_reply(conversation_id: int, body: TextBody) -> dict:
    if is_mock():
        return ok(mock_actions.check_reply(conversation_id, body.text))
    out = await support_client.call(
        "check_reply", {"inquiry_id": conversation_id}, {"text": body.text}
    )
    return ok(Validation.model_validate(out))


@router.post("/conversations/{conversation_id}/agent-messages", status_code=201)
async def agent_message(conversation_id: int, body: AgentMessageBody) -> dict:
    if is_mock():
        return ok(mock_actions.add_agent_message(conversation_id, body.text))
    out = await support_client.call(
        "send_reply",
        {
            "inquiry_id": conversation_id,
            "draft_style": body.draft_style,
            "draft_text": body.draft_text,
        },
        {"text": body.text},
        target=f"inquiry:{conversation_id}",
    )
    return ok(Conversation.model_validate(out))


@router.post("/conversations/{conversation_id}/reject-draft")
async def reject_draft(conversation_id: int, body: DraftStyleBody) -> dict:
    if is_mock():
        mock_actions.reject_draft(conversation_id)
        return ok(None)
    await support_client.call(
        "reject_draft",
        {"inquiry_id": conversation_id, "draft_style": body.draft_style},
        target=f"inquiry:{conversation_id}:{body.draft_style}",
    )
    return ok(None)


@router.post("/conversations/{conversation_id}/summary")
async def summary(conversation_id: int) -> dict:
    if is_mock():
        return ok(mock_actions.summarize(conversation_id))
    out = await support_client.call("summarize_inquiry", {"inquiry_id": conversation_id})
    return ok(Summary.model_validate(out))


@router.post("/conversations/{conversation_id}/status")
async def conversation_status(conversation_id: int, body: StatusBody) -> dict:
    if is_mock():
        return ok(mock_actions.set_status(conversation_id, body.status))
    out = await support_client.call(
        "set_inquiry_status",
        {"inquiry_id": conversation_id, "status": body.status},
        target=f"inquiry:{conversation_id}:{body.status}",
    )
    return ok(ConversationListItem.model_validate(out))


# ---------------------------------------------------------------- 쓰기 (목 모드)
# 승인·지급 계열(claims approve·reject·receive·inspection, queue-entries/process)은
# 실제 연결 때 support `execute()` 관문으로 넘긴다. 지금은 목 데이터만 바꾼다.
@mock_only.post("/reviews/{review_id}/status")
def review_status(review_id: int, body: ReviewStatusBody) -> dict:
    return ok(mock_actions.set_review_status(review_id, body.status))


# ------------------------------------ 인사 탭: 인수인계 질문 (목 전용, hr skill이 생기면 A2A 연결)
@mock_only.get("/handovers")
def handovers() -> dict:
    items = mock_handover.BOARD.list_handovers()
    return ok(items, total=len(items))


@mock_only.get("/handovers/{handover_id}")
def handover(handover_id: str) -> dict:
    return ok(mock_handover.BOARD.get(handover_id))


@mock_only.get("/handover-questions")
def handover_questions() -> dict:
    """인계자의 답을 기다리는 질문 전체. 각 질문에 `context_id`가 있어 답변이 같은 사건으로 간다."""
    items = mock_handover.BOARD.pending_questions()
    return ok(items, total=len(items))


@mock_only.post("/handover-questions/{question_id}/answer")
def answer_handover_question(question_id: str, body: HandoverAnswerRequest) -> dict:
    if body.question_id != question_id:
        raise ApiError(422, "INVALID_REQUEST", "question_id: 경로와 본문이 다르다")
    if not body.answer.strip():
        raise ApiError(422, "INVALID_REQUEST", "answer: 답변이 비었다")
    if not body.idempotency_key.strip():
        raise ApiError(422, "INVALID_REQUEST", "idempotency_key: 필수다")
    return ok(mock_handover.BOARD.answer(question_id, body.answer.strip(), body.idempotency_key))


router.include_router(mock_only)
