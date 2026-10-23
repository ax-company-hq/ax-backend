"""인수인계용 진행 중 항목 계산 (hr `list_in_progress`·`verify_handover`). 코드 규칙만 쓴다.

계약: `contracts/src/ax_contracts/schemas/planning.py`의 `InProgressItem`·`ListInProgress*`·
`VerifyHandover*` (#103). 상태를 바꾸지 않는다(읽기만).

항목 세 종류:

- `ticket`: 끝나지 않은 작업(`Task.state != "완료"`). ID `tkt_<작업 ID>` (예: `tkt_T-11`).
- `pr`: 아직 머지하지 않은 PR. ID `pr_<번호>` (예: `pr_31`).
- `deployment`: dev에 머지됐지만 복구·확인이 끝나지 않은 PR. ID `dep_pr_<번호>`.
  planning은 배포 상태를 모른다(infra 소관). 그래서 머지된 PR 중 아래 둘 중 하나면
  "확인 전"으로 본다.
  - 그 PR 문단에 장애가 연결됐고, 사건의 재발 방지 요청이 아직 `완료`가 아니다
    (status `incident_follow_up_open`).
  - 그 PR이 걸린 요청이 아직 `완료`가 아니다 (status `merged_unconfirmed`).

담당자·영역 대응 (planning 내부 규칙, 시연값):

- 담당자: 작업 `owner` 라벨 → 명부 `RosterEntry.member_id` (`OWNER_MEMBER_IDS`). 대응이 없는
  작업(에이전트가 혼자 끝낸 작업 등)은 사람 인계 대상이 아니라서 돌려주지 않는다.
- 영역: PR 바뀐 경로 접두사(`AREA_BY_PATH`) → 없으면 기획서 문단·제목 낱말(`AREA_BY_WORD`).
  값 집합은 명부 `RosterEntry.area`와 같다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from planning_agent.domain.models import PR, Request, State, Task

# 작업 owner 라벨 → 명부 member_id (시연값, hr 명부 목 데이터와 맞춘다)
OWNER_MEMBER_IDS: dict[str, str] = {
    "백엔드": "emp_01",
    "프론트": "emp_02",
}

# 바뀐 경로 접두사 → 명부 area (앞에서부터 처음 맞는 것)
AREA_BY_PATH: list[tuple[str, str]] = [
    ("shop-api/payment/", "payment"),
    ("shop-api/cart/", "payment"),
    ("shop-web/checkout/", "payment"),
    ("shop-api/orders/", "payment"),
    ("shop-api/search/", "search"),
    ("shop-web/search/", "search"),
    ("shop-api/inventory/", "inventory"),
    ("shop-api/catalog/", "catalog"),
    ("shop-web/catalog/", "catalog"),
    ("infra/", "infra-ops"),
]

# 경로로 못 정하면 기획서 문단·제목 낱말 → 명부 area
AREA_BY_WORD: list[tuple[str, str]] = [
    ("결제", "payment"),
    ("장바구니", "payment"),
    ("주문", "payment"),
    ("환불", "payment"),
    ("검색", "search"),
    ("재고", "inventory"),
    ("상품", "catalog"),
    ("신간", "catalog"),
    ("문의", "support-desk"),
    ("고객센터", "support-desk"),
    ("배포", "infra-ops"),
]

ITEM_PREFIXES = ("tkt_", "pr_", "dep_pr_")


@dataclass
class Item:
    """계약 `InProgressItem`이 될 값 + 근거 ref."""

    item_id: str
    kind: str  # "deployment" | "ticket" | "pr"
    title: str
    status: str
    owner_id: str
    area: str | None
    updated_at: datetime | None
    refs: list[str] = field(default_factory=list)


def ticket_id(task: Task) -> str:
    return f"tkt_{task.id}"


def pr_item_id(pr: PR) -> str:
    return f"pr_{pr.number}"


def deployment_id(pr: PR) -> str:
    return f"dep_pr_{pr.number}"


def is_planning_item_id(item_id: str) -> bool:
    return item_id.startswith(ITEM_PREFIXES)


def area_of_paths(paths: list[str]) -> str | None:
    for path in paths:
        for prefix, area in AREA_BY_PATH:
            if path.startswith(prefix):
                return area
    return None


def area_of_text(*texts: str) -> str | None:
    for text in texts:
        for word, area in AREA_BY_WORD:
            if word in text:
                return area
    return None


def _task_area(s: State, task: Task) -> str | None:
    paths = [path for p in s.prs if p.task == task.id for path in p.changed_paths]
    found = area_of_paths(paths)
    if found:
        return found
    spec = next((x for x in s.specs if x.id == task.spec), None)
    para = next((p for p in spec.paras if p.id == task.para), None) if spec else None
    return area_of_text(task.title, para.text if para else "", spec.title if spec else "")


def _pr_area(s: State, pr: PR, task: Task | None) -> str | None:
    return area_of_paths(pr.changed_paths) or (_task_area(s, task) if task else None)


def _request_of_task(s: State, task: Task) -> Request | None:
    spec = next((x for x in s.specs if x.id == task.spec), None)
    if spec is None:
        return None
    return next((r for r in s.requests if r.spec == spec.id), None) or next(
        (r for r in s.requests if r.id == spec.request), None
    )


def _deployment_status(s: State, pr: PR, task: Task) -> tuple[str | None, list[str]]:
    """머지된 PR이 아직 확인 전이면 (status, 근거 ref). 확인이 끝났으면 (None, [])."""
    for inc in s.incidents:
        if not any(c.pr == pr.id for c in inc.candidates):
            continue
        follow = next((r for r in s.requests if r.id == inc.follow_up), None)
        if follow is not None and follow.state not in ("완료", "기각"):
            refs = [f"incident:{inc.context_id}", f"req:{follow.id}"]
            return "incident_follow_up_open", refs
    req = _request_of_task(s, task)
    if req is not None and req.state not in ("완료", "기각"):
        return "merged_unconfirmed", [f"req:{req.id}"]
    return None, []


def all_items(s: State) -> list[Item]:
    """모든 담당자의 진행 중 항목 (작업 순서 → PR 번호 순). 상태를 바꾸지 않는다."""
    items: list[Item] = []
    tasks = {t.id: t for t in s.tasks}
    for task in s.tasks:
        owner = OWNER_MEMBER_IDS.get(task.owner)
        if owner is None or task.state == "완료":
            continue
        items.append(
            Item(
                item_id=ticket_id(task),
                kind="ticket",
                title=task.title,
                status=task.state,
                owner_id=owner,
                area=_task_area(s, task),
                updated_at=None,
                refs=[f"task:{task.id}", f"para:{task.spec}:{task.para}"],
            )
        )
    for pr in sorted(s.prs, key=lambda p: p.number):
        task = tasks.get(pr.task)
        owner = OWNER_MEMBER_IDS.get(task.owner) if task else None
        if task is None or owner is None:
            continue
        base_refs = [f"pr:{pr.number}", f"task:{task.id}", f"para:{task.spec}:{task.para}"]
        if not pr.merged:
            items.append(
                Item(
                    item_id=pr_item_id(pr),
                    kind="pr",
                    title=pr.title,
                    status="open",
                    owner_id=owner,
                    area=_pr_area(s, pr, task),
                    updated_at=None,
                    refs=base_refs,
                )
            )
            continue
        status, refs = _deployment_status(s, pr, task)
        if status is None:
            continue
        items.append(
            Item(
                item_id=deployment_id(pr),
                kind="deployment",
                title=f"{pr.id} 머지 후 복구·확인 대기: {pr.title}",
                status=status,
                owner_id=owner,
                area=_pr_area(s, pr, task),
                updated_at=pr.merged_at,
                refs=base_refs + refs,
            )
        )
    return items


def in_progress(s: State, owner_id: str, area: str | None = None) -> list[Item]:
    """담당자(`member_id`)·영역(`area`, 없으면 전체)의 진행 중 항목."""
    return [i for i in all_items(s) if i.owner_id == owner_id and (area is None or i.area == area)]


def known_ids(s: State) -> set[str]:
    """planning이 아는 항목 ID 전부 (끝난 것 포함). stale 판정에 쓴다."""
    ids = {ticket_id(t) for t in s.tasks}
    for pr in s.prs:
        ids.add(pr_item_id(pr))
        if pr.merged:
            ids.add(deployment_id(pr))
    return ids


@dataclass
class HandoverCheck:
    missing: list[Item]
    stale_item_ids: list[str]
    unknown_item_ids: list[str]
    current: list[Item]


def check_handover(
    s: State, owner_id: str, area: str | None, handover_item_ids: list[str]
) -> HandoverCheck:
    """인계 목록과 진행 중 항목 비교.

    - `missing`: 진행 중인데 인계 목록에 없는 항목 (판정에 쓴다).
    - `stale_item_ids`: 인계 목록에 있는 planning 항목 ID 중 이미 끝난 것 (정보로만).
      다른 담당자·영역에서 아직 진행 중인 항목은 끝난 것이 아니라서 넣지 않는다.
    - `unknown_item_ids`: planning ID 형식인데 planning에 없는 ID (근거 요약에만).
      결정 카드·문서처럼 planning 항목이 아닌 ID는 비교하지 않는다.
    """
    current = in_progress(s, owner_id, area)
    listed = list(dict.fromkeys(handover_item_ids))
    listed_set = set(listed)
    missing = [i for i in current if i.item_id not in listed_set]
    still_open = {i.item_id for i in all_items(s)}
    known = known_ids(s)
    stale: list[str] = []
    unknown: list[str] = []
    for item_id in listed:
        if not is_planning_item_id(item_id):
            continue
        if item_id not in known:
            unknown.append(item_id)
        elif item_id not in still_open:
            stale.append(item_id)
    return HandoverCheck(
        missing=missing, stale_item_ids=stale, unknown_item_ids=unknown, current=current
    )
