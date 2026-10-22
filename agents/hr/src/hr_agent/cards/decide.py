"""결정 탐지: 기록에서 "무엇을 왜 정했는지"를 찾는다.

규칙이 먼저다.
1. 결정 이유(`decision_reason`)가 구조화된 필드로 오면 그대로 쓴다.
2. 결정을 담은 기록(`DECISION_KINDS`)인데 이유가 없으면, 요약 문장에서 "…위해·…때문에" 꼴의
   절을 규칙으로 찾아 본다. 찾으면 `extracted=True`(확신도를 낮춘다), 못 찾으면 이유를 비워 둔다.
3. 결정이 아닌 기록(사건·사례·공지 등)에 이유가 없으면 결정이 없다 → 기록만 남긴다.

외부 입력(`untrusted`)은 여기서 읽지 않는다. LLM 추출(실제 연동 단계)이 붙어도 외부 문구는
데이터 블록으로만 넣는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ax_contracts.schemas import RecordCase
from hr_agent.cards.mapping import DECISION_KINDS

# "…하기 위해", "…때문에" 같은 이유 절을 문장 끝이나 괄호 안에서 찾는다.
_REASON = re.compile(r"([^.()\n]*?(?:위해서?|위하여|때문에|덕분에|으로 인해|로 인해))")


@dataclass(frozen=True)
class DecisionInfo:
    decision: str
    reason: str | None
    alternatives: list[str]
    extracted: bool = False  # 규칙으로 요약 문장에서 이유를 뽑았다


def _clean(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        text = item.strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def extract_reason(summary: str) -> str | None:
    """요약 문장에서 이유 절을 규칙으로 뽑는다. 없으면 None."""
    match = _REASON.search(summary)
    if not match:
        return None
    clause = match.group(1).strip(" ,")
    return clause or None


def detect_decision(record: RecordCase) -> DecisionInfo | None:
    alternatives = _clean(record.alternatives_considered)
    reason = (record.decision_reason or "").strip()
    if reason:
        return DecisionInfo(record.summary.strip(), reason, alternatives)
    if record.kind in DECISION_KINDS:
        extracted = extract_reason(record.summary)
        return DecisionInfo(
            record.summary.strip(), extracted, alternatives, extracted=extracted is not None
        )
    return None
