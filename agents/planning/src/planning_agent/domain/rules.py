"""코드로만 하는 계산: 간단 요청 판정, 자동 처리 단계, 중복 요청 찾기, 관련 PR 찾기, 롤백 교차검증.

LLM을 부르지 않는다. 같은 입력이면 항상 같은 결과다.
단계·기준값·구간은 시연값이다 (요구사항정의서 AS-B03·AS-B11 등 미결).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Literal

from planning_agent.domain.models import (
    PR,
    AutoCheck,
    AutoLevel,
    AutoPolicy,
    Incident,
    Request,
    State,
)

# ---- 자동 처리 단계 (data.ts AUTO_LEVELS, PROMOTE_AFTER). 시연값, AS-B11
AUTO_LEVELS: list[AutoLevel] = [
    AutoLevel(level=0, name="자동 처리 중지", lines=0, files=0),
    AutoLevel(level=1, name="문구·표시 소규모", lines=30, files=2),
    AutoLevel(level=2, name="문구·표시 중규모", lines=60, files=3),
    AutoLevel(level=3, name="화면 표시 전반", lines=100, files=5),
]
PROMOTE_AFTER = 3  # 연속 정상 몇 건이면 승급 제안 (시연값)

# ---- 간단 요청 판정 (data.ts checkSimple)
SENSITIVE = [
    "결제",
    "주문",
    "쿠폰",
    "환불",
    "정산",
    "장바구니",
    "재고",
    "개인정보",
    "로그인",
    "권한",
]
DISPLAY_ONLY = ["문구", "오타", "라벨", "링크", "배너", "안 보인다", "잘린다", "정렬"]
_SMALL_WORDS = re.compile("문구|오타|라벨")

# ---- 롤백 교차검증: 배포 시각 이전 이 구간 안에 머지된 PR이 있으면 승인한다.
# 시연값, 요구사항정의서 AS-B 미결 (구간 길이 확정 전)
ROLLBACK_LOOKBACK = timedelta(minutes=30)


def auto_limit(policy: AutoPolicy) -> dict:
    level = AUTO_LEVELS[policy.level]
    return {"level": level.level, "lines": level.lines, "files": level.files, "paths": policy.paths}


def estimate(title: str) -> tuple[int, int]:
    """요청 제목으로 예상 변경 (줄, 파일)을 정한다 (목 규칙, data.ts estimate)."""
    if "전체" in title:
        return 45, 3
    return (4, 1) if _SMALL_WORDS.search(title) else (18, 2)


def check_simple(title: str, policy: AutoPolicy) -> AutoCheck:
    """간단 요청 판정. 전부 통과하면 사람 확정·사람 머지 없이 끝까지 간다."""
    limit = auto_limit(policy)
    if policy.level == 0:
        return AutoCheck(
            ok=False,
            reasons=[
                "자동 처리 중지(L0): 자동 머지한 PR이 장애에 연결돼 범위가 줄어든 상태"
                " → 사람 확정 필요"
            ],
        )
    hit = next((w for w in SENSITIVE if w in title), None)
    display = any(w in title for w in DISPLAY_ONLY)
    rejected = "버튼만" in title  # 인사 ② check_plan 기각 이력 (목)
    lines, files = estimate(title)
    small = lines <= limit["lines"] and files <= limit["files"]
    lv = policy.level
    if small:
        size = (
            f"예상 변경 {lines}줄·{files}파일 "
            f"(L{lv} 기준 {limit['lines']}줄·{limit['files']}파일 이하)"
        )
    else:
        size = (
            f"예상 변경 {lines}줄·{files}파일 → L{lv} 기준"
            f"({limit['lines']}줄·{limit['files']}파일) 초과"
        )
    return AutoCheck(
        ok=hit is None and display and not rejected and small,
        reasons=[
            f'민감 경로 포함("{hit}") → 사람 확정 필요' if hit else "결제·주문·개인정보 경로 아님",
            "화면 표시만 바꾸는 요청" if display else "새 기능·동작 변경 → 사람 확정 필요",
            "인사 ② 기각 이력 있음 → 사람 확정 필요" if rejected else "인사 ② 기각 이력 없음",
            size,
        ],
    )


def find_duplicate(requests: list[Request], title: str) -> str | None:
    """같은 제목 요청이 있으면 원본 요청 ID (중복 연결된 것이면 그 원본)."""
    same = next((r for r in requests if r.title == title), None)
    if same is None:
        return None
    return same.duplicate_of or same.id


def para_of(state: State, pr: PR) -> tuple[str | None, str | None]:
    """PR → 작업 → (기획서 ID, 문단 ID)."""
    task = next((t for t in state.tasks if t.id == pr.task), None)
    return (task.spec, task.para) if task else (None, None)


def merged_in_window(
    state: State, window_from: datetime, window_to: datetime, component: str | None = None
) -> list[PR]:
    """구간 안에 머지된 PR. component가 있으면 바뀐 경로 접두사로 거른다. 최근 머지 순."""
    found = [
        p
        for p in state.prs
        if p.merged
        and p.merged_at is not None
        and window_from <= p.merged_at <= window_to
        and (not component or any(path.startswith(component) for path in p.changed_paths))
    ]
    return sorted(found, key=lambda p: (p.merged_at, p.number), reverse=True)


def deploy_of(state: State, service: str, version: str) -> PR | None:
    """버전에 해당하는 배포 = 그 버전으로 나간 머지 PR (데모: 머지 = dev 배포). 가장 최근 것."""
    target = f"{service} {version}"
    found = [
        p
        for p in state.prs
        if p.merged and p.merged_at is not None and p.deployed_version == target
    ]
    return max(found, key=lambda p: (p.merged_at, p.number), default=None)


def incident_of(state: State, context_id: str) -> Incident | None:
    """planning이 가진 사건 기록 (link_incident·find_related_prs로 받은 사건)."""
    return next((i for i in state.incidents if i.context_id == context_id), None)


RollbackReason = Literal["deploy_before_incident", "no_deploy_in_window", "deploy_after_incident"]


def verify_rollback(
    state: State, deployed_at: datetime, incident_opened_at: datetime
) -> tuple[bool, RollbackReason, list[PR]]:
    """롤백 대상 배포가 사건 전이고, 배포 전 구간 안에 머지 PR이 있으면 승인."""
    if deployed_at > incident_opened_at:
        return False, "deploy_after_incident", []
    prs = merged_in_window(state, deployed_at - ROLLBACK_LOOKBACK, deployed_at)
    if prs:
        return True, "deploy_before_incident", prs
    return False, "no_deploy_in_window", []
