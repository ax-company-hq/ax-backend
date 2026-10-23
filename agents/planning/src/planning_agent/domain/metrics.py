"""지표 6종 (프로토타입 `pages.tsx` Metrics와 같은 식). 숫자는 모두 코드로 계산한다."""

from __future__ import annotations

import math

from planning_agent.domain.models import State


def _round(value: float) -> int:
    """JS `Math.round`와 같게 반올림한다 (.5는 올림)."""
    return math.floor(value + 0.5)


def pct(a: int, b: int) -> int | None:
    return _round(a / b * 100) if b else None


def compute_metrics(s: State) -> dict:
    prs = s.prs
    avg_pr = (
        _round(sum(s.first_pr_minutes) / len(s.first_pr_minutes)) if s.first_pr_minutes else None
    )
    human = sum(x.human_sentences for x in s.specs) + sum(
        1 for t in s.threads for m in t.messages if m.dept != "planning"
    )
    last_pass = sum(1 for p in prs if p.reviews and p.reviews[-1] == "통과")
    first_pass = sum(1 for p in prs if p.reviews and p.reviews[0] == "통과")
    return {
        # 기획 한 줄 → 첫 PR: 요청 등록부터 첫 PR까지 평균 (분)
        "first_pr_minutes_avg": avg_pr,
        # 완료 기준 일치율: 마지막 검수가 통과인 PR 비율 (%)
        "done_criteria_match_rate": pct(last_pass, len(prs)),
        # 사람이 쓴 문장 수: 기획서 사람 문장 + 기획개발 밖 메시지 수 (확정·머지 클릭은 따로)
        "human_sentences": human,
        # PR 검수 1회 통과율: 첫 검수가 통과인 PR 비율 (%)
        "first_review_pass_rate": pct(first_pass, len(prs)),
        # 기각 안 경고: 인사 ② check_plan 경고 건수
        "warnings": s.warnings,
        # 개선 초안 채택률: 인사·데이터 초안 중 채택 비율 (%)
        "draft_adoption_rate": pct(s.drafts_accepted, s.drafts_in),
        # 계산에 쓴 원자료
        "basis": {
            "pr_count": len(prs),
            "pr_last_review_pass": last_pass,
            "pr_first_review_pass": first_pass,
            "first_pr_minutes": list(s.first_pr_minutes),
            "drafts_in": s.drafts_in,
            "drafts_accepted": s.drafts_accepted,
        },
    }
