"""상담원 화면 읽기 API. 경로는 `/api/dashboard/<복수 명사>`.

dashboard-web 옛 경로와의 대응은 README "엔드포인트" 표를 본다. 지금은 목 모드(`DASHBOARD_MOCK=1`)만
응답한다. 3단계부터 support 에이전트 skill(A2A), shop-api·memory 읽기 API로 바꾼다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ax_dashboard_api import mock_actions, mock_data, support_client
from ax_dashboard_api.envelope import ApiError, is_mock, ok, require_mock
from ax_dashboard_api.schemas import (
    AgentMessageBody,
    Conversation,
    ConversationCreate,
    ConversationListItem,
    DraftStyleBody,
    InspectionBody,
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


@mock_only.get("/kpis")
def kpis() -> dict:
    return ok(mock_data.KPI)


@mock_only.get("/kb-chunks")
def kb_chunks() -> dict:
    return ok(mock_data.KB_CHUNKS, total=len(mock_data.KB_CHUNKS))


@mock_only.get("/orders")
def orders(user_id: int) -> dict:
    items = mock_data.ORDERS_BY_USER.get(user_id, [])
    return ok(items, total=len(items))


@mock_only.get("/claims")
def claims(status: str | None = None) -> dict:
    items = mock_data.list_claims(status)
    return ok(items, total=len(items))


# 고정 경로가 `{claim_id}`보다 먼저 와야 한다
@mock_only.get("/claims/stats")
def claim_stats() -> dict:
    return ok(mock_data.claim_stats())


@mock_only.get("/claims/{claim_id}")
def claim(claim_id: int) -> dict:
    found = mock_data.get_claim(claim_id)
    if found is None:
        raise ApiError(404, "NOT_FOUND", f"클레임 {claim_id}를 찾을 수 없다")
    return ok(found)


@mock_only.get("/queue-entries")
def queue_entries() -> dict:
    view = mock_data.queue_view()
    return ok(view, total=len(view.queue))


@mock_only.get("/reviews")
def reviews() -> dict:
    return ok(mock_data.REVIEWS, total=len(mock_data.REVIEWS))


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
@mock_only.post("/claims/{claim_id}/approve")
def approve_claim(claim_id: int, body: NoteBody) -> dict:
    return ok(mock_actions.approve_claim(claim_id, body.note))


@mock_only.post("/claims/{claim_id}/reject")
def reject_claim(claim_id: int, body: NoteBody) -> dict:
    return ok(mock_actions.reject_claim(claim_id, body.note))


@mock_only.post("/claims/{claim_id}/receive")
def receive_claim(claim_id: int, body: InspectionBody) -> dict:
    return ok(mock_actions.receive_claim(claim_id, body.inspection, body.note))


@mock_only.post("/claims/{claim_id}/inspection")
def inspect_claim(claim_id: int, body: InspectionBody) -> dict:
    return ok(mock_actions.inspect_claim(claim_id, body.inspection, body.note))


@mock_only.post("/queue-entries/process")
def process_queue(body: QueueProcessBody | None = None) -> dict:
    body = body or QueueProcessBody()
    return ok(mock_actions.process_queue(body.limit, body.claim_ids))


@mock_only.post("/reviews/{review_id}/status")
def review_status(review_id: int, body: ReviewStatusBody) -> dict:
    return ok(mock_actions.set_review_status(review_id, body.status))


router.include_router(mock_only)
