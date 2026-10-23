"""시나리오 A 단계 표 (A-3 정의, .autopilot/a3-chain.md).

- 총괄 경로 12단계는 `orchestrator/.../plans/scenario_a.py`의 plan을 읽어 만든다.
  순서·skill·선행·필수는 plan이 정하고, 담당 부서는 부서 Agent Card에서 skill을 찾아 정한다
  (카드에 없으면 총괄 목업이 그 skill을 맡긴 부서).
  요구사항 ID와 통과 조건 문구만 `STEP_META`가 단계 id별로 덧붙인다(없는 단계는 비워 둔다).
- 고객 경로 5단계는 plan이 없어 `CUSTOMER_STAGES`로 정의한다 (#228 결정 A: 총괄이 부르지 않음).
- contextId 결정(#258 9번) 전까지 두 경로는 따로 점수를 낸다: "연결(사건) n/12" + "고객 경로 n/5".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ax_e2e.scenario_a import paths

INCIDENT, CUSTOMER = "incident", "customer"

# plan 단계 id → (요구사항 ID, 통과 조건). plan에 단계가 늘면 메타 없이도 표에는 들어간다.
STEP_META: dict[str, tuple[list[str], str]] = {
    "diagnose": (["DJF-01"], "원인 진단 결과(suspects)가 completed로 돌아온다"),
    "impact": (["DEP-DATA-02"], "실패 건수·영향 고객 수·피해 금액이 completed로 돌아온다"),
    "rollback": (["DJF-04", "SAF-04"], "B 등급 롤백이 planning 검증을 거쳐 completed가 된다"),
    "related_prs": (["DEP-DEV-01"], "장애 시각 전후 PR 후보가 completed로 돌아온다"),
    "link_incident": (["DEP-DEV-02"], "사건에 PR이 연결되어 completed가 된다"),
    "notice": (["DEP-MKT-01"], "점검 공지 초안(draft_id)이 completed로 돌아온다"),
    "publish_notice": (["DEP-MKT-01"], "점검 공지가 게시되어(notice_id) completed가 된다"),
    "loss": (["DEP-FIN-01"], "손실 리포트가 completed로 돌아온다"),
    "recovery": (["DJF-03"], "복구 검증 결과가 completed로 돌아온다"),
    "recovered_draft": (["DEP-MKT-01"], "정상화 공지 초안이 completed로 돌아온다"),
    "recovered_notice": (["DEP-MKT-01"], "정상화 공지가 게시되어 completed가 된다"),
    "record": (["DEP-HR-01"], "사건 기록(card_id)이 completed로 남는다"),
}


@dataclass
class Stage:
    no: int
    path: str  # incident | customer
    key: str  # 경로 안의 단계 id (plan step id 또는 고객 단계 id)
    name: str
    dept: str
    req_ids: list[str]
    condition: str
    skill: str | None = None
    after: tuple[str, ...] = ()
    required: bool = True
    requester: str = "orchestrator"  # 단독 호출 때의 요청자
    note: str = field(default="")

    @property
    def label(self) -> str:
        return f"[{'총괄' if self.path == INCIDENT else '고객'}] {self.name}"


_LABEL_DEPT = re.compile(r"\(([a-z-]+)\)\s*$")


def _planned_dept(skill: str) -> str | None:
    """카드에 아직 없는 skill의 예정 담당: 총괄 목업(가짜 부서)이 그 skill을 맡긴 부서."""
    from ax_orchestrator.mock.fake_departments import CARDS

    return next((d for d, skills in CARDS.items() if any(k == skill for k, _, _ in skills)), None)


def _plan_stages() -> list[Stage]:
    from ax_orchestrator.plans.scenario_a import scenario_a

    out: list[Stage] = []
    for i, step in enumerate(scenario_a().steps, start=1):
        label_dept = _LABEL_DEPT.search(step.label)
        dept = paths.dept_of_skill(step.skill) if step.skill else None
        dept = (
            dept or _planned_dept(step.skill) or (label_dept.group(1) if label_dept else "unknown")
        )
        reqs, cond = STEP_META.get(step.id, ([], "completed로 돌아온다"))
        out.append(
            Stage(
                no=i,
                path=INCIDENT,
                key=step.id,
                name=f"{step.id} ({step.skill})",
                dept=dept,
                req_ids=list(reqs),
                condition=cond,
                skill=step.skill,
                after=(*step.after, *step.also_after),
                required=step.required,
            )
        )
    return out


# 고객 경로 5단계 (a3-chain.md). 호출 방법은 customer.py가 가진다.
CUSTOMER_STAGES: list[dict] = [
    {
        "key": "inquiry",
        "name": "장애 중 고객 문의 접수 (create_inquiry)",
        "dept": "support",
        "skill": "create_inquiry",
        "requester": "shop-api",
        "req_ids": ["DEP-CS-01"],
        "condition": "문의가 접수되어 completed로 돌아온다",
    },
    {
        "key": "judge",
        "name": "보상 판정 (request_compensation)",
        "dept": "support",
        "skill": "request_compensation",
        "requester": "dashboard-api",
        "req_ids": ["DEP-CS-02"],
        "condition": "단가표로 보상 대상(eligible)이 판정된다",
    },
    {
        "key": "issue_coupon",
        "name": "쿠폰 지급 요청 (support → marketing issue_coupon)",
        "dept": "marketing",
        "skill": "issue_coupon",
        "requester": "support",
        "req_ids": ["DEP-MKT-02", "DEP-CS-02"],
        "condition": "support의 A2A 호출로 마케팅 issue_coupon이 시작되고 처리된다",
    },
    {
        "key": "finance_check",
        "name": "쿠폰 금액 교차검증 (B, finance verify_amount)",
        "dept": "finance",
        "skill": "verify_amount",
        "requester": "safety",
        "req_ids": ["DEP-FIN-02", "SAF-04"],
        "condition": "재무가 금액을 원천 데이터로 재계산해 승인·거절한다",
    },
    {
        "key": "notice_reply",
        "name": "공지 사실 읽어 응대 (support → marketing get_notice)",
        "dept": "marketing",
        "skill": "get_notice",
        "requester": "support",
        "req_ids": ["DEP-MKT-04", "DEP-CS-01"],
        "condition": "support가 마케팅 get_notice로 공지 사실을 읽는다",
    },
]


def customer_stages(start: int = 1) -> list[Stage]:
    out = []
    for i, spec in enumerate(CUSTOMER_STAGES, start=start):
        dept = spec["dept"]
        skill = spec["skill"]
        out.append(
            Stage(
                no=i,
                path=CUSTOMER,
                key=spec["key"],
                name=spec["name"],
                dept=dept,
                req_ids=list(spec["req_ids"]),
                condition=spec["condition"],
                skill=skill,
                requester=spec["requester"],
            )
        )
    return out


def build_stage_table() -> list[Stage]:
    """총괄 12단계(plan에서) + 고객 5단계. 번호는 1부터 이어진다."""
    plan = _plan_stages()
    return [*plan, *customer_stages(start=len(plan) + 1)]
