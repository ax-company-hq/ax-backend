"""기획서 확정·PR 머지 자동 처리 판정 (코드로만 계산, LLM 없음).

`execute(confirm_spec)`·`execute(merge_pr)`에 보내는 params를 safety #171 규칙 키 그대로 만든다
(`safety/src/ax_safety/rules/thresholds.toml`, 근거 #166). 값은 모두 도메인 상태에서 계산하고,
요청 쪽(화면·A2A)이 보낸 숫자는 쓰지 않는다.

- 기획서: 질문 0·경고 0·근거 1 이상·되돌릴 수 없음 0이면 자동 확정. 질문이 남으면 "답 대기",
  그 밖에 걸리면 "교차검증 대기".
- PR: 마지막 검수 통과 + 아래 규칙에 안 걸리면 자동 머지. 걸리면 "교차검증 대기",
  되돌릴 수 없는 영역에 닿으면 "사람 확인".

규칙 기준값은 #171 `thresholds.toml`의 제안값을 옮겨 둔 것이다. 최종 판정은 safety가 다시 하고,
safety가 보류·거절하면 서비스가 "교차검증 대기"로 둔다 (두 곳이 어긋나도 막히는 쪽으로).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from planning_agent.domain.models import PR, Para, Spec, State
from planning_agent.domain.trace import changed_paths_for

# ---- 되돌릴 수 없는 영역 (경로 접두사 → 이유). 제안 목록, 확정은 safety·사람 (#166 노트)
IRREVERSIBLE_PREFIXES: dict[str, str] = {
    "shop-api/payment/migrations/": "결제 데이터 삭제·변경 마이그레이션",
    "shop-api/accounts/pii/": "개인정보 필드",
    "shop-api/settlement/payout/": "실제 송금·정산 연동",
}

# ---- #171 thresholds.toml merge_pr 규칙 (제안값, generate_pr와 같은 크기·경로 기준)
PR_MAX_LINES = 300  # pr_changed_lines: changed_lines > 300
PR_MAX_FILES = 20  # pr_changed_files: changed_files > 20
PR_ALLOWED_PATHS: tuple[str, ...] = ("shop-api/", "docs/")  # pr_allowed_paths
REVIEW_MISMATCH_LIMIT = 2  # pr_review_mismatch: review_mismatch_count >= 2

WAIT_ANSWER = "답 대기"
WAIT_CROSS_CHECK = "교차검증 대기"
WAIT_HUMAN = "사람 확인"


@dataclass
class Check:
    """판정 결과. `params`는 execute에 그대로 보내고, `rules`는 걸린 #171 규칙 키."""

    params: dict[str, Any]
    rules: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rules


def irreversible_paths(paths: list[str]) -> list[str]:
    """되돌릴 수 없는 영역에 닿는 경로."""
    return [p for p in paths if any(p.startswith(prefix) for prefix in IRREVERSIBLE_PREFIXES)]


def planned_area(index: int) -> str:
    """기획서의 n번째 요구사항이 바꿀 영역 (목 규칙: 첫 요구사항은 백엔드, 나머지는 화면)."""
    return "shop-api/cart/" if index == 0 else "shop-web/checkout/"


def planned_paths(spec: Spec, para: Para, index: int) -> list[str]:
    """요구사항 문단이 바꿀 파일 경로. 문단에 경로가 적혀 있으면 그것, 없으면 역추적 규칙."""
    if para.paths:
        return list(dict.fromkeys(para.paths))
    return changed_paths_for(spec.id, para.id, planned_area(index))


# ---------------------------------------------------------------- 기획서 (confirm_spec)


def spec_params(spec: Spec) -> dict[str, Any]:
    """`confirm_spec` params: open_questions·warnings·basis_count·irreversible_count."""
    reqs = [p for p in spec.paras if p.kind == "req"]
    return {
        "open_questions": 1 if spec.question else 0,
        "warnings": sum(1 for p in spec.paras if p.warn),
        "basis_count": len(spec.basis),
        "irreversible_count": sum(
            1 for i, p in enumerate(reqs) if irreversible_paths(planned_paths(spec, p, i))
        ),
    }


def spec_check(spec: Spec) -> Check:
    params = spec_params(spec)
    rules: list[str] = []
    if params["open_questions"] > 0:
        rules.append("spec_open_question")
    if params["warnings"] > 0:
        rules.append("spec_rejected_warning")
    if params["basis_count"] < 1:
        rules.append("spec_no_basis")
    if params["irreversible_count"] > 0:
        rules.append("irreversible")
    return Check(params=params, rules=rules)


def spec_wait_state(check: Check) -> str:
    return WAIT_ANSWER if "spec_open_question" in check.rules else WAIT_CROSS_CHECK


# ---------------------------------------------------------------- PR (merge_pr)


def _para_key(state: State, pr: PR) -> str | None:
    task = next((t for t in state.tasks if t.id == pr.task), None)
    return f"{task.spec}:{task.para}" if task else None


def _incident_paras(state: State) -> set[str]:
    return {f"{s.id}:{p.id}" for s in state.specs for p in s.paras if p.incident}


def linked_incident_count(state: State, pr: PR) -> int:
    """장애 표시 문단 연결 수.

    이 PR의 문단 + 같은 파일을 바꾼 다른 PR의 문단 중 장애 표시(link_incident)가 있는 것.
    """
    marked = _incident_paras(state)
    linked: set[str] = set()
    own = _para_key(state, pr)
    if own in marked:
        linked.add(own)
    paths = set(pr.changed_paths)
    for other in state.prs:
        if other.id == pr.id or not paths & set(other.changed_paths):
            continue
        key = _para_key(state, other)
        if key in marked:
            linked.add(key)
    return len(linked)


def pr_params(state: State, pr: PR) -> dict[str, Any]:
    """`merge_pr` params: generate_pr와 같은 크기·경로 + 검수 불일치·장애 연결·되돌릴 수 없음."""
    paths = sorted(set(pr.changed_paths))
    return {
        "changed_lines": pr.gate.lines,
        "changed_files": pr.gate.files,
        "changed_paths": paths,
        "review_mismatch_count": sum(1 for r in pr.reviews if r == "불일치"),
        "linked_incident_count": linked_incident_count(state, pr),
        "irreversible_count": len(irreversible_paths(paths)),
    }


def pr_check(state: State, pr: PR) -> Check:
    params = pr_params(state, pr)
    rules: list[str] = []
    if params["changed_lines"] > PR_MAX_LINES:
        rules.append("pr_changed_lines")
    if params["changed_files"] > PR_MAX_FILES:
        rules.append("pr_changed_files")
    if not all(any(p.startswith(a) for a in PR_ALLOWED_PATHS) for p in params["changed_paths"]):
        rules.append("pr_allowed_paths")
    if params["review_mismatch_count"] >= REVIEW_MISMATCH_LIMIT:
        rules.append("pr_review_mismatch")
    if params["linked_incident_count"] > 0:
        rules.append("pr_linked_incident")
    if params["irreversible_count"] > 0:
        rules.append("irreversible")
    return Check(params=params, rules=rules)


def pr_wait_state(check: Check) -> str:
    return WAIT_HUMAN if "irreversible" in check.rules else WAIT_CROSS_CHECK
