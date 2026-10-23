"""AX 개발실(개발 에이전트 진척 화면)이 읽는 `status.json`의 형식 (DevStatus).

시나리오 A e2e(`e2e-result.json`), 실서버 실행(`real-result.json`), 아침 리포트(`morning.json`)가
각자 아는 부분만 채워 같은 형식으로 남기고, 개발실 갱신 작업이 하나로 합친다.
그래서 모든 묶음 필드에 기본값이 있고 일부만 채운 값도 유효하다.
이 형식은 읽기 전용 결과물이며 상태를 바꾸지 않는다. 비밀값·고객 원문은 담지 않는다.

- 필드 이름은 snake_case. 피드의 `from`은 파이썬 예약어라 `from_`로 받고 별칭 `from`으로 내보낸다
  (`model_dump(by_alias=True)`).
- 계약은 추가만 한다(CLAUDE.md 12): 필드는 더하기만 하고 지우거나 바꾸지 않는다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from ax_contracts.schemas.common import AxModel, UtcDatetime

# 단계 상태: pass 통과 / pending 아직 없음 / broken 있는데 실패(통과 이력 없음)
# / regressed 통과하다 실패
StageState = Literal["pass", "pending", "broken", "regressed"]
OrcState = Literal["done", "doing", "todo", "problem"]
BlockedKind = Literal["review", "decision", "env", "wip"]
PersonState = Literal["done", "doing", "todo", "waiting", "problem"]
FeedKind = Literal["agent", "bot", "human"]


class StageResult(AxModel):
    """한 단계의 한 관점(단독·연결·실서버) 결과."""

    state: StageState = "pending"
    since: UtcDatetime | None = None  # 이 상태가 된 시각
    last_pass_commit: str | None = None  # 마지막으로 통과한 dev 커밋
    issue: int | None = None  # 이 단계 때문에 열린 이슈 번호
    note: str | None = None


class Stage(AxModel):
    no: int
    name: str
    dept: str
    req_ids: list[str] = Field(default_factory=list)
    unit: StageResult = Field(default_factory=StageResult)  # 단독: 그 부서만 고정 입력으로 호출
    chain: StageResult = Field(default_factory=StageResult)  # 연결: 앞 단계의 A2A 호출로 이어짐
    real: StageResult | None = None  # 실서버. 아직 없으면 None


class Totals(AxModel):
    unit_pass: int = 0
    chain_pass: int = 0
    real_pass: int = 0
    total: int = 15
    real_runs_consecutive: int = 0


class HistoryPoint(AxModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    chain_pass: int
    real_pass: int


class OrcItem(AxModel):
    id: str  # 예: ORC-01
    title: str
    state: OrcState
    note: str | None = None
    issue: int | None = None


class Cap(AxModel):
    login: str
    name: str
    write_used: int
    write_cap: int
    depts: list[str] = Field(default_factory=list)


class WipItem(AxModel):
    dept: str
    open_bot_prs: int
    limit: int


class Blocked(AxModel):
    kind: BlockedKind
    title: str
    detail: str | None = None
    url: str | None = None
    since: UtcDatetime | None = None


class Bottlenecks(AxModel):
    caps: list[Cap] = Field(default_factory=list)
    wip: list[WipItem] = Field(default_factory=list)
    blocked: list[Blocked] = Field(default_factory=list)


class Person(AxModel):
    who: str  # 팀원 이름 또는 "모두"
    task: str
    detail: str | None = None
    state: PersonState
    url: str | None = None


class FeedItem(AxModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    at: UtcDatetime
    kind: FeedKind
    from_: str = Field(alias="from")
    to: str | None = None
    text: str
    url: str | None = None


class MergeRate(AxModel):
    overall: float | None = None
    by_dept: dict[str, float] = Field(default_factory=dict)


class Signals(AxModel):
    reverts_yesterday: int = 0
    needs_human_open: int = 0
    merge_rate: MergeRate = Field(default_factory=MergeRate)
    human_interventions_yesterday: int = 0


class ForecastPoint(AxModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    chain_pass: int
    real_pass: int


class DevStatus(AxModel):
    """개발실 `status.json` 전체. 일부만 채운 부분 결과도 유효하다."""

    generated_at: UtcDatetime
    scenario: Literal["SCN-A"] = "SCN-A"
    stages: list[Stage] = Field(default_factory=list)  # e2e/scenario_a/stages.py 순서
    totals: Totals = Field(default_factory=Totals)
    history: list[HistoryPoint] = Field(default_factory=list)  # 진척 추이
    forecast: list[ForecastPoint] | None = None  # 아침 리포트가 계산한 예상(선택)
    orc: list[OrcItem] = Field(default_factory=list)
    bottlenecks: Bottlenecks = Field(default_factory=Bottlenecks)
    people: list[Person] = Field(default_factory=list)
    feed: list[FeedItem] = Field(default_factory=list)  # 최근 50개
    signals: Signals = Field(default_factory=Signals)
