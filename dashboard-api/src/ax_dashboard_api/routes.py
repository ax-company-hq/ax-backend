"""상담원 화면 읽기 API. 경로는 `/api/dashboard/<복수 명사>`.

dashboard-web 옛 경로와의 대응은 README "엔드포인트" 표를 본다. 지금은 목 모드(`DASHBOARD_MOCK=1`)만
응답한다. 3단계부터 support 에이전트 skill(A2A), shop-api·memory 읽기 API로 바꾼다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ax_dashboard_api import mock_actions, mock_data
from ax_dashboard_api.envelope import ApiError, ok, require_mock
from ax_dashboard_api.schemas import (
    AgentMessageBody,
    ConversationCreate,
    DraftStyleBody,
    InspectionBody,
    NoteBody,
    QueueProcessBody,
    ReviewStatusBody,
    StatusBody,
    TextBody,
)

router = APIRouter(prefix="/api/dashboard", dependencies=[Depends(require_mock)])


@router.get("/health")
def health() -> dict:
    return ok(mock_data.health())


@router.get("/users")
def users() -> dict:
    return ok(mock_data.USERS, total=len(mock_data.USERS))


@router.get("/metrics")
def metrics(days: int = Query(14, ge=1, le=90)) -> dict:
    return ok(mock_data.dashboard(days))


@router.get("/kpis")
def kpis() -> dict:
    return ok(mock_data.KPI)


@router.get("/kb-chunks")
def kb_chunks() -> dict:
    return ok(mock_data.KB_CHUNKS, total=len(mock_data.KB_CHUNKS))


@router.get("/conversations")
def conversations() -> dict:
    items = mock_data.list_conversations()
    return ok(items, total=len(items))


@router.get("/conversations/{conversation_id}")
def conversation(conversation_id: int) -> dict:
    found = mock_data.get_conversation(conversation_id)
    if found is None:
        raise ApiError(404, "NOT_FOUND", f"문의 {conversation_id}를 찾을 수 없다")
    return ok(found)


@router.get("/orders")
def orders(user_id: int) -> dict:
    items = mock_data.ORDERS_BY_USER.get(user_id, [])
    return ok(items, total=len(items))


@router.get("/claims")
def claims(status: str | None = None) -> dict:
    items = mock_data.list_claims(status)
    return ok(items, total=len(items))


# 고정 경로가 `{claim_id}`보다 먼저 와야 한다
@router.get("/claims/stats")
def claim_stats() -> dict:
    return ok(mock_data.claim_stats())


@router.get("/claims/{claim_id}")
def claim(claim_id: int) -> dict:
    found = mock_data.get_claim(claim_id)
    if found is None:
        raise ApiError(404, "NOT_FOUND", f"클레임 {claim_id}를 찾을 수 없다")
    return ok(found)


@router.get("/queue-entries")
def queue_entries() -> dict:
    view = mock_data.queue_view()
    return ok(view, total=len(view.queue))


@router.get("/reviews")
def reviews() -> dict:
    return ok(mock_data.REVIEWS, total=len(mock_data.REVIEWS))


# ---------------------------------------------------------------- 쓰기 (목 모드)
# 승인·지급 계열(claims approve·reject·receive·inspection, queue-entries/process)은
# 실제 연결 때 support `execute()` 관문으로 넘긴다. 지금은 목 데이터만 바꾼다.
@router.post("/conversations", status_code=201)
def create_conversation(body: ConversationCreate) -> dict:
    return ok(
        mock_actions.create_conversation(body.user_id, body.subject, body.message, body.order_id)
    )


@router.post("/conversations/{conversation_id}/customer-messages", status_code=201)
def customer_message(conversation_id: int, body: TextBody) -> dict:
    return ok(mock_actions.add_customer_message(conversation_id, body.text))


@router.post("/conversations/{conversation_id}/check")
def check_reply(conversation_id: int, body: TextBody) -> dict:
    return ok(mock_actions.check_reply(conversation_id, body.text))


@router.post("/conversations/{conversation_id}/agent-messages", status_code=201)
def agent_message(conversation_id: int, body: AgentMessageBody) -> dict:
    return ok(mock_actions.add_agent_message(conversation_id, body.text))


@router.post("/conversations/{conversation_id}/reject-draft")
def reject_draft(conversation_id: int, body: DraftStyleBody) -> dict:
    mock_actions.reject_draft(conversation_id)
    return ok(None)


@router.post("/conversations/{conversation_id}/summary")
def summary(conversation_id: int) -> dict:
    return ok(mock_actions.summarize(conversation_id))


@router.post("/conversations/{conversation_id}/status")
def conversation_status(conversation_id: int, body: StatusBody) -> dict:
    return ok(mock_actions.set_status(conversation_id, body.status))


@router.post("/claims/{claim_id}/approve")
def approve_claim(claim_id: int, body: NoteBody) -> dict:
    return ok(mock_actions.approve_claim(claim_id, body.note))


@router.post("/claims/{claim_id}/reject")
def reject_claim(claim_id: int, body: NoteBody) -> dict:
    return ok(mock_actions.reject_claim(claim_id, body.note))


@router.post("/claims/{claim_id}/receive")
def receive_claim(claim_id: int, body: InspectionBody) -> dict:
    return ok(mock_actions.receive_claim(claim_id, body.inspection, body.note))


@router.post("/claims/{claim_id}/inspection")
def inspect_claim(claim_id: int, body: InspectionBody) -> dict:
    return ok(mock_actions.inspect_claim(claim_id, body.inspection, body.note))


@router.post("/queue-entries/process")
def process_queue(body: QueueProcessBody | None = None) -> dict:
    body = body or QueueProcessBody()
    return ok(mock_actions.process_queue(body.limit, body.claim_ids))


@router.post("/reviews/{review_id}/status")
def review_status(review_id: int, body: ReviewStatusBody) -> dict:
    return ok(mock_actions.set_review_status(review_id, body.status))
