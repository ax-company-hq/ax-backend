"""인사 탭 나머지 화면(온보딩·변경·명부·감사 기록·계획 비교)의 목 모드 데이터와 동작.

형식은 모두 계약(`ax_contracts.schemas.hr`)을 쓰고, 목이 아닐 때는 `hr_client`로 hr skill을 부른다
(`routes.py`). 진행률은 hr과 같은 규칙으로 코드가 센다: 진행률 = 완료한 단계 수 / 전체 단계 수.
인수인계·후보 카드는 `mock_handover.py`에 있다.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ax_contracts.schemas.hr import (
    AuditEntry,
    ChangeView,
    ChecklistItem,
    CheckPlanComputed,
    CheckPlanOutput,
    GetRosterOutput,
    OnboardingStep,
    OnboardingView,
    PlanDraft,
    PlanFinding,
    RosterEntry,
    SimilarCard,
    StartOnboardingOutput,
)
from ax_dashboard_api.envelope import ApiError

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
SHIFT_END = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)
NEXT_END = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
REJECTED_CARD = "card_9304adb5e780"


def _progress(steps: list[OnboardingStep]) -> int:
    return int(100 * sum(s.done for s in steps) / len(steps)) if steps else 100


def seed_onboardings() -> list[OnboardingView]:
    titles = [
        ("payment 영역의 담당·교대와 진행 중인 일을 훑어본다", None, True),
        ("결정 읽기: [기각한 안] 결제 서버 캐시 도입안을 기각했다", REJECTED_CARD, False),
        ("결정 읽기: [기획 확정] 결제 재시도를 최대 2회로 확정했다", "card_ef926aecb3c2", False),
        ("담당·예비 담당 연락처를 명부에서 확인한다", None, False),
        ("영역 접근 권한을 확인한다", None, False),
    ]
    steps = [
        OnboardingStep(step_id=f"stp_onb_0001_{i}", order=i, title=title, card_id=card, done=done)
        for i, (title, card, done) in enumerate(titles, start=1)
    ]
    return [
        OnboardingView(
            onboarding_id="onb_0001",
            context_id="ctx_onb_0001",
            member_id="emp_10",
            area="payment",
            steps=steps,
            progress=_progress(steps),
            created_at=NOW,
        )
    ]


def seed_changes() -> list[ChangeView]:
    def checklist(change_id: str, titles: list[str]) -> list[ChecklistItem]:
        return [
            ChecklistItem(item_id=f"chk_{change_id}_{i}", title=t)
            for i, t in enumerate(titles, start=1)
        ]

    return [
        ChangeView(
            change_id="chg_0001",
            context_id="ctx_asg_0001",
            change_kind="offboarding",
            member_id="emp_03",
            area="search",
            successor_id="emp_04",
            status="requested",
            handover_id="hov_0002",
            checklist=checklist(
                "chg_0001",
                [
                    "후임을 지정한다",
                    "인수인계를 확정한다",
                    "권한 회수 목록을 각 부서에 전달한다",
                    "퇴직을 확정한다(사람 승인)",
                ],
            ),
            created_at=NOW,
            updated_at=NOW,
        ),
        ChangeView(
            change_id="chg_0002",
            context_id="ctx_asg_0002",
            change_kind="join",
            member_id="emp_10",
            area="payment",
            status="confirmed",
            onboarding_id="onb_0001",
            checklist=checklist("chg_0002", ["온보딩을 시작한다"]),
            created_at=NOW,
            updated_at=NOW,
        ),
    ]


def seed_roster() -> list[RosterEntry]:
    def entry(area: str, member: str, role: str, start: datetime, end: datetime) -> RosterEntry:
        return RosterEntry(area=area, member_id=member, role=role, shift_start=start, shift_end=end)

    return [
        entry("payment", "emp_01", "owner", NOW, SHIFT_END),
        entry("payment", "emp_02", "backup", SHIFT_END, NEXT_END),
        entry("infra-ops", "emp_08", "owner", NOW, SHIFT_END),
        entry("infra-ops", "emp_09", "backup", SHIFT_END, NEXT_END),
    ]


def seed_audit() -> dict[str, list[AuditEntry]]:
    return {
        "ctx_hnd_0001": [
            AuditEntry(
                seq=1,
                at=NOW,
                kind="start_handover",
                payload={"id": "hov_0001", "completeness": 50, "status": "input_required"},
            ),
            AuditEntry(
                seq=2,
                at=NOW,
                kind="answer_handover_question",
                payload={"q": "hq_0001", "completeness": 75, "status": "input_required"},
            ),
        ]
    }


class HrBoard:
    """메모리 상태. 테스트는 새 보드로 바꿔 끼운다."""

    def __init__(self) -> None:
        self.onboardings = seed_onboardings()
        self.changes = seed_changes()
        self.roster = seed_roster()
        self.audit = seed_audit()

    def list_onboardings(
        self, member_id: str | None = None, area: str | None = None
    ) -> list[OnboardingView]:
        return [
            o
            for o in self.onboardings
            if (member_id is None or o.member_id == member_id) and (area is None or o.area == area)
        ]

    def update_step(self, onboarding_id: str, step_id: str, done: bool) -> StartOnboardingOutput:
        onboarding = next((o for o in self.onboardings if o.onboarding_id == onboarding_id), None)
        if onboarding is None:
            raise ApiError(404, "NOT_FOUND", f"온보딩 {onboarding_id}를 찾을 수 없다")
        step = next((s for s in onboarding.steps if s.step_id == step_id), None)
        if step is None:
            raise ApiError(404, "NOT_FOUND", f"단계 {step_id}를 찾을 수 없다")
        step.done = done  # 이미 그 상태면 그대로 성공 (hr의 duplicate)
        onboarding.progress = _progress(onboarding.steps)
        return StartOnboardingOutput(
            onboarding_id=onboarding_id, steps=onboarding.steps, progress=onboarding.progress
        )

    def list_changes(
        self,
        status: str | None = None,
        area: str | None = None,
        member_id: str | None = None,
    ) -> list[ChangeView]:
        return [
            c
            for c in self.changes
            if (status is None or c.status == status)
            and (area is None or c.area == area)
            and (member_id is None or c.member_id == member_id)
        ]

    def get_roster(self, area: str | None = None) -> GetRosterOutput:
        entries = [e for e in self.roster if area is None or e.area == area]
        covered = {e.area for e in self.roster}
        gap_areas = ["search"] if "search" not in covered and area in (None, "search") else []
        return GetRosterOutput(entries=entries, gap_areas=gap_areas, collected_at=NOW)

    def audit_timeline(self, context_id: str) -> list[AuditEntry]:
        return self.audit.get(context_id, [])

    @staticmethod
    def check_plan(draft: PlanDraft) -> CheckPlanOutput:
        """기각된 안(결제 서버 캐시)과 비슷하면 경고한다. 목의 단순 규칙이다."""
        if "캐시" in f"{draft.title} {draft.summary}":
            finding = PlanFinding(
                code="REJECTED_BEFORE",
                level="warn",
                message="이미 기각된 안과 비슷하다: [기각한 안] 결제 서버 캐시 도입안을 기각했다",
                card_ids=[REJECTED_CARD],
            )
            return CheckPlanOutput(
                verdict="warn",
                findings=[finding],
                similar_cards=[
                    SimilarCard(
                        card_id=REJECTED_CARD,
                        summary="[기각한 안] 결제 서버 캐시 도입안을 기각했다",
                        status="active",
                    )
                ],
                computed=CheckPlanComputed(
                    days_since_rejected=0, basis=f"기각 카드 {REJECTED_CARD}의 결정 시각"
                ),
            )
        return CheckPlanOutput(verdict="ok", no_related_cards=True)


BOARD = HrBoard()
