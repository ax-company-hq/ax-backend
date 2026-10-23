"""planning 연결: 진행 중 항목 조회(`list_in_progress`)와 인수인계 검증(`verify_handover`).

실제 연동(3단계)은 `call_agent("planning", ...)`로 같은 인터페이스를 구현한다. 지금은 계약이 정한
값의 뜻(`owner_id` = 명부 `member_id`, `area` = 명부 `area`)에 맞춘 목 데이터를 쓴다.
검증은 사실 확인만 한다: 진행 중 항목이 인계 목록에 빠짐없이 있는지 비교하고
내용은 판단하지 않는다.
"""

from __future__ import annotations

from typing import Protocol

from ax_contracts.schemas import (
    InProgressItem,
    ListInProgressInput,
    ListInProgressOutput,
    VerifyHandoverInput,
    VerifyHandoverOutput,
)
from hr_agent.config import Clock


class PlanningClient(Protocol):
    async def list_in_progress(self, query: ListInProgressInput) -> ListInProgressOutput: ...

    async def verify_handover(self, query: VerifyHandoverInput) -> VerifyHandoverOutput: ...


def _item(
    item_id: str, kind: str, title: str, status: str, owner: str, area: str
) -> InProgressItem:
    return InProgressItem(
        item_id=item_id, kind=kind, title=title, status=status, owner_id=owner, area=area
    )


# 데모용 가상 데이터. 담당자·영역은 `roster/seed.py`의 명부와 같은 값이다.
DEFAULT_ITEMS: list[InProgressItem] = [
    _item("pr_412", "pr", "결제 재시도 로직 PR", "open", "emp_01", "payment"),
    _item("tkt_201", "ticket", "결제 타임아웃 설정 정리", "in_progress", "emp_01", "payment"),
    _item("dep_102", "deployment", "shop-api v1.4.1 복구 확인", "verifying", "emp_01", "payment"),
    _item("tkt_301", "ticket", "검색 색인 재생성 일정", "in_progress", "emp_03", "search"),
    _item("pr_501", "pr", "카탈로그 카테고리 개편 PR", "open", "emp_05", "catalog"),
    _item("dep_801", "deployment", "인프라 점검 배포", "rolling_out", "emp_08", "infra-ops"),
]


class MockPlanningClient:
    def __init__(self, clock: Clock, items: list[InProgressItem] | None = None) -> None:
        self._clock = clock
        self.items = list(DEFAULT_ITEMS if items is None else items)

    def _in_progress(self, owner_id: str, area: str | None) -> list[InProgressItem]:
        return [
            i for i in self.items if i.owner_id == owner_id and (area is None or i.area == area)
        ]

    async def list_in_progress(self, query: ListInProgressInput) -> ListInProgressOutput:
        found = self._in_progress(query.owner_id, query.area)
        return ListInProgressOutput(items=found, collected_at=self._clock())

    async def verify_handover(self, query: VerifyHandoverInput) -> VerifyHandoverOutput:
        in_progress = self._in_progress(query.owner_id, query.area)
        listed = set(query.handover_item_ids)
        missing = [i for i in in_progress if i.item_id not in listed]
        known = {i.item_id for i in in_progress}
        stale = sorted(i for i in listed if i not in known)
        return VerifyHandoverOutput(
            approved=not missing,
            reason_code="missing_items" if missing else "complete",
            missing=missing,
            stale_item_ids=stale,
            checked_at=self._clock(),
        )
