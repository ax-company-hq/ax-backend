"""인사 탭 나머지 화면(명부·감사 기록·계획 비교)의 목 모드 데이터와 동작.

형식은 모두 계약(`ax_contracts.schemas.hr`)을 쓰고, 목이 아닐 때는 `hr_client`로 hr skill을 부른다
(`routes.py`). 온보딩·발령 화면은 hr에서 기능을 빼면서 함께 뺐다 (#374, #377).
인수인계·후보 카드는 `mock_handover.py`에 있다.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ax_contracts.schemas.hr import (
    AuditEntry,
    CheckPlanComputed,
    CheckPlanOutput,
    GetRosterOutput,
    PlanDraft,
    PlanFinding,
    RosterEntry,
    SimilarCard,
)

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
SHIFT_END = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)
NEXT_END = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
REJECTED_CARD = "card_9304adb5e780"


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
        self.roster = seed_roster()
        self.audit = seed_audit()

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
