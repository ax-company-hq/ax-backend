"""기획개발 화면·서비스가 쓰는 상태 모델 (프로토타입 `data.ts`의 타입을 옮긴 것).

- 필드는 snake_case다. 프로토타입의 `from`은 파이썬 예약어라 `from_` 필드에 별칭 `from`을 붙였다
  (JSON에는 `from`으로 나간다). 직렬화는 항상 `by_alias=True`로 한다.
- 시각은 UTC aware datetime으로 저장한다. KST 표시는 화면이 한다.
- 상태 값(요청 상태·작업 상태 등)은 프로토타입과 같은 한국어 값을 쓴다 (화면이 그대로 쓴다).
- 이 모델은 화면용 내부 모델이다. 부서 간 계약은 `contracts/`의 모델만 쓴다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ax_contracts.schemas import TENANT_ID_DEFAULT, UtcDatetime

Dept = Literal[
    "planning",
    "hr",
    "support",
    "marketing",
    "data",
    "finance",
    "infra",
    "orchestrator",
    "safety",
    "shop",
    "memory",
    "human",
]

DEPT_NAME: dict[str, str] = {
    "planning": "기획개발",
    "hr": "인사",
    "support": "고객지원",
    "marketing": "마케팅",
    "data": "데이터분석",
    "finance": "재무/정산",
    "infra": "인프라(DejaFix)",
    "orchestrator": "통합 에이전트",
    "safety": "안전 레이어",
    "shop": "판매 사이트",
    "memory": "회사 기억",
    "human": "사람",
}

ReqState = Literal["접수", "기획 중", "작업 중", "완료", "중복 연결", "기각"]
TaskState = Literal["진행 중", "PR 검수", "머지 대기", "완료", "C 보류"]
Review = Literal["통과", "불일치"]
ActivityKind = Literal["A2A", "이벤트", "execute", "API", "사람"]
Grade = Literal["A", "B", "C", "read"]
BlockKind = Literal["h2", "p", "todo", "ref"]


class DomainModel(BaseModel):
    """도메인 모델 기반. 정의하지 않은 필드는 거부하고, 별칭·필드 이름 모두로 만들 수 있다."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class AutoCheck(DomainModel):
    """간단 요청 판정 결과. 사유는 항상 4줄(민감 경로·표시 전용·기각 이력·크기)."""

    ok: bool
    reasons: list[str]


class Request(DomainModel):
    id: str
    context_id: str
    tenant_id: str = TENANT_ID_DEFAULT
    from_: Dept = Field(alias="from")
    via: str  # 들어온 연결 지점
    title: str
    evidence: str
    state: ReqState
    spec: str | None = None
    duplicate_of: str | None = None
    auto: AutoCheck | None = None  # 간단 요청 판정 결과 (create_ticket으로 들어온 것만)


class ParaWarn(DomainModel):
    card: str
    text: str


class Para(DomainModel):
    id: str  # R1, C1 …
    kind: Literal["req", "done"]
    text: str
    warn: ParaWarn | None = None  # 인사 ② check_plan 결과
    incident: str | None = None  # 장애 표시 (link_incident)


class Spec(DomainModel):
    id: str
    title: str
    request: str
    status: Literal["초안", "확정"]
    problem: str
    goal: str
    paras: list[Para]
    question: str | None = None
    basis: list[str] = Field(default_factory=list)
    human_sentences: int = 0
    auto: bool = False  # 간단 요청 규칙으로 사람 확정 없이 처리됨


class Task(DomainModel):
    id: str
    spec: str
    para: str
    title: str
    owner: str
    state: TaskState


class PrGate(DomainModel):
    """안전 레이어 코드 규칙 결과 (pr_changed_lines·files·allowed_paths)."""

    result: Literal["통과", "C 보류"]
    lines: int
    files: int
    paths: str


class PR(DomainModel):
    id: str  # 표시용 "PR #28"
    number: int  # 28 (계약 RelatedPr.pr_number)
    task: str
    title: str
    branch: str
    reviews: list[Review]
    gate: PrGate
    merged: bool
    merged_at: UtcDatetime | None = None
    changed_paths: list[str] = Field(default_factory=list)
    auto: bool = False  # 간단 요청 규칙으로 자동 머지됨


class Message(DomainModel):
    who: str
    dept: Dept
    text: str
    decision: str | None = None
    at: UtcDatetime | None = None


class Thread(DomainModel):
    """소통 채널. spec = 문서에 붙는 스레드, team = 기획개발팀 채널, dept = 다른 부서 채널."""

    id: str
    on: str
    title: str
    messages: list[Message] = Field(default_factory=list)
    kind: Literal["spec", "team", "dept"] | None = None
    peer: Dept | None = None


class DocBlock(DomainModel):
    id: str
    kind: BlockKind
    text: str
    done: bool | None = None


class Doc(DomainModel):
    """정리 문서: 요청·기획서·작업·PR·대화·결정을 한 장으로 모으고 사람이 고친다."""

    id: str
    title: str
    about: str | None = None
    updated_at: UtcDatetime
    blocks: list[DocBlock] = Field(default_factory=list)
    by: Literal["ai", "human"] | None = None


class Decision(DomainModel):
    id: str
    spec: str
    text: str
    reason: str
    alternatives: str
    from_: str = Field(alias="from")
    sent_to_hr: bool


class IncidentCandidate(DomainModel):
    pr: str
    spec: str
    para: str
    merged_at: UtcDatetime


class RollbackCheck(DomainModel):
    target: str
    result: Literal["승인", "거절"]
    why: str


class Incident(DomainModel):
    context_id: str
    tenant_id: str = TENANT_ID_DEFAULT
    title: str
    opened_at: UtcDatetime
    candidates: list[IncidentCandidate] = Field(default_factory=list)
    rollback_check: RollbackCheck | None = None
    linked: bool = False
    follow_up: str | None = None


class Activity(DomainModel):
    seq: int | None = None  # 시뮬레이터로 들어온 건의 순번 (새로 받은 것 표시용)
    at: UtcDatetime
    context_id: str
    tenant_id: str = TENANT_ID_DEFAULT
    from_: Dept = Field(alias="from")
    to: Dept
    point: str  # 연결 지점
    kind: ActivityKind
    grade: Grade | None = None
    text: str


class OutMsg(DomainModel):
    """기획개발 → 다른 부서로 나간 메시지 (다른 부서 화면의 "기획개발에서 받은 것")."""

    id: int
    at: UtcDatetime
    context_id: str
    tenant_id: str = TENANT_ID_DEFAULT
    to: Dept
    point: str
    text: str
    read: bool = False


class AutoLevel(DomainModel):
    level: int
    name: str
    lines: int
    files: int


class AutoRun(DomainModel):
    pr: str
    req: str
    at: UtcDatetime
    lines: int
    files: int
    result: Literal["정상", "장애 연결"]


class AutoProposal(DomainModel):
    to: int
    at: UtcDatetime
    why: str


class AutoLog(DomainModel):
    at: UtcDatetime
    text: str
    by: Literal["사람", "자동"]


class AutoPolicy(DomainModel):
    """자동 처리 범위. 넓히기는 사람 승인, 줄이기는 자동."""

    level: int
    paths: str
    streak: int = 0  # 마지막 단계 변경 뒤 연속 정상 건수
    history: list[AutoRun] = Field(default_factory=list)
    proposal: AutoProposal | None = None
    log: list[AutoLog] = Field(default_factory=list)


class State(DomainModel):
    tenant_id: str = TENANT_ID_DEFAULT
    seq: int = 0  # 들어오고 나간 연동 건마다 1씩 (새로 받은 것 표시용)
    outbox: list[OutMsg] = Field(default_factory=list)
    requests: list[Request] = Field(default_factory=list)
    specs: list[Spec] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    prs: list[PR] = Field(default_factory=list)
    threads: list[Thread] = Field(default_factory=list)
    docs: list[Doc] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    incidents: list[Incident] = Field(default_factory=list)
    activity: list[Activity] = Field(default_factory=list)
    # 지표 계산용 원자료 (코드로 계산)
    drafts_in: int = 0
    drafts_accepted: int = 0
    warnings: int = 0
    first_pr_minutes: list[int] = Field(default_factory=list)
    auto_policy: AutoPolicy


def dump(model: BaseModel) -> dict:
    """화면·SSE로 내보내는 JSON 형태 (`from` 별칭, ISO8601 UTC)."""
    return model.model_dump(mode="json", by_alias=True)
