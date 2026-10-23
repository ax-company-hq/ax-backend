"""기획개발 상태 변화 (프로토타입 `data.ts` 리듀서의 모든 액션을 메서드로 옮긴 것).

규칙:

- 상태를 바꾸는 단계(요청 등록·기획서 저장·작업 생성·PR 생성·머지·이벤트 전송·자동 범위 변경)는
  템플릿 실행 관문 `ax_agent_template.execute.execute()`를 거친다. 관문이 `executed`가 아닌 결과
  (거절·보류·무응답)를 주면 `GateRefused`를 던지고 그 액션 전체를 버린다(fail-closed).
  승급 제안(`auto_policy_propose`, C)만은 보류(`held`)가 정상 결과다 (팀장 승인 대기).
- 액션은 상태 복사본 위에서 계산하고, 끝까지 성공했을 때만 바꿔 끼운다
  (중간 실패 시 원래 상태 유지).
- 기획개발이 다른 부서로 보낸 활동은 모두 그 부서 창구(outbox)에도 쌓는다.
- 숫자·중복·판정은 `rules.py`의 코드로만 계산한다. ID·시각은 주입한 시계와 상태 길이로 만든다.
- 시뮬레이터 액션(`inbound`, `dept_chat`)은 목 모드에서만 받는다.
"""

from __future__ import annotations

import asyncio
import re
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ax_agent_template import config as template_config
from ax_agent_template import execute as gate_module
from ax_contracts.schemas import TENANT_ID_DEFAULT, Evidence, ExecuteDecision, RequestMeta
from ax_contracts.schemas.record import RecordCase, RecordKind
from planning_agent.domain.autocheck import (
    WAIT_CROSS_CHECK,
    Check,
    planned_area,
    planned_paths,
    pr_check,
    pr_wait_state,
    spec_check,
    spec_wait_state,
)
from planning_agent.domain.clock import Clock, default_clock_factory
from planning_agent.domain.docs import gather_blocks, next_block_id
from planning_agent.domain.handover import HandoverCheck, Item, check_handover, in_progress
from planning_agent.domain.models import (
    DEPT_NAME,
    PR,
    Activity,
    AutoLog,
    AutoPolicy,
    AutoProposal,
    AutoReview,
    AutoRun,
    Decision,
    Doc,
    DocBlock,
    Incident,
    IncidentCandidate,
    Message,
    OutMsg,
    Para,
    PrGate,
    Request,
    Spec,
    State,
    Task,
    dump,
)
from planning_agent.domain.rules import (
    AUTO_LEVELS,
    PROMOTE_AFTER,
    auto_limit,
    check_simple,
    deploy_of,
    estimate,
    find_duplicate,
    incident_of,
    merged_in_window,
    para_of,
    verify_rollback,
)
from planning_agent.domain.seed import initial_state
from planning_agent.domain.trace import changed_paths_for

AGENT_NAME = "planning"
TEAM_LEAD = "노예진"  # 확정·머지·승급 승인 버튼을 누르는 팀장 (목)

Gate = Callable[[str, dict, RequestMeta, Evidence], Awaitable[ExecuteDecision]]

INBOUND_KINDS = (
    "support_ticket",
    "marketing_ticket",
    "hr_draft",
    "data_draft",
    "data_query",
    "infra_query",
    "orch_incident",
    "safety_verify",
    "support_simple",
    "marketing_simple",
)
ADVANCE_LIMIT = 40  # 다음 단계 반복 상한 (무한 반복 방지)
_TICKET_KINDS = ("support_ticket", "marketing_ticket", "support_simple", "marketing_simple")

SAMPLE_TICKETS = {
    "support": [
        "배송 조회 화면에서 송장 번호가 안 보인다",
        "쿠폰 적용 후 결제 금액이 두 번 계산된다",
        "결제 실패 뒤 장바구니가 비워진다",
    ],
    "marketing": [
        "기획전 배너가 모바일에서 잘린다",
        "신간 알림 신청 버튼 추가",
        "장바구니 보존 문구를 공지에도 반영",
    ],
}
SIMPLE_TICKETS = {
    "support": [
        "상품 상세의 배송 안내 문구 오타 수정",
        "고객센터 운영 시간 안내 문구 변경",
        "마이페이지 메뉴 라벨 오타 수정",
    ],
    "marketing": [
        '신간 목록 정렬 라벨을 "최신순"으로 변경',
        "이벤트 페이지 링크가 지난 기획전으로 연결된다",
        "기획전 페이지 안내 문구 전체 교체",
    ],
}
DEPT_CHAT = {
    "support": [
        "배송 조회 송장 번호 문의가 하루 10건 넘게 들어옵니다. 우선순위 올려 주실 수 있나요?",
        "REQ-12 처리 상태 고객에게 안내해도 될까요?",
    ],
    "marketing": [
        "다음 주 기획전 배너 문구 확정본 공유드려요. 장바구니 보존 공지는 REQ-12 완료 뒤에 낼게요.",
        "신간 알림 버튼 기획 일정이 궁금합니다.",
    ],
    "hr": [
        '이번 주 인수인계 질문 중 "결제 실패 안내"가 3번 반복됐습니다. '
        "운영 매뉴얼 반영 초안 보낼게요.",
        "결정 카드 DC-44에 SPEC-7 결정 이유를 붙였습니다.",
    ],
    "data": [
        "결제 실패율이 14:00~14:10에 또 올랐습니다. 직전 배포가 있었는지 확인 부탁드려요.",
        "장바구니 이탈률 지표 정의 공유드립니다 (코드로 계산).",
    ],
    "infra": [
        "shop-api v2.4.1 롤백 후 p95가 정상 범위로 돌아왔습니다.",
        "카나리 배포 설정 초안 준비됐습니다. REQ-101에 붙여 주세요.",
    ],
}
# answerQuestion의 목 해석 (프로토타입과 같은 고정 문장. 실제로는 extract_decision이 만든다)
ANSWER_R1_TEXT = "결제 실패 시 장바구니 상태 보존 (품절 상품은 남기고 품절 표시)"
ANSWER_DECISION = {
    "text": "품절 상품은 장바구니에 남기고 품절 표시",
    "reason": "고객지원 의견: 다시 담는 불편이 더 큼",
    "alternatives": "품절 상품 자동 삭제",
}
ANSWER_WHO = "박지아 (고객지원)"


# ---------------------------------------------------------------- 관문·결과 타입


async def template_gate(
    action: str, params: dict, context: RequestMeta, evidence: Evidence
) -> ExecuteDecision:
    """템플릿 실행 관문. 모듈 속성으로 찾아 부르므로 테스트에서 `_send_to_safety`를 바꿀 수 있다."""
    return await gate_module.execute(action, params, context, evidence)


def _pr_size_params(lines: int, files: int, paths: list[str], task: str) -> dict:
    """`generate_pr` execute params. 안전 레이어 코드 규칙(#83)이 읽는 키 이름을 쓴다."""
    return {
        "changed_lines": lines,
        "changed_files": files,
        "changed_paths": sorted(set(paths)),
        "task": task,
    }


class GateRefused(Exception):
    """관문이 실행을 허락하지 않았다. 액션 전체를 버린다."""

    def __init__(self, action: str, idempotency_key: str, decision: ExecuteDecision):
        super().__init__(f"{action} 실행 거부 ({decision.status})")
        self.action = action
        self.idempotency_key = idempotency_key
        self.decision = decision


class ActionError(Exception):
    """잘못된 액션 (모르는 type, 빠진 필드, 목 모드 밖 시뮬레이터)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class GateCall:
    action: str
    idempotency_key: str
    status: str
    grade: str
    execution_id: str


@dataclass
class ActionResult:
    changed: bool = False
    outcome: str | None = None
    output: dict[str, Any] = field(default_factory=dict)


# ---- 액션 입력 (프로토타입 Action과 같은 모양)


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class InboundParams(_Params):
    kind: Literal[INBOUND_KINDS]  # type: ignore[valid-type]


class SendParams(_Params):
    to: str
    point: str
    text: str
    ctx: str = "ctx_req_0012"


class ToParams(_Params):
    to: str


class SpecParams(_Params):
    spec: str


class AnswerParams(_Params):
    spec: str
    text: str


class PrParams(_Params):
    pr: str | int


class RequestParams(_Params):
    request: str


class RejectDraftParams(_Params):
    """개선 초안 기각. 이유·검토한 대안이 비면 hr에 보내지 않고 `MISSING_DECISION_REASON`."""

    request: str
    decision_reason: str = ""
    alternatives_considered: list[str] = Field(default_factory=list)


class RejectDecisionParams(_Params):
    """기각한 안을 결정으로 남긴다 (예: 기획서 검토 중 버린 안)."""

    spec: str
    text: str
    decision_reason: str = ""
    alternatives_considered: list[str] = Field(default_factory=list)


class EmptyParams(_Params):
    pass


class PostMessageParams(_Params):
    thread: str
    text: str


class DeptChatParams(_Params):
    from_: str = Field(alias="from")


class ExtractDecisionParams(_Params):
    thread: str
    index: int


class NewDocParams(_Params):
    title: str = ""


class GatherDocParams(_Params):
    about: str
    by: Literal["ai", "human"] | None = None


class RenameDocParams(_Params):
    doc: str
    title: str


class AddBlockParams(_Params):
    doc: str
    kind: Literal["h2", "p", "todo", "ref"]
    text: str | None = None


class EditBlockParams(_Params):
    doc: str
    block: str
    text: str


class BlockParams(_Params):
    doc: str
    block: str


# snake_case 액션 이름 → (메서드, 입력 모델). 화면이 보내는 camelCase(`confirmSpec`)도 받는다.
ACTIONS: dict[str, tuple[str, type[_Params]]] = {
    "inbound": ("_inbound", InboundParams),
    "send": ("_send", SendParams),
    "mark_read": ("_mark_read", ToParams),
    "confirm_spec": ("_confirm_spec", SpecParams),
    "check_spec": ("_check_spec", SpecParams),
    "answer_question": ("_answer_question", AnswerParams),
    "generate_prs": ("_generate_prs", SpecParams),
    "review_pr": ("_review_pr", PrParams),
    "merge_pr": ("_merge_pr", PrParams),
    "accept_draft": ("_accept_draft", RequestParams),
    "reject_draft": ("_reject_draft", RejectDraftParams),
    "reject_decision": ("_reject_decision", RejectDecisionParams),
    "approve_promotion": ("_approve_promotion", EmptyParams),
    "reject_promotion": ("_reject_promotion", EmptyParams),
    "post_message": ("_post_message", PostMessageParams),
    "dept_chat": ("_dept_chat", DeptChatParams),
    "extract_decision": ("_extract_decision", ExtractDecisionParams),
    "new_doc": ("_new_doc", NewDocParams),
    "gather_doc": ("_gather_doc", GatherDocParams),
    "rename_doc": ("_rename_doc", RenameDocParams),
    "add_block": ("_add_block", AddBlockParams),
    "edit_block": ("_edit_block", EditBlockParams),
    "toggle_todo": ("_toggle_todo", BlockParams),
    "remove_block": ("_remove_block", BlockParams),
}
SIMULATOR_ACTIONS = {"inbound", "dept_chat"}


def action_name(raw: str) -> str:
    """`confirmSpec` → `confirm_spec`. 이미 snake_case면 그대로."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", raw).lower()


def _token() -> str:
    try:
        return template_config.agent_token(AGENT_NAME)
    except RuntimeError:
        return "unconfigured"  # 관문 쪽에서 거부된다 (fail-closed)


def _fmt(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%m-%d %H:%M UTC")


# ---------------------------------------------------------------- 서비스


class PlanningService:
    """프로세스 메모리에 상태 하나를 두고 액션을 적용한다 (목 모드 저장소)."""

    def __init__(
        self,
        clock: Clock | None = None,
        gate: Gate | None = None,
        tenant_id: str = TENANT_ID_DEFAULT,
    ):
        self._clock_factory: Callable[[], Clock] = (
            (lambda: clock) if clock else default_clock_factory()
        )
        self._gate = gate or template_gate
        self.tenant_id = tenant_id
        self.gate_calls: list[GateCall] = []
        self._subscribers: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = {}
        self._locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self.version = 0
        self._init_state()

    # ---- 상태·구독

    def _init_state(self) -> None:
        self.clock: Clock = self._clock_factory()
        self.state: State = initial_state()
        self.state.tenant_id = self.tenant_id
        self._idempotent: dict[str, ActionResult] = {}

    def reset(self) -> None:
        """초기 데이터로 되돌린다 (구독자는 유지하고 새 상태를 알린다)."""
        self._init_state()
        self.gate_calls.clear()
        self.version += 1
        self._notify()

    def snapshot(self) -> dict:
        return dump(self.state)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers[id(queue)] = (asyncio.get_running_loop(), queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.pop(id(queue), None)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def _notify(self) -> None:
        for key, (loop, queue) in list(self._subscribers.items()):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, self.version)
            except RuntimeError:  # 닫힌 이벤트 루프
                self._subscribers.pop(key, None)

    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            lock = self._locks[loop] = asyncio.Lock()
        return lock

    # ---- 적용 틀

    async def _apply(self, fn: Callable[..., Awaitable[ActionResult | None]], **params: Any):
        """복사본에 액션을 적용하고, 끝까지 성공하면 바꿔 끼운다. 관문 거부면 원래 상태 유지."""
        async with self._lock():
            s = self.state.model_copy(deep=True)
            before = len(s.activity)
            result = await fn(s, **params) or ActionResult()
            await self._advance(s)
            self._to_outbox(s, before)
            result.changed = s != self.state
            if result.changed:
                self.state = s
                self.version += 1
                self._notify()
            return result

    def _to_outbox(self, s: State, before: int) -> None:
        """기획개발이 다른 부서로 보낸 활동은 그 부서 창구(outbox)에도 쌓는다."""
        next_id = len(s.outbox)
        for a in s.activity[before:]:
            if a.from_ == "planning" and a.to != "planning":
                next_id += 1
                s.outbox.append(
                    OutMsg(
                        id=next_id,
                        at=a.at,
                        context_id=a.context_id,
                        tenant_id=a.tenant_id,
                        to=a.to,
                        point=a.point,
                        text=a.text,
                    )
                )

    async def _execute(
        self,
        action: str,
        ctx: str,
        target: str,
        summary: str,
        params: dict | None = None,
        refs: list[str] | None = None,
        allow: tuple[str, ...] = ("executed",),
    ) -> ExecuteDecision:
        """실행 관문을 거친다. 허용 상태가 아니면 `GateRefused` (fail-closed)."""
        key = f"{ctx}:{action}:{target}"
        meta = RequestMeta(
            context_id=ctx,
            tenant_id=self.tenant_id,
            token=_token(),
            deadline=datetime.now(UTC) + timedelta(seconds=30),
            idempotency_key=key,
        )
        evidence = Evidence(data_summary=summary, ref_ids=refs or [target])
        try:
            decision = await self._gate(
                action, {"target": target, **(params or {})}, meta, evidence
            )
        except Exception:
            decision = ExecuteDecision(execution_id="exe_unavailable", grade="C", status="rejected")
        self.gate_calls.append(
            GateCall(action, key, decision.status, decision.grade, decision.execution_id)
        )
        if decision.status not in allow:
            raise GateRefused(action, key, decision)
        return decision

    def _act(
        self,
        at: datetime,
        ctx: str,
        frm: str,
        to: str,
        point: str,
        kind: str,
        text: str,
        grade: str | None = None,
        seq: int | None = None,
    ) -> Activity:
        return Activity(
            seq=seq,
            at=at,
            context_id=ctx,
            tenant_id=self.tenant_id,
            from_=frm,
            to=to,
            point=point,
            kind=kind,
            grade=grade,
            text=text,
        )

    @staticmethod
    def _request_of_spec(s: State, spec: Spec) -> Request | None:
        return next((r for r in s.requests if r.spec == spec.id), None) or next(
            (r for r in s.requests if r.id == spec.request), None
        )

    @staticmethod
    def _find_pr(s: State, ref: str | int) -> PR | None:
        if isinstance(ref, int):
            return next((p for p in s.prs if p.number == ref), None)
        found = next((p for p in s.prs if p.id == ref), None)
        if found:
            return found
        digits = re.sub(r"\D", "", ref)
        return next((p for p in s.prs if digits and p.number == int(digits)), None)

    def _hold(
        self,
        target: Spec | PR,
        state: str,
        check: Check,
        decision: ExecuteDecision | None = None,
    ) -> AutoReview:
        """자동 확정·자동 머지를 멈추고 대기열에 올린다 (판단은 기존 사람 버튼)."""
        target.auto_review = AutoReview(
            state=state,
            rules=list(check.rules),
            params=dict(check.params),
            at=self.clock(),
            gate_status=decision.status if decision else None,
            review_id=decision.review_id if decision else None,
        )
        return target.auto_review

    # ---- 화면 액션 진입점

    async def dispatch(self, action: dict) -> ActionResult:
        """`{"type": "confirmSpec", "spec": "SPEC-7"}` 같은 프로토타입 Action을 적용한다."""
        if not isinstance(action, dict) or not isinstance(action.get("type"), str):
            raise ActionError("INVALID_ACTION", "type이 없다")
        name = action_name(action["type"])
        if name not in ACTIONS:
            raise ActionError("UNKNOWN_ACTION", f"모르는 액션: {action['type']}")
        if name in SIMULATOR_ACTIONS and not template_config.is_mock(AGENT_NAME):
            raise ActionError("SIMULATOR_DISABLED", "시뮬레이터 액션은 목 모드에서만 받는다")
        method, model = ACTIONS[name]
        try:
            params = model.model_validate({k: v for k, v in action.items() if k != "type"})
        except ValidationError as exc:
            raise ActionError("INVALID_ACTION", str(exc)) from exc
        kwargs = {k: v for k, v in params.model_dump().items()}
        return await self._apply(getattr(self, method), **kwargs)

    # 이름이 있는 메서드 (테스트·내부 호출용). 모두 dispatch와 같은 길을 탄다.
    async def inbound(self, kind: str) -> ActionResult:
        return await self.dispatch({"type": "inbound", "kind": kind})

    async def confirm_spec(self, spec: str) -> ActionResult:
        return await self.dispatch({"type": "confirm_spec", "spec": spec})

    async def check_spec(self, spec: str) -> ActionResult:
        return await self.dispatch({"type": "check_spec", "spec": spec})

    async def answer_question(self, spec: str, text: str) -> ActionResult:
        return await self.dispatch({"type": "answer_question", "spec": spec, "text": text})

    async def generate_prs(self, spec: str) -> ActionResult:
        return await self.dispatch({"type": "generate_prs", "spec": spec})

    async def review_pr(self, pr: str) -> ActionResult:
        return await self.dispatch({"type": "review_pr", "pr": pr})

    async def merge_pr(self, pr: str) -> ActionResult:
        return await self.dispatch({"type": "merge_pr", "pr": pr})

    async def accept_draft(self, request: str) -> ActionResult:
        return await self.dispatch({"type": "accept_draft", "request": request})

    async def reject_draft(
        self,
        request: str,
        decision_reason: str = "",
        alternatives_considered: list[str] | None = None,
    ) -> ActionResult:
        return await self.dispatch(
            {
                "type": "reject_draft",
                "request": request,
                "decision_reason": decision_reason,
                "alternatives_considered": alternatives_considered or [],
            }
        )

    async def reject_decision(
        self,
        spec: str,
        text: str,
        decision_reason: str = "",
        alternatives_considered: list[str] | None = None,
    ) -> ActionResult:
        return await self.dispatch(
            {
                "type": "reject_decision",
                "spec": spec,
                "text": text,
                "decision_reason": decision_reason,
                "alternatives_considered": alternatives_considered or [],
            }
        )

    async def approve_promotion(self) -> ActionResult:
        return await self.dispatch({"type": "approve_promotion"})

    async def reject_promotion(self) -> ActionResult:
        return await self.dispatch({"type": "reject_promotion"})

    async def post_message(self, thread: str, text: str) -> ActionResult:
        return await self.dispatch({"type": "post_message", "thread": thread, "text": text})

    async def dept_chat(self, from_: str) -> ActionResult:
        return await self.dispatch({"type": "dept_chat", "from": from_})

    async def extract_decision(self, thread: str, index: int) -> ActionResult:
        return await self.dispatch({"type": "extract_decision", "thread": thread, "index": index})

    async def new_doc(self, title: str = "") -> ActionResult:
        return await self.dispatch({"type": "new_doc", "title": title})

    async def gather_doc(self, about: str, by: str | None = None) -> ActionResult:
        return await self.dispatch({"type": "gather_doc", "about": about, "by": by})

    async def rename_doc(self, doc: str, title: str) -> ActionResult:
        return await self.dispatch({"type": "rename_doc", "doc": doc, "title": title})

    async def add_block(self, doc: str, kind: str, text: str | None = None) -> ActionResult:
        return await self.dispatch({"type": "add_block", "doc": doc, "kind": kind, "text": text})

    async def edit_block(self, doc: str, block: str, text: str) -> ActionResult:
        return await self.dispatch({"type": "edit_block", "doc": doc, "block": block, "text": text})

    async def toggle_todo(self, doc: str, block: str) -> ActionResult:
        return await self.dispatch({"type": "toggle_todo", "doc": doc, "block": block})

    async def remove_block(self, doc: str, block: str) -> ActionResult:
        return await self.dispatch({"type": "remove_block", "doc": doc, "block": block})

    async def send(self, to: str, point: str, text: str) -> ActionResult:
        return await self.dispatch({"type": "send", "to": to, "point": point, "text": text})

    async def mark_read(self, to: str) -> ActionResult:
        return await self.dispatch({"type": "mark_read", "to": to})

    # ---------------------------------------------------------------- 들어오는 연동 (시뮬레이터)

    async def _inbound(self, s: State, kind: str) -> ActionResult | None:
        seq = s.seq + 1
        t = self.clock()
        ctx_req = f"ctx_req_{20 + seq:04d}"
        merged = [p for p in s.prs if p.merged]
        last_merged = merged[-1] if merged else None
        s.seq = seq

        def act(ctx, frm, to, point, kind_, text, grade=None):
            return self._act(t, ctx, frm, to, point, kind_, text, grade, seq)

        if kind in _TICKET_KINDS:
            frm = "support" if kind.startswith("support") else "marketing"
            simple = kind.endswith("simple")
            pool = SIMPLE_TICKETS[frm] if simple else SAMPLE_TICKETS[frm]
            # 간단 요청은 아직 안 보낸 것부터, 일반 요청은 돌아가며 (중복 연결 시연용)
            used = {r.title for r in s.requests}
            if simple:
                title = next(
                    (x for x in pool if x not in used), f"{pool[seq % len(pool)]} ({seq}차)"
                )
            else:
                title = pool[seq % len(pool)]
            return await self._register_ticket(s, frm, title, ctx_req, t, seq)

        if kind in ("hr_draft", "data_draft"):
            frm = "hr" if kind == "hr_draft" else "data"
            req_id = f"REQ-{20 + seq}"
            title = (
                "반복되는 인수인계 질문을 운영 매뉴얼에 반영"
                if frm == "hr"
                else "같은 시간대 결제 실패율 반복 상승 원인 개선"
            )
            via = "ticket_draft_propose (③)" if frm == "hr" else "개선 초안 (원인 추적 루프 ③)"
            text = f"개선 초안 수신 → 요청함 {req_id}"
            await self._execute(
                "create_ticket", ctx_req, req_id, text, {"title": title, "via": via}
            )
            s.drafts_in += 1
            s.requests.append(
                Request(
                    id=req_id,
                    context_id=ctx_req,
                    tenant_id=self.tenant_id,
                    from_=frm,
                    via=via,
                    title=title,
                    evidence=f"{DEPT_NAME[frm]} 시뮬레이터에서 보냄",
                    state="접수",
                    flow=True,
                )
            )
            point = "ticket_draft_propose" if frm == "hr" else "개선 초안 (루프 ③)"
            s.activity.append(act(ctx_req, frm, "planning", point, "A2A", text, "A"))
            return ActionResult(output={"request_id": req_id})

        if kind in ("data_query", "infra_query"):
            frm = "data" if kind == "data_query" else "infra"
            ctx = s.incidents[0].context_id if s.incidents else "ctx_inc_0001"
            found = f"{last_merged.id} ({last_merged.title})" if last_merged else "후보 없음"
            ask = (
                "이상 구간 직전 배포가 있었나 질의"
                if frm == "data"
                else "장애 시각 직전 머지 PR 질의"
            )
            s.activity.append(act(ctx, frm, "planning", "find_related_prs", "A2A", ask, "read"))
            s.activity.append(
                act(
                    ctx,
                    "planning",
                    frm,
                    "find_related_prs",
                    "A2A",
                    f"관련 PR 후보 회신: {found} · 원인 판정은 하지 않음",
                    "read",
                )
            )
            return None

        if kind == "orch_incident":
            n = len(s.incidents) + 1
            while any(i.context_id == f"ctx_inc_{n:04d}" for i in s.incidents):
                n += 1
            ctx = f"ctx_inc_{n:04d}"
            pr = last_merged or (s.prs[0] if s.prs else None)
            if pr is None:
                return None
            s.activity.append(
                act(
                    ctx,
                    "orchestrator",
                    "planning",
                    "incident.opened",
                    "이벤트",
                    f"사건 {ctx} 개시 (infra.anomaly_detected 감지)",
                )
            )
            s.activity.append(
                act(
                    ctx,
                    "orchestrator",
                    "planning",
                    "link_incident",
                    "A2A",
                    f"사건을 {pr.id}에 연결 요청",
                    "A",
                )
            )
            linked = await self._link(
                s,
                ctx,
                [pr],
                title=f"결제 API 응답 지연 (시뮬레이터 {n}회차)",
                follow_up_title=f"{pr.id} 관련 재발 방지 검토",
                follow_from="infra",
                at=t,
                seq=seq,
            )
            s.activity.append(
                act(ctx, "planning", "orchestrator", "link_incident", "A2A", linked["reply"], "A")
            )
            s.activity.extend(linked["demote_acts"])
            return ActionResult(
                outcome="linked_duplicate" if linked["duplicate"] else None,
                output={
                    "incident": ctx,
                    "linked_paras": linked["linked_paras"],
                    "follow_up_request_id": linked["follow_id"],
                },
            )

        # safety_verify: infra rollback_deployment(B)의 교차검증 (시뮬레이터는 마지막 머지 PR 기준)
        ok = last_merged is not None
        ctx = s.incidents[0].context_id if s.incidents else "ctx_inc_0001"
        s.activity.append(
            act(
                ctx,
                "safety",
                "planning",
                "verify_rollback_target",
                "A2A",
                "infra rollback_deployment(B) 교차검증 요청",
                "read",
            )
        )
        reply = (
            f"승인 · 장애 직전 머지 {last_merged.id} 확인 (deploy_before_incident)"
            if last_merged
            else "거절 · 기간 안 배포 없음 (no_deploy_in_window) → completed + rejected_by_review"
        )
        s.activity.append(
            act(ctx, "planning", "safety", "verify_rollback_target", "A2A", reply, "read")
        )
        return ActionResult(outcome=None if ok else "rejected_by_review", output={"approved": ok})

    async def _register_ticket(
        self,
        s: State,
        frm: str,
        title: str,
        ctx_req: str,
        t: datetime,
        seq: int,
        *,
        evidence: str | None = None,
        source_ref: str | None = None,
        evidence_ids: list[str] | None = None,
        req_id: str | None = None,
    ) -> ActionResult:
        """create_ticket: 같은 제목이면 중복 연결, 아니면 등록 후 간단 요청 판정.

        `source_ref`·`evidence_ids`는 저장만 한다 (중복·간단 요청 판정에 쓰지 않는다).
        """
        dup_id = find_duplicate(s.requests, title)
        req_id = req_id or f"REQ-{20 + seq}"
        auto = None if dup_id else check_simple(title, s.auto_policy)
        text = (
            f'"{title}" → 같은 요청 {dup_id}에 연결 (linked_duplicate)'
            if dup_id
            else f"변경 요청 {req_id} 등록: {title}"
        )
        await self._execute(
            "create_ticket", ctx_req, req_id, text, {"title": title, "duplicate_of": dup_id}
        )
        req = Request(
            id=req_id,
            context_id=ctx_req,
            tenant_id=self.tenant_id,
            from_=frm,
            via="create_ticket",
            title=title,
            evidence=evidence or f"{DEPT_NAME[frm]} 시뮬레이터에서 보냄",
            state="중복 연결" if dup_id else "접수",
            duplicate_of=dup_id,
            auto=auto,
            source_ref=source_ref,
            evidence_ids=list(evidence_ids or []),
        )
        s.requests.append(req)
        s.activity.append(
            self._act(t, ctx_req, frm, "planning", "create_ticket", "A2A", text, "A", seq)
        )
        if auto is None:
            s.activity.append(
                self._act(
                    t,
                    ctx_req,
                    "planning",
                    frm,
                    "get_request_status",
                    "A2A",
                    f"{req_id}는 {dup_id}에 연결됨",
                    "read",
                    seq,
                )
            )
            return ActionResult(
                outcome="linked_duplicate", output={"request_id": req_id, "duplicate_of": dup_id}
            )
        if auto.ok:
            judged = "간단 요청 판정 → 사람 승인 없이 끝까지 처리"
        else:
            need = [r for r in auto.reasons if "필요" in r or "초과" in r]
            judged = f"일반 요청 · {', '.join(need)} → 기획서 초안부터 사람 클릭 없이 진행"
        s.activity.append(
            self._act(t, ctx_req, "planning", "planning", "classify_request", "A2A", judged, "read")
        )
        if auto.ok:
            await self._auto_complete(s, req)
            return ActionResult(output={"request_id": req_id, "auto": True})
        req.flow = True
        s.activity.append(
            self._act(
                t,
                ctx_req,
                "planning",
                frm,
                "get_request_status",
                "A2A",
                f"{req_id} 접수됨 → 기획서 작성",
                "read",
            )
        )
        return ActionResult(output={"request_id": req_id, "auto": False})

    # ---- 사람 클릭 없이 다음 단계로 (flow 요청만, 시드 건은 그대로)

    async def _advance(self, s: State) -> None:
        """액션마다 더 진행할 게 없을 때까지 다음 단계를 돈다: 기획서 초안(개선 초안은 채택 먼저) →
        자동 확정 검사 → PR 만들기 → 검수 불일치면 수정 후 재검수 → 자동 머지 검사.

        멈추는 곳: 자동 확정·머지 규칙에 걸린 건(`auto_review` 대기열), 검수 불일치 2회,
        safety 보류·거절. 관문이 거부하면 그 단계에서 멈추고 앞선 단계는 그대로 둔다."""
        if s.auto_policy.level == 0:  # L0 = 자동 처리 중지: 접수에서 멈춘다
            return
        for _ in range(ADVANCE_LIMIT):
            try:
                if not await self._advance_step(s):
                    return
            except GateRefused:
                return

    async def _advance_step(self, s: State) -> bool:
        flow = {r.id for r in s.requests if r.flow}
        for r in s.requests:
            if r.flow and r.state == "접수" and r.spec is None and r.duplicate_of is None:
                if r.via != "create_ticket":
                    await self._accept_draft(s, r.id)
                await self._draft_spec(s, r)
                return True
        for sp in s.specs:
            if sp.request not in flow or sp.status != "확정":
                continue
            has_pr = {p.task for p in s.prs}
            if any(
                t.spec == sp.id and t.state == "진행 중" and t.id not in has_pr for t in s.tasks
            ):
                await self._generate_prs(s, sp.id)
                return True
        for p in s.prs:
            task = next((t for t in s.tasks if t.id == p.task), None)
            sp = next((x for x in s.specs if task and x.id == task.spec), None)
            if sp is None or sp.request not in flow or p.merged or p.auto_review is not None:
                continue
            if p.reviews and p.reviews[-1] == "불일치" and p.reviews.count("불일치") < 2:
                await self._review_pr(s, p.id)
                return True
        return False

    async def _draft_spec(self, s: State, req: Request) -> None:
        """요청으로 기획서 초안을 쓰고(write_spec) 자동 확정 규칙으로 넘긴다."""
        spec_id = f"SPEC-{len(s.specs) + 6}"
        ctx = req.context_id
        t = self.clock()
        s.activity.append(
            self._act(
                t, ctx, "planning", "hr", "check_plan", "A2A", "② 기각 이력 질의 → 없음", "read"
            )
        )
        text = f"{spec_id} 초안 작성 (요구사항 1, 완료 기준 1, 질문 0) → 확정 조건 확인"
        await self._execute("write_spec", ctx, spec_id, text, {"request": req.id})
        sp = Spec(
            id=spec_id,
            title=req.title,
            request=req.id,
            status="초안",
            problem=f"{DEPT_NAME[req.from_]}에서 받은 요청: {req.evidence}",
            goal=req.title,
            paras=[
                Para(id="R1", kind="req", text=req.title),
                Para(id="C1", kind="done", text="바뀐 뒤 같은 문의·이상 신호가 다시 생기지 않는다"),
            ],
            basis=[f"{req.id} ({DEPT_NAME[req.from_]})", req.evidence],
        )
        s.specs.append(sp)
        req.spec = spec_id
        req.state = "기획 중"
        s.activity.append(self._act(t, ctx, "planning", "planning", "write_spec", "A2A", text, "A"))
        await self._try_auto_confirm(s, sp)

    async def _auto_complete(self, s: State, req: Request) -> None:
        """간단 요청 한 번에 처리: write_spec → 자동 확정 → split_tasks → generate_pr(B) →
        review_pr → merge_pr(B) → record_case → 요청 부서 회신. 사람 클릭 없음."""
        spec_id = f"SPEC-{len(s.specs) + 6}"
        task_id = f"T-{len(s.tasks) + 10}"
        number = len(s.prs) + 30
        pr_id = f"PR #{number}"
        lines, files = estimate(req.title)
        limit = auto_limit(s.auto_policy)
        ctx = req.context_id
        pol = s.auto_policy
        streak = pol.streak + 1
        t = self.clock()
        propose = (
            pol.proposal is None and streak >= PROMOTE_AFTER and pol.level < len(AUTO_LEVELS) - 1
        )
        next_lv = AUTO_LEVELS[min(pol.level + 1, len(AUTO_LEVELS) - 1)]
        why = f"L{pol.level}에서 연속 {streak}건 정상 머지, 자동 머지 PR의 장애 연결 0건"
        propose_at = self.clock() if propose else None

        def step(frm, to, point, kind, text, grade=None) -> Activity:
            act = self._act(self.clock(), ctx, frm, to, point, kind, text, grade)
            s.activity.append(act)
            return act

        step("planning", "hr", "check_plan", "A2A", "② 기각 이력 질의 → 없음", "read")

        spec_text = (
            f"{spec_id} 작성 (요구사항 1, 완료 기준 1, 질문 0) → 간단 요청 규칙으로 자동 확정"
        )
        await self._execute("write_spec", ctx, spec_id, spec_text, {"request": req.id})
        s.specs.append(
            Spec(
                id=spec_id,
                title=req.title,
                request=req.id,
                status="확정",
                auto=True,
                problem=f"{DEPT_NAME[req.from_]}에서 받은 요청: {req.title}",
                goal="화면 표시를 요청대로 고친다 (동작 변경 없음)",
                paras=[
                    Para(id="R1", kind="req", text=req.title),
                    Para(id="C1", kind="done", text="바뀐 화면에서 요청한 표시가 그대로 보인다"),
                ],
                basis=[f"{req.id} ({DEPT_NAME[req.from_]})", "간단 요청 규칙 통과"],
                human_sentences=0,
            )
        )
        step("planning", "planning", "write_spec", "A2A", spec_text, "A")
        # 자동 확정도 confirm_spec 관문을 거친다 (#171 규칙 키, 값은 기획서에서 코드로 계산)
        spec_rules = spec_check(s.specs[-1])
        await self._execute(
            "confirm_spec",
            ctx,
            spec_id,
            f"{spec_id} 간단 요청 규칙으로 자동 확정",
            {**spec_rules.params, "auto": True},
        )

        await self._execute("split_tasks", ctx, task_id, f"R1 → 작업 {task_id}", {"spec": spec_id})
        s.tasks.append(
            Task(
                id=task_id, spec=spec_id, para="R1", title=req.title, owner="에이전트", state="완료"
            )
        )
        step("planning", "planning", "split_tasks", "A2A", f"R1 → 작업 {task_id}", "A")

        gen_text = f"{pr_id} 변경 {lines}줄·{files}파일·{limit['paths']} → 코드 규칙 통과"
        auto_paths = changed_paths_for(spec_id, "R1", limit["paths"])
        await self._execute(
            "generate_pr",
            ctx,
            pr_id,
            gen_text,
            _pr_size_params(lines, files, auto_paths, task_id),
        )
        pr = PR(
            id=pr_id,
            number=number,
            task=task_id,
            title=f"R1: {req.title}",
            branch=f"gen/{task_id.lower()}",
            reviews=["통과"],
            gate=PrGate(result="통과", lines=lines, files=files, paths=limit["paths"]),
            merged=False,
            changed_paths=auto_paths,
            auto=True,
        )
        s.prs.append(pr)
        step("planning", "safety", "execute(generate_pr)", "execute", gen_text, "B")
        step(
            "planning",
            "shop",
            "github_create_pull",
            "API",
            f"{pr_id} 생성 ({pr.branch}, 라벨 generated-pr·auto-simple)",
        )

        review_text = f"{pr_id} 검수 통과 (완료 기준 C1), CI 통과"
        await self._execute("review_pr", ctx, f"{pr_id}:r1", review_text)
        step("planning", "planning", "review_pr", "A2A", review_text, "A")

        merge_text = f"{pr_id} 간단 요청 규칙·검수·CI 통과 → 자동 머지 허용"
        merge_rules = pr_check(s, pr)
        try:
            await self._execute(
                "merge_pr", ctx, pr_id, merge_text, {**merge_rules.params, "auto": True}
            )
        except GateRefused as exc:
            # safety가 보류·거절하면 PR은 열어 두고 교차검증 대기로 둔다 (판단은 사람 머지 버튼)
            self._hold(pr, WAIT_CROSS_CHECK, merge_rules, exc.decision)
            for x in s.tasks:
                if x.id == task_id:
                    x.state = "머지 대기"
            for r in s.requests:
                if r.id == req.id:
                    r.spec = spec_id
                    r.state = "작업 중"
            step(
                "planning",
                req.from_,
                "get_request_status",
                "A2A",
                f"{req.id} {pr_id} 자동 머지 보류 ({exc.decision.status}) → 교차검증 대기",
                "read",
            )
            return
        step("planning", "safety", "execute(merge_pr)", "execute", merge_text, "B")
        merged_act = step(
            "planning", "shop", "github_merge_pull", "API", f"{pr_id} 자동 머지 → dev 배포"
        )
        pr.merged = True
        pr.merged_at = merged_act.at

        case_text = f"① spec_confirmed·pr_merged·request_closed · {req.id} (자동 처리)"
        await self._execute("record_case", ctx, f"{req.id}:auto_closed", case_text)
        step("planning", "hr", "record_case", "A2A", case_text, "A")
        step(
            "planning",
            req.from_,
            "get_request_status",
            "A2A",
            f'{req.id} "{req.title}" 완료 · {pr_id} 머지 (사람 승인 없이 자동 처리)',
            "read",
        )

        for r in s.requests:
            if r.id == req.id:
                r.spec = spec_id
                r.state = "완료"
        s.first_pr_minutes.append(3)

        # 자동 처리 이력을 쌓고, 연속 정상이 기준에 닿으면 승급을 제안한다 (실행은 사람 승인 뒤)
        pol.streak = streak
        pol.history.append(
            AutoRun(pr=pr_id, req=req.id, at=t, lines=lines, files=files, result="정상")
        )
        if propose and propose_at is not None:
            text = (
                f"자동 처리 범위 L{pol.level} → L{pol.level + 1}"
                f"({next_lv.lines}줄·{next_lv.files}파일)"
                f" 승급 제안 · {why} → 팀장 승인 대기"
            )
            # 승급 제안은 C: 보류(held)가 정상 결과다. 실제 승급은 사람 승인(approve_promotion) 뒤
            await self._execute(
                "auto_policy_propose",
                "auto_policy",
                f"L{pol.level}->L{pol.level + 1}:{pr_id}",
                text,
                {"to": pol.level + 1},
                allow=("executed", "held"),
            )
            pol.proposal = AutoProposal(to=pol.level + 1, at=t, why=why)
            pol.log.append(
                AutoLog(at=t, text=f"L{pol.level} → L{pol.level + 1} 승급 제안 ({why})", by="자동")
            )
            s.activity.append(
                self._act(
                    propose_at,
                    ctx,
                    "planning",
                    "safety",
                    "auto_policy.propose",
                    "execute",
                    text,
                    "C",
                )
            )

    # ---------------------------------------------------------------- 장애 연결 (link_incident)

    @staticmethod
    def _next_follow_id(s: State, n: int) -> str:
        ids = {r.id for r in s.requests}
        while f"REQ-{100 + n}" in ids:
            n += 1
        return f"REQ-{100 + n}"

    async def _link(
        self,
        s: State,
        ctx: str,
        prs: list[PR],
        *,
        title: str | None,
        follow_up_title: str | None,
        follow_from: str,
        at: datetime,
        seq: int | None,
    ) -> dict:
        """사건을 PR·문단에 연결하고, 재발 방지 요청을 등록(같은 제목이면 연결만)한다.
        자동 머지 PR이 연결되면 자동 처리 범위를 한 단계 줄인다 (줄이는 쪽은 승인 없이)."""
        pr_ids = [p.id for p in prs]
        await self._execute(
            "link_incident",
            ctx,
            ",".join(pr_ids) or "none",
            f"사건 {ctx}를 {', '.join(pr_ids) or 'PR 없음'}에 연결",
            {"pr_numbers": [p.number for p in prs]},
        )
        incident = next((i for i in s.incidents if i.context_id == ctx), None)
        if incident is None:
            incident = Incident(
                context_id=ctx,
                tenant_id=self.tenant_id,
                title=title or f"사건 {ctx}",
                opened_at=at,
            )
            s.incidents.insert(0, incident)
        labels: list[str] = []
        linked_paras: list[str] = []
        for pr in prs:
            spec_id, para_id = para_of(s, pr)
            if not any(c.pr == pr.id for c in incident.candidates):
                incident.candidates.append(
                    IncidentCandidate(
                        pr=pr.id,
                        spec=spec_id or "-",
                        para=para_id or "-",
                        merged_at=pr.merged_at or at,
                    )
                )
            labels.append(f"{spec_id or '-'} {para_id or '-'}")
            spec = next((x for x in s.specs if x.id == spec_id), None)
            para = next((p for p in spec.paras if p.id == para_id), None) if spec else None
            if para:
                para.incident = ctx
                linked_paras.append(f"{spec_id}:{para_id}")
        incident.linked = True

        follow_id: str | None = None
        duplicate = False
        if follow_up_title:
            dup = find_duplicate(s.requests, follow_up_title)
            if dup:
                follow_id, duplicate = dup, True
            else:
                follow_id = self._next_follow_id(s, len(s.incidents))
                await self._execute(
                    "create_ticket",
                    ctx,
                    follow_id,
                    f"재발 방지 요청 {follow_id} 등록: {follow_up_title}",
                    {"title": follow_up_title, "via": "link_incident"},
                )
                s.requests.append(
                    Request(
                        id=follow_id,
                        context_id=ctx,
                        tenant_id=self.tenant_id,
                        from_=follow_from,
                        via="link_incident (재발 방지)",
                        title=follow_up_title,
                        evidence=f"사건 {ctx}",
                        state="접수",
                    )
                )
            incident.follow_up = follow_id

        demote_acts: list[Activity] = []
        auto_prs = [p for p in prs if p.auto]
        if auto_prs:
            pol: AutoPolicy = s.auto_policy
            label = ", ".join(p.id for p in auto_prs)
            ids = {p.id for p in auto_prs}
            if pol.level > 0:
                lv = pol.level
                text = (
                    f"자동 머지 {label}가 장애에 연결 → 자동 처리 범위 L{lv} → L{lv - 1} 축소"
                    " (줄이는 쪽은 승인 없이)"
                )
                await self._execute(
                    "auto_policy_update", ctx, f"L{lv}->L{lv - 1}", text, {"to": lv - 1}
                )
                pol.level = lv - 1
                pol.log.append(
                    AutoLog(
                        at=at,
                        text=(
                            f"{label}(자동 머지)가 사건 {ctx}에 연결 → L{lv} → L{lv - 1} 자동 축소"
                        ),
                        by="자동",
                    )
                )
                demote_acts.append(
                    self._act(
                        at,
                        ctx,
                        "planning",
                        "safety",
                        "auto_policy.updated",
                        "이벤트",
                        text,
                        None,
                        seq,
                    )
                )
            pol.streak = 0
            pol.proposal = None
            for h in pol.history:
                if h.pr in ids:
                    h.result = "장애 연결"

        where = ", ".join(labels)
        if not where:
            # 연결할 PR이 없으면 문단에 장애를 표시하지 않는다 (재발 방지 요청만 알린다)
            reply = "연결할 PR 없음 · 장애 표시 안 함" + (
                f", 재발 방지 요청 {follow_id}" if follow_id else ""
            )
        elif follow_id and duplicate:
            reply = f"{where}에 장애 표시, 같은 요청 {follow_id}에 연결 (linked_duplicate)"
        elif follow_id:
            reply = f"{where}에 장애 표시, 재발 방지 요청 {follow_id} 등록"
        else:
            reply = f"{where}에 장애 표시"
        return {
            "linked_paras": linked_paras,
            "follow_id": follow_id,
            "duplicate": duplicate,
            "demote_acts": demote_acts,
            "reply": reply,
        }

    # ------------------------------------------------------------ A2A skill (handlers.py가 부른다)

    async def find_related_prs(
        self,
        *,
        context_id: str,
        requester: str,
        window_from: datetime,
        window_to: datetime,
        component: str | None,
    ) -> list[tuple[PR, str | None, str | None]]:
        """구간 안 머지 PR 후보 (read). 원인은 판정하지 않는다. 활동 로그만 남긴다."""
        found = [
            (p, *para_of(self.state, p))
            for p in merged_in_window(self.state, window_from, window_to, component)
        ]

        async def log(s: State) -> None:
            t = self.clock()
            comp = f", 경로 {component}" if component else ""
            ask = f"구간 {_fmt(window_from)}~{_fmt(window_to)} 머지 PR 질의{comp}"
            ids = ", ".join(p.id for p, _, _ in found) or "후보 없음"
            s.activity.append(
                self._act(
                    t, context_id, requester, "planning", "find_related_prs", "A2A", ask, "read"
                )
            )
            s.activity.append(
                self._act(
                    t,
                    context_id,
                    "planning",
                    requester,
                    "find_related_prs",
                    "A2A",
                    f"관련 PR 후보 회신: {ids} · 원인 판정은 하지 않음",
                    "read",
                )
            )

        await self._apply(log)
        return found

    def rollback_record(
        self, context_id: str, service: str, version: str
    ) -> tuple[PR | None, Incident | None]:
        """롤백 검증에 쓸 planning 자기 기록: (그 버전으로 나간 머지 PR, 요청 사건)."""
        return deploy_of(self.state, service, version), incident_of(self.state, context_id)

    async def verify_rollback_target(
        self,
        *,
        context_id: str,
        requester: str,
        service: str,
        target_version: str,
        deployed_at: datetime,
        incident_opened_at: datetime,
    ) -> tuple[bool, str, list[tuple[PR, str | None, str | None]]]:
        """infra rollback_deployment(B)의 교차검증 (read)."""
        approved, reason, prs = verify_rollback(self.state, deployed_at, incident_opened_at)
        related = [(p, *para_of(self.state, p)) for p in prs]

        async def log(s: State) -> None:
            t = self.clock()
            s.activity.append(
                self._act(
                    t,
                    context_id,
                    requester,
                    "planning",
                    "verify_rollback_target",
                    "A2A",
                    f"{service} {target_version} rollback_deployment(B) 교차검증 요청",
                    "read",
                )
            )
            if approved:
                reply = f"승인 · 배포 직전 머지 {', '.join(p.id for p in prs)} 확인 ({reason})"
            else:
                reply = f"거절 · {reason} → completed + rejected_by_review"
            s.activity.append(
                self._act(
                    t,
                    context_id,
                    "planning",
                    requester,
                    "verify_rollback_target",
                    "A2A",
                    reply,
                    "read",
                )
            )

        await self._apply(log)
        return approved, reason, related

    async def list_in_progress(
        self, *, context_id: str, requester: str, owner_id: str, area: str | None
    ) -> tuple[list[Item], datetime]:
        """담당자·영역의 진행 중 항목과 조회 시각 (read). 활동 로그만 남긴다."""
        items = in_progress(self.state, owner_id, area)
        t = self.clock()

        async def log(s: State) -> None:
            where = owner_id + (f" · {area}" if area else "")
            ids = ", ".join(i.item_id for i in items) or "없음"
            s.activity.append(
                self._act(
                    t,
                    context_id,
                    requester,
                    "planning",
                    "list_in_progress",
                    "A2A",
                    f"진행 중 항목 조회 ({where})",
                    "read",
                )
            )
            s.activity.append(
                self._act(
                    t,
                    context_id,
                    "planning",
                    requester,
                    "list_in_progress",
                    "A2A",
                    f"진행 중 항목 {len(items)}건 회신: {ids}",
                    "read",
                )
            )

        await self._apply(log)
        return items, t

    async def verify_handover(
        self,
        *,
        context_id: str,
        requester: str,
        handover_id: str,
        owner_id: str,
        area: str | None,
        handover_item_ids: list[str],
    ) -> tuple[HandoverCheck, datetime]:
        """hr confirm_handover(B)의 교차검증과 확인 시각 (read). 빠진 진행 중 항목만 본다."""
        check = check_handover(self.state, owner_id, area, handover_item_ids)
        t = self.clock()

        async def log(s: State) -> None:
            s.activity.append(
                self._act(
                    t,
                    context_id,
                    requester,
                    "planning",
                    "verify_handover",
                    "A2A",
                    f"인수인계 {handover_id} confirm_handover(B) 교차검증 요청",
                    "read",
                )
            )
            if check.missing:
                ids = ", ".join(i.item_id for i in check.missing)
                reply = f"거절 · 인계 목록에 빠진 진행 중 항목 {ids} (missing_items)"
            else:
                n = len(check.current)
                reply = f"승인 · 진행 중 항목 {n}건 모두 인계 목록에 있음 (complete)"
            if check.stale_item_ids:
                reply += f" · 이미 끝난 항목 {', '.join(check.stale_item_ids)}"
            s.activity.append(
                self._act(
                    t, context_id, "planning", requester, "verify_handover", "A2A", reply, "read"
                )
            )

        await self._apply(log)
        return check, t

    async def create_ticket(
        self,
        *,
        context_id: str,
        requester: str,
        title: str,
        evidence: str,
        source_ref: str | None,
        evidence_ids: list[str],
        idempotency_key: str,
    ) -> ActionResult:
        """변경 요청 접수 (A, #174). 같은 요청이 있으면 묶고(linked_duplicate), 없으면 새 REQ와
        `ctx_req_`를 발급한다(created). 같은 idempotency_key의 두 번째 요청은 이전 결과를 돌려준다.

        - 중복 판정은 기존 도메인 규칙(`find_duplicate`, 같은 제목)만 쓴다. `source_ref`·
          `evidence_ids`는 저장만 한다.
        - 묶이면 원래 요청의 `context_id`를 돌려준다 (요청 부서는 그 사건에 문의를 잇는다).
        - 결과 통지는 같은 `context_id`에 `path`를 비운 새 요청으로 보낸다(합의). 요청 부서의 수신
          skill이 아직 없어 지금은 활동 기록(get_request_status)까지만 남긴다.
        - 요청을 보낸 A2A `context_id`는 활동 근거로만 남기고 사건 ID로 쓰지 않는다.
        """
        cached = self._idempotent.get(idempotency_key)
        if cached is not None:
            return ActionResult(changed=False, outcome=cached.outcome, output=dict(cached.output))

        async def run(s: State) -> ActionResult:
            seq = s.seq + 1
            s.seq = seq
            t = self.clock()
            dup_id = find_duplicate(s.requests, title)
            original = next((r for r in s.requests if r.id == dup_id), None) if dup_id else None
            used_ctx = {r.context_id for r in s.requests}
            n = 20 + seq
            while f"ctx_req_{n:04d}" in used_ctx:
                n += 1
            ctx_req = original.context_id if original else f"ctx_req_{n:04d}"
            used_ids = {r.id for r in s.requests}
            m = 20 + seq
            while f"REQ-{m}" in used_ids:
                m += 1
            frm = requester if requester in DEPT_NAME else "orchestrator"
            result = await self._register_ticket(
                s,
                frm,
                title,
                ctx_req,
                t,
                seq,
                evidence=evidence,
                source_ref=source_ref,
                evidence_ids=evidence_ids,
                req_id=f"REQ-{m}",
            )
            req_id = result.output["request_id"]
            return ActionResult(
                outcome="linked_duplicate" if dup_id else "created",
                output={
                    "request_id": req_id,
                    "context_id": ctx_req,
                    "duplicate_of": dup_id,
                    "auto": result.output.get("auto"),
                    "via_context_id": context_id,
                },
            )

        result = await self._apply(run)
        self._idempotent[idempotency_key] = result
        return result

    async def link_incident(
        self,
        *,
        context_id: str,
        requester: str,
        incident_context_id: str,
        pr_numbers: list[int],
        follow_up_title: str | None,
        idempotency_key: str,
    ) -> ActionResult:
        """사건을 PR·문단에 연결 (A). 같은 idempotency_key의 두 번째 요청은 이전 결과를 돌려준다."""
        cached = self._idempotent.get(idempotency_key)
        if cached is not None:
            return ActionResult(changed=False, outcome=cached.outcome, output=dict(cached.output))

        async def run(s: State) -> ActionResult:
            t = self.clock()
            prs: list[PR] = []
            for n in dict.fromkeys(pr_numbers):
                pr = next((p for p in s.prs if p.number == n), None)
                if pr:
                    prs.append(pr)
            ctx = incident_context_id
            ids = ", ".join(p.id for p in prs) or "PR 없음"
            s.activity.append(
                self._act(
                    t,
                    ctx,
                    requester,
                    "planning",
                    "link_incident",
                    "A2A",
                    f"사건을 {ids}에 연결 요청",
                    "A",
                )
            )
            linked = await self._link(
                s,
                ctx,
                prs,
                title=None,
                follow_up_title=follow_up_title,
                follow_from=requester if requester in DEPT_NAME else "infra",
                at=t,
                seq=None,
            )
            s.activity.append(
                self._act(
                    t, ctx, "planning", requester, "link_incident", "A2A", linked["reply"], "A"
                )
            )
            s.activity.extend(linked["demote_acts"])
            missing = [n for n in pr_numbers if not any(p.number == n for p in prs)]
            return ActionResult(
                outcome="linked_duplicate" if linked["duplicate"] else None,
                output={
                    "linked_paras": linked["linked_paras"],
                    "follow_up_request_id": linked["follow_id"],
                    "linked_prs": [p.id for p in prs],
                    "missing_pr_numbers": missing,
                },
            )

        result = await self._apply(run)
        self._idempotent[idempotency_key] = result
        return result

    # ---------------------------------------------------------------- 사람·에이전트 액션

    async def _send(self, s: State, to: str, point: str, text: str, ctx: str) -> None:
        t = self.clock()
        await self._execute("send_message", ctx, f"{to}:{point}:{len(s.activity)}", text)
        s.seq += 1
        s.activity.append(self._act(t, ctx, "planning", to, point, "A2A", text))

    async def _mark_read(self, s: State, to: str) -> None:
        for m in s.outbox:
            if m.to == to:
                m.read = True

    async def _answer_question(self, s: State, spec: str, text: str) -> None:
        sp = next((x for x in s.specs if x.id == spec), None)
        if sp is None or not sp.question:
            return
        dec_id = f"D-{len(s.decisions)}"
        req = self._request_of_spec(s, sp)
        ctx = req.context_id if req else f"spec:{sp.id}"
        th = next((x for x in s.threads if x.on == spec), None)
        summary = f"스레드에서 결정 {dec_id} 추출, R1 반영, 질문 닫음"
        await self._execute("extract_decision", ctx, dec_id, summary, {"spec": spec})
        case = f"① kind decision_extracted · {dec_id} (결정·이유·대안)"
        await self._execute("record_case", ctx, f"{dec_id}:decision_extracted", case)
        for x in s.threads:
            if x.on == spec:
                x.messages.append(
                    Message(who=ANSWER_WHO, dept="support", text=text, decision=dec_id)
                )
        sp.question = None
        for p in sp.paras:
            if p.id == "R1":
                p.text = ANSWER_R1_TEXT
        s.decisions.append(
            Decision(
                id=dec_id,
                spec=spec,
                from_=f"스레드 {th.id}" if th else "스레드",
                sent_to_hr=True,
                **ANSWER_DECISION,
            )
        )
        s.activity.append(
            self._act(
                self.clock(), ctx, "planning", "planning", "extract_decision", "A2A", summary, "A"
            )
        )
        s.activity.append(
            self._act(self.clock(), ctx, "planning", "hr", "record_case", "A2A", case, "A")
        )
        # 질문이 닫혔으면 자동 확정 규칙을 다시 본다 (통과하면 확정·작업 분할, 아니면 대기열)
        await self._try_auto_confirm(s, sp)

    async def _confirm_spec(self, s: State, spec: str) -> None:
        """사람 확정 버튼 (교차검증 대기·답 대기 건의 판단도 여기서 한다)."""
        sp = next((x for x in s.specs if x.id == spec), None)
        if sp is None or sp.status == "확정":
            return
        await self._do_confirm(s, sp, spec_check(sp), auto=False)

    async def _check_spec(self, s: State, spec: str) -> ActionResult | None:
        """자동 확정 규칙을 다시 본다 (예: 인사 경고가 풀린 뒤). 통과하면 확정, 아니면 대기열."""
        sp = next((x for x in s.specs if x.id == spec), None)
        if sp is None or sp.status == "확정":
            return None
        confirmed = await self._try_auto_confirm(s, sp)
        review = sp.auto_review
        return ActionResult(
            outcome="auto_confirmed" if confirmed else "waiting",
            output={"spec": sp.id, "state": None if confirmed else review and review.state},
        )

    async def _try_auto_confirm(self, s: State, sp: Spec) -> bool:
        """질문 0·경고 0·근거 1 이상·되돌릴 수 없음 0이면 자동 확정하고 작업 분할로 간다.

        아니면 확정하지 않고 "답 대기"(질문 남음) 또는 "교차검증 대기"로 둔다.
        safety가 confirm_spec을 보류·거절해도 "교차검증 대기" (앞선 단계는 그대로 남는다)."""
        if sp.status == "확정":
            return False
        req = self._request_of_spec(s, sp)
        ctx = req.context_id if req else f"spec:{sp.id}"
        check = spec_check(sp)
        if check.ok:
            try:
                await self._do_confirm(s, sp, check, auto=True)
                return True
            except GateRefused as exc:
                if exc.action != "confirm_spec":
                    raise
                hold = self._hold(sp, WAIT_CROSS_CHECK, check, exc.decision)
        else:
            hold = self._hold(sp, spec_wait_state(check), check)
        why = ", ".join(hold.rules) or f"safety {hold.gate_status}"
        s.activity.append(
            self._act(
                hold.at,
                ctx,
                "planning",
                "planning",
                "auto_review",
                "execute",
                f"{sp.id} 자동 확정 안 함 → {hold.state} ({why})",
                "A",
            )
        )
        return False

    async def _do_confirm(self, s: State, sp: Spec, check: Check, *, auto: bool) -> None:
        req = self._request_of_spec(s, sp)
        ctx = req.context_id if req else f"spec:{sp.id}"
        reqs = [p for p in sp.paras if p.kind == "req"]
        base = len(s.tasks) + 10
        tasks = [
            Task(
                id=f"T-{base + i}",
                spec=sp.id,
                para=p.id,
                title=p.text.split(" (")[0],
                owner="백엔드" if i == 0 else "프론트",
                state="진행 중",
            )
            for i, p in enumerate(reqs)
        ]
        # FR-SPEC-006: 인사 경고(warn)가 남은 문단을 둔 채 확정하면 "warn 무시 진행"을 감사 기록으로
        # 남긴다 (execute params·record_case·활동 세 곳). 경고 수는 #171 `warnings`로도 보낸다.
        warn_ignored = [
            {"para": p.id, "card": p.warn.card, "text": p.warn.text} for p in sp.paras if p.warn
        ]
        audit: str | None = None
        if warn_ignored:
            where = ", ".join(f"{w['para']}({w['card']})" for w in warn_ignored)
            audit = f"warn 무시 진행 · {sp.id} {where} 경고를 둔 채 확정 (팀장 {TEAM_LEAD})"
        c = check.params
        rule_text = (
            f"질문 {c['open_questions']}·경고 {c['warnings']}·근거 {c['basis_count']}"
            f"·되돌릴 수 없음 {c['irreversible_count']}"
        )
        if auto:
            confirm_text = f"{sp.id} 자동 확정 ({rule_text} → confirm_spec 규칙 통과, 사후 감사)"
        else:
            confirm_text = f"{sp.id} 확정 (팀장 {TEAM_LEAD})" + (f" · {audit}" if audit else "")
        params: dict[str, Any] = {**check.params, "auto": auto}
        if warn_ignored:
            params["warn_ignored"] = warn_ignored
        await self._execute("confirm_spec", ctx, sp.id, confirm_text, params)
        split = f"요구사항 {len(reqs)}개 → 작업 {', '.join(t.id for t in tasks)}"
        await self._execute("split_tasks", ctx, sp.id, split, {"tasks": [t.id for t in tasks]})
        case = f"① kind spec_confirmed · {sp.id}" + (" (자동 확정)" if auto else "")
        if auto:
            summary = f"{sp.id} {sp.title} 자동 확정 (사후 감사 대상)"
        else:
            summary = f"{sp.id} {sp.title} 확정" + (f" ({audit})" if audit else "")
        record = RecordCase(
            context_id=ctx,
            tenant_id=self.tenant_id,
            kind=RecordKind.SPEC_CONFIRMED,
            summary=summary,
            decision_reason=f"confirm_spec 규칙 통과: {rule_text}" if auto else None,
            source_refs=[f"spec:{sp.id}"]
            + [f"para:{sp.id}:{w['para']}" for w in warn_ignored]
            + [f"card:{w['card']}" for w in warn_ignored],
            author=AGENT_NAME,
        )
        case_params: dict[str, Any] = {"record_case": record.model_dump(mode="json")}
        if warn_ignored:
            case_params["warn_ignored"] = warn_ignored
        await self._execute(
            "record_case", ctx, f"{sp.id}:spec_confirmed", case, case_params, record.source_refs
        )
        sp.status = "확정"
        sp.auto_review = None
        if auto:
            sp.auto = True
        s.tasks.extend(tasks)
        for r in s.requests:
            if r.spec == sp.id:
                r.state = "작업 중"
        if auto:
            s.activity.append(
                self._act(
                    self.clock(),
                    ctx,
                    "planning",
                    "planning",
                    "audit.auto_confirmed",
                    "execute",
                    confirm_text,
                    "A",
                )
            )
        else:
            s.activity.append(
                self._act(
                    self.clock(),
                    ctx,
                    "human",
                    "planning",
                    "기획서 확정",
                    "사람",
                    f"팀장 {TEAM_LEAD}이 {sp.id} 확정 (사람 클릭 1)",
                )
            )
        if audit:
            s.activity.append(
                self._act(
                    self.clock(),
                    ctx,
                    "planning",
                    "planning",
                    "audit.warn_ignored",
                    "execute",
                    audit,
                    "A",
                )
            )
        s.activity.append(
            self._act(self.clock(), ctx, "planning", "planning", "split_tasks", "A2A", split, "A")
        )
        s.activity.append(
            self._act(self.clock(), ctx, "planning", "hr", "record_case", "A2A", case, "A")
        )
        if audit:
            s.activity.append(
                self._act(
                    self.clock(),
                    ctx,
                    "planning",
                    "hr",
                    "record_case",
                    "A2A",
                    f"{case} · {audit}",
                    "A",
                )
            )
        if req:
            s.activity.append(
                self._act(
                    self.clock(),
                    ctx,
                    "planning",
                    req.from_,
                    "get_request_status",
                    "A2A",
                    f'{req.id} 상태 "작업 중"을 요청 부서가 조회',
                    "read",
                )
            )

    async def _generate_prs(self, s: State, spec: str) -> None:
        todo = [t for t in s.tasks if t.spec == spec and t.state == "진행 중"]
        if not todo:
            return
        sp = next((x for x in s.specs if x.id == spec), None)
        req = self._request_of_spec(s, sp) if sp else None
        ctx = req.context_id if req else f"spec:{spec}"
        n = len(s.prs) + 30
        reqs = [p for p in sp.paras if p.kind == "req"] if sp else []
        created: list[PR] = []
        for i, t in enumerate(todo):
            number = n + i
            review = "불일치" if i == 1 else "통과"
            paths = planned_area(i)
            lines, files = (86, 3) if i == 0 else (41, 2)
            para = next((p for p in reqs if p.id == t.para), None)
            changed = (
                planned_paths(sp, para, reqs.index(para))
                if sp and para
                else changed_paths_for(t.spec, t.para, paths)
            )
            pr = PR(
                id=f"PR #{number}",
                number=number,
                task=t.id,
                title=f"{t.para}: {t.title}",
                branch=f"gen/{t.id.lower()}",
                reviews=[review],
                gate=PrGate(result="통과", lines=lines, files=files, paths=paths),
                merged=False,
                changed_paths=changed,
            )
            gen_text = f"{pr.id} 변경 {lines}줄·{files}파일·{paths} → 코드 규칙 통과"
            await self._execute(
                "generate_pr",
                ctx,
                pr.id,
                gen_text,
                _pr_size_params(lines, files, pr.changed_paths, t.id),
            )
            review_text = f"{pr.id} 검수 {review}{' (완료 기준 C2)' if review == '불일치' else ''}"
            await self._execute("review_pr", ctx, f"{pr.id}:r1", review_text)
            s.prs.append(pr)
            created.append(pr)
            t.state = "머지 대기" if review == "통과" else "PR 검수"
            s.activity.append(
                self._act(
                    self.clock(),
                    ctx,
                    "planning",
                    "safety",
                    "execute(generate_pr)",
                    "execute",
                    gen_text,
                    "B",
                )
            )
            s.activity.append(
                self._act(
                    self.clock(),
                    ctx,
                    "planning",
                    "shop",
                    "github_create_pull",
                    "API",
                    f"{pr.id} 생성 ({pr.branch}, 라벨 generated-pr)",
                )
            )
            s.activity.append(
                self._act(
                    self.clock(), ctx, "planning", "planning", "review_pr", "A2A", review_text, "A"
                )
            )
        s.first_pr_minutes.append(52)
        # 검수 통과 PR은 merge_pr 규칙(#171)에 안 걸리면 자동 머지, 걸리면 대기열
        for pr in created:
            await self._try_auto_merge(s, pr)

    async def _review_pr(self, s: State, pr: str | int) -> None:
        p = self._find_pr(s, pr)
        if p is None:
            return
        task = next((t for t in s.tasks if t.id == p.task), None)
        ctx = self._ctx_of_pr(s, p)
        text = f"{p.id} 수정 커밋 후 재검수 통과"
        await self._execute("review_pr", ctx, f"{p.id}:r{len(p.reviews) + 1}", text)
        p.reviews.append("통과")
        if task:
            task.state = "머지 대기"
        s.activity.append(
            self._act(self.clock(), ctx, "planning", "planning", "review_pr", "A2A", text, "A")
        )
        await self._try_auto_merge(s, p)

    async def _merge_pr(self, s: State, pr: str | int) -> None:
        """사람 머지 버튼 (교차검증 대기·사람 확인 건의 판단도 여기서 한다)."""
        p = self._find_pr(s, pr)
        if p is None or p.merged:
            return
        await self._do_merge(s, p, pr_check(s, p), auto=False)

    async def _try_auto_merge(self, s: State, p: PR) -> bool:
        """마지막 검수 통과 + merge_pr 규칙에 안 걸리면 자동 머지.

        걸리면 "교차검증 대기", 되돌릴 수 없는 영역이면 "사람 확인"으로 두고 머지하지 않는다.
        safety가 merge_pr을 보류·거절해도 "교차검증 대기" (PR 생성·검수는 그대로 남는다)."""
        if p.merged or not p.reviews or p.reviews[-1] != "통과":
            return False
        check = pr_check(s, p)
        if check.ok:
            try:
                await self._do_merge(s, p, check, auto=True)
                return True
            except GateRefused as exc:
                if exc.action != "merge_pr":
                    raise
                hold = self._hold(p, WAIT_CROSS_CHECK, check, exc.decision)
        else:
            hold = self._hold(p, pr_wait_state(check), check)
        why = ", ".join(hold.rules) or f"safety {hold.gate_status}"
        s.activity.append(
            self._act(
                hold.at,
                self._ctx_of_pr(s, p),
                "planning",
                "planning",
                "auto_review",
                "execute",
                f"{p.id} 자동 머지 안 함 → {hold.state} ({why})",
                "B",
            )
        )
        return False

    def _ctx_of_pr(self, s: State, p: PR) -> str:
        task = next((t for t in s.tasks if t.id == p.task), None)
        sp = next((x for x in s.specs if task and x.id == task.spec), None)
        req = self._request_of_spec(s, sp) if sp else None
        return req.context_id if req else f"pr:{p.number}"

    async def _do_merge(self, s: State, p: PR, check: Check, *, auto: bool) -> None:
        task = next((t for t in s.tasks if t.id == p.task), None)
        spec_id = task.spec if task else None
        sp = next((x for x in s.specs if x.id == spec_id), None)
        req = self._request_of_spec(s, sp) if sp else None
        ctx = req.context_id if req else f"pr:{p.number}"
        c = check.params
        rule_text = (
            f"{c['changed_lines']}줄·{c['changed_files']}파일·검수 불일치 "
            f"{c['review_mismatch_count']}·장애 연결 {c['linked_incident_count']}"
            f"·되돌릴 수 없음 {c['irreversible_count']}"
        )
        if auto:
            merge_text = f"{p.id} 자동 머지 ({rule_text} → merge_pr 규칙 통과, 사후 감사)"
        else:
            merge_text = f"팀장 {TEAM_LEAD}이 {p.id} 머지"
        await self._execute("merge_pr", ctx, p.id, merge_text, {**check.params, "auto": auto})
        case = f"① kind pr_merged · {p.id}" + (" (자동 머지)" if auto else "")
        if auto:
            record = RecordCase(
                context_id=ctx,
                tenant_id=self.tenant_id,
                kind=RecordKind.PR_MERGED,
                summary=f"{p.id} {p.title} 자동 머지 (사후 감사 대상)",
                decision_reason=f"merge_pr 규칙 통과: {rule_text}",
                source_refs=[f"pr:{p.number}"],
                author=AGENT_NAME,
            )
            await self._execute(
                "record_case",
                ctx,
                f"{p.id}:pr_merged",
                case,
                {"record_case": record.model_dump(mode="json")},
                record.source_refs,
            )
        else:
            await self._execute("record_case", ctx, f"{p.id}:pr_merged", case)
        if task:
            task.state = "완료"
        all_done = bool(spec_id) and all(t.state == "완료" for t in s.tasks if t.spec == spec_id)
        closed = [r for r in s.requests if r.spec == spec_id] if all_done else []
        closed_ids = ", ".join(r.id for r in closed)
        if closed:
            await self._execute(
                "record_case",
                ctx,
                f"{spec_id}:request_closed",
                f"① kind request_closed · {closed_ids}",
            )
        t = self.clock()
        p.merged = True
        p.merged_at = t
        p.auto_review = None
        if auto:
            p.auto = True
            s.activity.append(
                self._act(
                    t, ctx, "planning", "safety", "execute(merge_pr)", "execute", merge_text, "B"
                )
            )
            s.activity.append(
                self._act(
                    self.clock(),
                    ctx,
                    "planning",
                    "shop",
                    "github_merge_pull",
                    "API",
                    f"{p.id} 자동 머지 → dev 배포",
                )
            )
        else:
            s.activity.append(
                self._act(
                    t,
                    ctx,
                    "human",
                    "planning",
                    "PR 머지",
                    "사람",
                    f"팀장 {TEAM_LEAD}이 {p.id} 머지 (사람 클릭)",
                )
            )
        s.activity.append(
            self._act(self.clock(), ctx, "planning", "hr", "record_case", "A2A", case, "A")
        )
        if closed:
            s.activity.append(
                self._act(
                    self.clock(),
                    ctx,
                    "planning",
                    "hr",
                    "record_case",
                    "A2A",
                    f"① kind request_closed · {closed_ids}",
                    "A",
                )
            )
            for r in closed:
                r.state = "완료"
            to = req.from_ if req else closed[0].from_
            s.activity.append(
                self._act(
                    self.clock(),
                    ctx,
                    "planning",
                    to,
                    "get_request_status",
                    "A2A",
                    f'{closed_ids} "완료"를 요청 부서에 회신',
                    "read",
                )
            )

    async def _accept_draft(self, s: State, request: str) -> None:
        r = next((x for x in s.requests if x.id == request), None)
        if r is None or r.state != "접수":
            return
        text = f"{r.id} 채택 → 기획서 작성 대기열"
        await self._execute("review_ticket_draft", r.context_id, r.id, text)
        s.drafts_accepted += 1
        r.state = "기획 중"
        s.activity.append(
            self._act(
                self.clock(),
                r.context_id,
                "planning",
                "planning",
                "review_ticket_draft",
                "A2A",
                text,
                "A",
            )
        )

    # ---- 기각 (hr record_case kind decision_rejected)

    def _rejection_record(
        self,
        ctx: str,
        summary: str,
        decision_reason: str,
        alternatives_considered: list[str],
        source_refs: list[str],
    ) -> RecordCase:
        """`decision_rejected` 기록을 만든다. 이유·검토한 대안이 없으면 보내지 않고 오류.

        hr `check_plan`이 "과거에 기각된 안"을 판정하는 재료라서 둘 다 필수로 둔다.
        """
        reason = decision_reason.strip()
        alternatives = [a.strip() for a in alternatives_considered if a.strip()]
        if not reason or not alternatives:
            raise ActionError(
                "MISSING_DECISION_REASON",
                "decision_rejected에는 decision_reason과 alternatives_considered가 필요하다"
                " (hr에 보내지 않았다)",
            )
        return RecordCase(
            context_id=ctx,
            tenant_id=self.tenant_id,
            kind=RecordKind.DECISION_REJECTED,
            summary=summary,
            decision_reason=reason,
            alternatives_considered=alternatives,
            source_refs=source_refs,
            author=AGENT_NAME,
        )

    async def _send_rejection(self, s: State, record: RecordCase, target: str) -> None:
        """hr `record_case`(kind decision_rejected)를 execute로 보내고 활동에 남긴다."""
        case = f"① kind decision_rejected · {target}"
        await self._execute(
            "record_case",
            record.context_id,
            f"{target}:decision_rejected",
            case,
            {"record_case": record.model_dump(mode="json")},
            record.source_refs,
        )
        s.activity.append(
            self._act(
                self.clock(),
                record.context_id,
                "planning",
                "hr",
                "record_case",
                "A2A",
                f"{case} (이유: {record.decision_reason})",
                "A",
            )
        )

    async def _reject_draft(
        self,
        s: State,
        request: str,
        decision_reason: str,
        alternatives_considered: list[str],
    ) -> ActionResult | None:
        """개선 초안 기각 → hr record_case(decision_rejected). 이유·대안이 없으면 하지 않는다."""
        r = next((x for x in s.requests if x.id == request), None)
        record = self._rejection_record(
            r.context_id if r else f"req:{request}",
            f"[기각한 안] {r.title if r else request}",
            decision_reason,
            alternatives_considered,
            [f"req:{request}"],
        )
        if r is None or r.state != "접수":
            return None
        reason = record.decision_reason or ""
        text = f"{r.id} 기각 (이유: {reason})"
        await self._execute(
            "review_ticket_draft", r.context_id, r.id, text, {"decision": "rejected"}
        )
        await self._send_rejection(s, record, r.id)
        dec_id = f"D-{len(s.decisions)}"
        s.decisions.append(
            Decision(
                id=dec_id,
                spec=r.spec or "-",
                text=f"[기각한 안] {r.title}",
                reason=reason,
                alternatives=", ".join(record.alternatives_considered),
                from_=f"개선 초안 {r.id}",
                sent_to_hr=True,
            )
        )
        r.state = "기각"
        s.activity.append(
            self._act(
                self.clock(),
                r.context_id,
                "planning",
                "planning",
                "review_ticket_draft",
                "A2A",
                text,
                "A",
            )
        )
        s.activity.append(
            self._act(
                self.clock(),
                r.context_id,
                "planning",
                r.from_,
                "get_request_status",
                "A2A",
                f'{r.id} "{r.title}" 기각 (이유: {reason})',
                "read",
            )
        )
        return ActionResult(
            output={"request_id": r.id, "decision_id": dec_id, "record_kind": "decision_rejected"}
        )

    async def _reject_decision(
        self,
        s: State,
        spec: str,
        text: str,
        decision_reason: str,
        alternatives_considered: list[str],
    ) -> ActionResult | None:
        """기획서 검토 중 기각한 안을 결정으로 남기고 hr에 decision_rejected로 보낸다."""
        sp = next((x for x in s.specs if x.id == spec), None)
        req = self._request_of_spec(s, sp) if sp else None
        ctx = req.context_id if req else f"spec:{spec}"
        record = self._rejection_record(
            ctx,
            f"[기각한 안] {text.strip()}",
            decision_reason,
            alternatives_considered,
            [f"spec:{spec}"],
        )
        if sp is None or not text.strip():
            return None
        dec_id = f"D-{len(s.decisions)}"
        await self._send_rejection(s, record, dec_id)
        s.decisions.append(
            Decision(
                id=dec_id,
                spec=sp.id,
                text=f"[기각한 안] {text.strip()}",
                reason=record.decision_reason or "",
                alternatives=", ".join(record.alternatives_considered),
                from_=f"기획서 {sp.id}",
                sent_to_hr=True,
            )
        )
        return ActionResult(output={"decision_id": dec_id, "record_kind": "decision_rejected"})

    async def _approve_promotion(self, s: State) -> None:
        pol = s.auto_policy
        if pol.proposal is None:
            return
        to = pol.proposal.to
        lv = AUTO_LEVELS[to]
        t = self.clock()
        text = (
            f"간단 요청 기준 {lv.lines}줄·{lv.files}파일·{pol.paths}로 갱신 요청"
            " (기준표 pr_changed_lines·files)"
        )
        await self._execute(
            "auto_policy_update",
            "auto_policy",
            f"L{pol.level}->L{to}",
            text,
            {"to": to, "lines": lv.lines, "files": lv.files, "paths": pol.paths},
        )
        s.activity.append(
            self._act(
                t,
                "auto_policy",
                "human",
                "planning",
                "자동 범위 승급 승인",
                "사람",
                f"팀장 {TEAM_LEAD}이 L{pol.level} → L{to} 승인 (사람 클릭)",
            )
        )
        s.activity.append(
            self._act(t, "auto_policy", "planning", "safety", "auto_policy.updated", "이벤트", text)
        )
        pol.log.append(
            AutoLog(
                at=t,
                text=f"L{pol.level} → L{to} 승급 승인 ({lv.lines}줄·{lv.files}파일)",
                by="사람",
            )
        )
        pol.level = to
        pol.streak = 0
        pol.proposal = None

    async def _reject_promotion(self, s: State) -> None:
        pol = s.auto_policy
        if pol.proposal is None:
            return
        t = self.clock()
        pol.log.append(
            AutoLog(at=t, text=f"L{pol.proposal.to} 승급 보류 (연속 건수 다시 셈)", by="사람")
        )
        pol.streak = 0
        pol.proposal = None

    # ---- 소통

    async def _post_message(self, s: State, thread: str, text: str) -> None:
        th = next((x for x in s.threads if x.id == thread), None)
        if th is None or not text.strip():
            return
        body = text.strip()
        t = self.clock()
        req = next((r for r in s.requests if th.on and r.spec == th.on), None)
        ctx = req.context_id if req else f"thread:{th.id}"
        if th.peer:
            await self._execute("send_message", ctx, f"{th.id}:{len(th.messages)}", body)
            s.seq += 1
            s.activity.append(self._act(t, ctx, "planning", th.peer, "message", "A2A", body))
        th.messages.append(Message(who=TEAM_LEAD, dept="planning", text=body, at=t))

    async def _dept_chat(self, s: State, from_: str) -> None:
        pool = DEPT_CHAT.get(from_)
        th = next((x for x in s.threads if x.peer == from_), None)
        if not pool or th is None:
            return
        seq = s.seq + 1
        t = self.clock()
        text = pool[sum(1 for m in th.messages if m.dept == from_) % len(pool)]
        s.seq = seq
        th.messages.append(Message(who=f"{DEPT_NAME[from_]} 에이전트", dept=from_, text=text, at=t))
        s.activity.append(
            self._act(t, f"thread:{th.id}", from_, "planning", "message", "A2A", text, None, seq)
        )

    async def _extract_decision(self, s: State, thread: str, index: int) -> None:
        th = next((x for x in s.threads if x.id == thread), None)
        if th is None or not 0 <= index < len(th.messages) or th.messages[index].decision:
            return
        m = th.messages[index]
        dec_id = f"D-{len(s.decisions)}"
        ctx = f"thread:{th.id}"
        summary = f"{th.title} 메시지에서 결정 {dec_id} 추출"
        await self._execute("extract_decision", ctx, dec_id, summary)
        case = f"① kind decision_extracted · {dec_id}"
        await self._execute("record_case", ctx, f"{dec_id}:decision_extracted", case)
        m.decision = dec_id
        s.decisions.append(
            Decision(
                id=dec_id,
                spec=th.on or "-",
                text=m.text,
                reason=f"{m.who} 의견 ({th.title})",
                alternatives="-",
                from_=f"소통 {th.title}",
                sent_to_hr=True,
            )
        )
        s.activity.append(
            self._act(
                self.clock(), ctx, "planning", "planning", "extract_decision", "A2A", summary, "A"
            )
        )
        s.activity.append(
            self._act(self.clock(), ctx, "planning", "hr", "record_case", "A2A", case, "A")
        )

    # ---- 문서 (사람이 고치는 정리 문서. 상태 변경 관문 대상 아님)

    def _patch_doc(self, s: State, doc: str, fn: Callable[[Doc], None]) -> None:
        d = next((x for x in s.docs if x.id == doc), None)
        if d is None:
            return
        fn(d)
        d.updated_at = self.clock()

    async def _new_doc(self, s: State, title: str) -> ActionResult:
        doc_id = f"DOC-{len(s.docs) + 1}"
        s.docs.append(
            Doc(
                id=doc_id,
                title=title or "제목 없는 문서",
                updated_at=self.clock(),
                blocks=[DocBlock(id="b1", kind="p", text="")],
            )
        )
        return ActionResult(output={"doc_id": doc_id})

    async def _gather_doc(self, s: State, about: str, by: str | None) -> ActionResult | None:
        r = next((x for x in s.requests if x.id == about), None)
        if r is None:
            return None
        doc_id = f"DOC-{len(s.docs) + 1}"
        blocks = gather_blocks(s, about)
        s.docs.append(
            Doc(
                id=doc_id,
                title=f"{r.id} 관련 정리 · {r.title}",
                about=r.id,
                updated_at=self.clock(),
                blocks=blocks,
                by=by or "human",
            )
        )
        ai = by == "ai"
        prefix = "AI 어시스턴트 요청으로 " if ai else ""
        s.activity.append(
            self._act(
                self.clock(),
                r.context_id,
                "planning" if ai else "human",
                "planning",
                "gather_doc",
                "A2A" if ai else "사람",
                f"{prefix}{r.id}에 걸린 문서·작업·PR·대화·결정을 {doc_id}로 정리"
                f" (블록 {len(blocks)}개)",
                "read",
            )
        )
        return ActionResult(output={"doc_id": doc_id, "blocks": len(blocks)})

    async def _rename_doc(self, s: State, doc: str, title: str) -> None:
        self._patch_doc(s, doc, lambda d: setattr(d, "title", title))

    async def _add_block(self, s: State, doc: str, kind: str, text: str | None) -> None:
        def add(d: Doc) -> None:
            d.blocks.append(
                DocBlock(
                    id=next_block_id(d),
                    kind=kind,
                    text=text or "",
                    done=False if kind == "todo" else None,
                )
            )

        self._patch_doc(s, doc, add)

    async def _edit_block(self, s: State, doc: str, block: str, text: str) -> None:
        def edit(d: Doc) -> None:
            for b in d.blocks:
                if b.id == block:
                    b.text = text

        self._patch_doc(s, doc, edit)

    async def _toggle_todo(self, s: State, doc: str, block: str) -> None:
        def toggle(d: Doc) -> None:
            for b in d.blocks:
                if b.id == block:
                    b.done = not b.done

        self._patch_doc(s, doc, toggle)

    async def _remove_block(self, s: State, doc: str, block: str) -> None:
        def remove(d: Doc) -> None:
            d.blocks = [b for b in d.blocks if b.id != block]

        self._patch_doc(s, doc, remove)


__all__ = [
    "ACTIONS",
    "INBOUND_KINDS",
    "ActionError",
    "ActionResult",
    "GateCall",
    "GateRefused",
    "PlanningService",
    "action_name",
]
