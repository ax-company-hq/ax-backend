"""상담원 화면 읽기 API. 경로는 `/api/dashboard/<복수 명사>`.

dashboard-web 옛 경로와의 대응은 README "엔드포인트" 표를 본다. 지금은 목 모드(`DASHBOARD_MOCK=1`)만
응답한다. 3단계부터 support 에이전트 skill(A2A), shop-api·memory 읽기 API로 바꾼다.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query

from ax_contracts.schemas.hr import (
    AnswerHandoverQuestionOutput,
    AuditEntry,
    ChangeStatus,
    CheckPlanInput,
    CheckPlanOutput,
    DecideCandidateCardOutput,
    GetRosterOutput,
    HandoverAnswerRequest,
    HandoverQuestion,
    HandoverStatus,
    HandoverView,
)
from ax_contracts.schemas.record import Card
from ax_dashboard_api import (
    claim_mapping,
    hr_client,
    mock_actions,
    mock_customers,
    mock_data,
    mock_handover,
    mock_hr,
    relay,
    support_client,
)
from ax_dashboard_api import (
    communications as communication_log,
)
from ax_dashboard_api.auth import require_human_for_writes
from ax_dashboard_api.envelope import ApiError, is_mock, ok, require_mock
from ax_dashboard_api.schemas import (
    AgentMessageBody,
    CandidateDecisionBody,
    Conversation,
    ConversationCreate,
    ConversationListItem,
    DraftStyleBody,
    InspectionBody,
    Kpi,
    NoteBody,
    OnboardingStepBody,
    QueueProcessBody,
    ReviewStatusBody,
    StatusBody,
    Summary,
    TextBody,
    Validation,
)

HR_LIST_LIMIT = 200  # hr 조회 skill의 최대 건수

router = APIRouter(prefix="/api/dashboard", dependencies=[Depends(require_human_for_writes)])
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


# ------------------------------------ 인사 탭: 인수인계·후보 카드
# 목 모드는 `mock_handover`, 아니면 hr skill을 A2A로 부른다 (`hr_client`).
# 응답은 계약 모델 그대로다.
@router.get("/handovers")
async def handovers(status: HandoverStatus | None = None, area: str | None = None) -> dict:
    if is_mock():
        items = mock_handover.BOARD.list_handovers(status, area)
    else:
        items = await _live_handovers(status=status, area=area)
    return ok(items, total=len(items))


async def _live_handovers(**filters: str | None) -> list[HandoverView]:
    payload = {k: v for k, v in filters.items() if v is not None}
    out = await hr_client.call("list_handovers", {**payload, "limit": HR_LIST_LIMIT})
    return [HandoverView.model_validate(h) for h in out["handovers"]]


@router.get("/handovers/{handover_id}")
async def handover(handover_id: str) -> dict:
    if is_mock():
        return ok(mock_handover.BOARD.get(handover_id))
    found = next((h for h in await _live_handovers() if h.handover_id == handover_id), None)
    if found is None:  # hr에 단건 조회 skill이 없어 목록에서 찾는다
        raise ApiError(404, "NOT_FOUND", f"인수인계 {handover_id}를 찾을 수 없다")
    return ok(found)


@router.get("/handover-questions")
async def handover_questions(
    assignee_id: str | None = None, handover_id: str | None = None
) -> dict:
    """인계자의 답을 기다리는 질문 전체. 각 질문에 `context_id`가 있어 답변이 같은 사건으로 간다."""
    if is_mock():
        items = mock_handover.BOARD.pending_questions(assignee_id, handover_id)
    else:
        items = await _live_questions(assignee_id=assignee_id, handover_id=handover_id)
    return ok(items, total=len(items))


async def _live_questions(**filters: str | None) -> list[HandoverQuestion]:
    out = await hr_client.call(
        "list_open_questions", {k: v for k, v in filters.items() if v is not None}
    )
    return [HandoverQuestion.model_validate(q) for q in out["questions"]]


@router.post("/handover-questions/{question_id}/answer")
async def answer_handover_question(question_id: str, body: HandoverAnswerRequest) -> dict:
    if body.question_id != question_id:
        raise ApiError(422, "INVALID_REQUEST", "question_id: 경로와 본문이 다르다")
    answer = body.answer.strip()
    if not answer:
        raise ApiError(422, "INVALID_REQUEST", "answer: 답변이 비었다")
    if not body.idempotency_key.strip():
        raise ApiError(422, "INVALID_REQUEST", "idempotency_key: 필수다")
    if is_mock():
        return ok(mock_handover.BOARD.answer(question_id, answer, body.idempotency_key))
    # 답변은 질문이 속한 사건의 context_id로 보낸다. 이미 답해서 목록에서 빠진 질문(같은 답의
    # 재전송)은 사건을 알 수 없어 새 사건으로 보낸다. hr은 질문 ID로 대기 상태를 찾고 같은 답은
    # 한 번만 반영한다.
    known = next((q for q in await _live_questions() if q.question_id == question_id), None)
    out = await hr_client.call(
        "answer_handover_question",
        {"question_id": question_id, "answer": answer},
        context_id=known.context_id if known else None,
        key=body.idempotency_key,
    )
    return ok(AnswerHandoverQuestionOutput.model_validate(out))


@router.get("/candidate-cards")
async def candidate_cards(area: str | None = None) -> dict:
    """확신이 낮아 후보로 둔 카드 (사람이 승인·폐기)."""
    if is_mock():
        items = mock_handover.BOARD.candidate_cards(area)
    else:
        out = await hr_client.call("list_candidate_cards", {"area": area} if area else {})
        items = [Card.model_validate(c) for c in out["cards"]]
    return ok(items, total=len(items))


@router.post("/candidate-cards/{card_id}/decision")
async def decide_candidate_card(card_id: str, body: CandidateDecisionBody) -> dict:
    """후보 카드 승인·폐기. hr이 `decide_candidate_card`(A)로 실행하고, 같은 결정은 성공한다."""
    if is_mock():
        return ok(mock_handover.BOARD.decide(card_id, body.decision))
    payload: dict[str, str] = {"card_id": card_id, "decision": body.decision}
    if body.note and body.note.strip():
        payload["note"] = body.note.strip()
    out = await hr_client.call(
        "decide_candidate_card", payload, target=f"card:{card_id}:{body.decision}"
    )
    return ok(DecideCandidateCardOutput.model_validate(out))


# ------------------- 폐기 예정: 온보딩·발령 (hr에서 기능을 뺐다, #374·#377)
# 경로를 지우면 깨지는 변경(`contracts-compat`)이라 응답만 바꿔 두고, 프론트(인사 화면)가 쓰지
# 않게 된 뒤 따로 지운다. hr에는 이 skill이 없으니 부르지 않는다. 쿼리·본문 모양은 그대로 둔다.
RETIRED_MESSAGE = "온보딩·발령 기능은 인사 에이전트에서 빠졌다. 이 경로는 폐기 예정이다"


@router.get("/onboardings")
async def onboardings(member_id: str | None = None, area: str | None = None) -> dict:
    return ok([], total=0)


@router.post("/onboardings/{onboarding_id}/steps/{step_id}")
async def update_onboarding_step(
    onboarding_id: str, step_id: str, body: OnboardingStepBody
) -> dict:
    raise ApiError(410, "ENDPOINT_RETIRED", RETIRED_MESSAGE)


@router.get("/assignment-changes")
async def assignment_changes(
    status: ChangeStatus | None = None, area: str | None = None, member_id: str | None = None
) -> dict:
    return ok([], total=0)


@router.get("/roster")
async def roster(area: str | None = None) -> dict:
    """영역별 담당·교대와 담당 공백. 개인 정보는 없다."""
    if is_mock():
        return ok(mock_hr.BOARD.get_roster(area))
    return ok(GetRosterOutput.model_validate(await hr_client.call("get_roster", _given(area=area))))


@router.get("/hr-activity/{context_id}")
async def hr_activity(context_id: str) -> dict:
    """사건에서 hr이 한 일의 시간순 기록."""
    if is_mock():
        items = mock_hr.BOARD.audit_timeline(context_id)
    else:
        out = await hr_client.call("get_audit_timeline", {"context_id": context_id})
        items = [AuditEntry.model_validate(e) for e in out["entries"]]
    return ok(items, total=len(items))


@router.post("/plan-checks")
async def plan_checks(body: CheckPlanInput) -> dict:
    """새 계획이 과거에 기각된 안과 겹치는지 판정한다 (읽기, 저장 안 함)."""
    if is_mock():
        return ok(mock_hr.BOARD.check_plan(body.draft))
    out = await hr_client.call("check_plan", {"draft": body.draft.model_dump(mode="json")})
    return ok(CheckPlanOutput.model_validate(out))


# ---------------------------------------- 고객관리 탭 (읽기 전용: support skill이 코드로 센 값)
# 응답 모양은 support 출력을 그대로 둔다 (위험 단계·보상 제안은 코드가 사실에서 낸 값이다).
@router.get("/customers/at-risk")
async def customers_at_risk(
    min_level: Literal["medium", "high"] = "medium", limit: int = Query(20, ge=1, le=50)
) -> dict:
    """이탈·재문의 위험이 기준 단계 이상인 구매자 (점수 높은 순)."""
    if is_mock():
        out = mock_customers.at_risk(min_level, limit)
    else:
        out = await support_client.call(
            "list_at_risk_customers", {"min_level": min_level, "limit": limit}
        )
    return ok(out["items"], total=out["total"])


@router.get("/customers/{buyer_id}/signals")
async def customer_signals(buyer_id: str) -> dict:
    """구매자 한 명의 위험 신호와 이유별 점수."""
    if is_mock():
        return ok(mock_customers.signals(buyer_id))
    return ok(await support_client.call("customer_signals", {"buyer_id": buyer_id}))


@router.get("/conversations/{conversation_id}/compensation-suggestion")
async def compensation_suggestion(conversation_id: int) -> dict:
    """문의의 주문 사실로 보상 쿠폰 후보를 제안한다 (읽기). 요청은 사람이 승인해 따로 보낸다."""
    if is_mock():
        return ok(mock_customers.compensation_suggestion(conversation_id))
    return ok(await support_client.call("suggest_compensation", {"inquiry_id": conversation_id}))


@router.get("/conversations/{conversation_id}/payment-diagnosis")
async def payment_diagnosis(conversation_id: int) -> dict:
    """문의의 주문에서 결제가 어디서 막혔는지 (읽기)."""
    if is_mock():
        return ok(mock_customers.payment_diagnosis(conversation_id))
    return ok(await support_client.call("diagnose_payment", {"inquiry_id": conversation_id}))


@router.get("/inquiry-surge")
async def inquiry_surge() -> dict:
    """최근 창의 분류별 문의 급증 (평소 대비)."""
    if is_mock():
        return ok(mock_customers.inquiry_surge())
    return ok(await support_client.call("detect_inquiry_surge"))


@router.get("/change-requests")
async def change_requests(
    inquiry_id: int | None = None,
    status: Literal["requested", "completed", "declined"] | None = None,
) -> dict:
    """문의에서 기획개발로 보낸 변경 요청과 처리 상태."""
    if is_mock():
        out = mock_customers.change_requests(inquiry_id, status)
    else:
        payload = {k: v for k, v in {"inquiry_id": inquiry_id, "status": status}.items() if v}
        out = await support_client.call("list_change_requests", payload)
    return ok(out["items"], total=len(out["items"]))


@router.get("/communications")
def communications(
    dept: str | None = None, context_id: str | None = None, include_events: bool = False
) -> dict:
    """부서별 소통 탭. 공통 로그의 A2A 요청·응답을 한 줄씩 보여 준다 (코드가 만든 필드만).

    `include_events=true`면 발행된 이벤트 줄(`kind="event"`, 종류만)도 함께 보여 준다.
    """
    lines, truncated = communication_log.list_communications(
        dept, context_id, include_events=include_events
    )
    body = ok(lines, total=len(lines))
    body["meta"]["truncated"] = truncated
    return body


# ------------------------------------ AI 대화방: 총괄 사건·안전 관문 기록 (읽기 전용 중계)
@router.get("/incidents")
async def incidents() -> dict:
    """총괄이 다루는 사건(단계·이벤트·사람 결정 대기). 사람 결정은 안전 관문 보류 큐에서 한다."""
    items = relay.mock_incidents() if is_mock() else await relay.incidents()
    return ok(items, total=len(items))


@router.get("/incidents/{context_id}")
async def incident(context_id: str) -> dict:
    if is_mock():
        found = next((i for i in relay.mock_incidents() if i["context_id"] == context_id), None)
        if found is None:
            raise ApiError(404, "NOT_FOUND", f"사건 {context_id}를 찾을 수 없다")
        return ok(found)
    return ok(await relay.incident(context_id))


@router.get("/executions")
async def executions(context_id: str | None = None) -> dict:
    """안전 관문 실행 기록. 요청 원문(`params`)은 빼고 판정과 금액만 보여 준다."""
    if is_mock():
        items = [
            e
            for e in relay.mock_executions()
            if context_id is None or e["request"]["context_id"] == context_id
        ]
    else:
        items = await relay.executions(context_id)
    return ok(items, total=len(items))


def _given(**filters: str | None) -> dict[str, str]:
    """값이 있는 필터만 hr 입력으로 보낸다."""
    return {k: v for k, v in filters.items() if v is not None}


router.include_router(mock_only)
