"""결정 탐지: 기록에서 "무엇을 왜 정했는지"를 찾는다.

규칙이 먼저다.
1. 결정 이유(`decision_reason`)가 구조화된 필드로 오면 그대로 쓴다.
2. 결정을 담은 기록(`DECISION_KINDS`)인데 이유가 없으면, 요약 문장에서 "이유는 …"·"…위해·…때문에"
   꼴의 절을 규칙으로 찾아 본다. 찾으면 `extracted=True`(확신도를 낮춘다), 못 찾으면 비워 둔다.
3. 결정이 아닌 기록(사건·사례·공지 등)에 이유가 없으면 결정이 없다 → 기록만 남긴다.
4. 종류가 `decision`·`decision_extracted`인 기록의 요약에 "기각했다·채택하지 않기로 했다" 같은
   표현이 있으면 기각한 안으로 읽는다(`rejected`). 철회·부정("기각하지 않고")이 있으면 읽지 않는다.

외부 입력(`untrusted`)은 여기서 읽지 않는다. LLM 추출(실제 연동 단계)이 붙어도 외부 문구는
데이터 블록으로만 넣는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ax_contracts.schemas import RecordCase, RecordKind
from hr_agent.cards.mapping import DECISION_KINDS

# 이유를 직접 밝히는 표지: "이유는 …", "사유: …", "근거는 …"
_MARKER = re.compile(
    r"(?:이유는|이유\s*[:：]|사유는|사유\s*[:：]|근거는|근거\s*[:：]|목적은)"
    r"\s*([^.()\n]+)"
)

_NO_REASON_WORDS = ("없", "불명", "미정", "아직")

# 이유 절의 끝말: "…하기 위해", "…때문에", "…이므로", "…를 고려해" 등.
# "위해 요소"처럼 명사로 쓰인 경우는 이유가 아니다.
_CONNECTIVE = (
    r"(?:위해서?(?!\s*(?:요소|요인|물질|행위))|위하여|때문에|덕분에|탓에"
    r"|(?:으)?로 인해|(?<![가-힣])인해|(?:으)?므로|(?:이)?라서|고려해서?|감안해서?|대비해서?"
    r"|[을를] 위한|목적"
    r"|(?:으)?려고|[이가]\s[가-힣]+(?:어|여|아|져|겨|해)서)"
)
# 절은 마침표·쉼표·괄호·줄표 같은 경계 뒤에서 시작한다.
_REASON = re.compile(r"([^.,;()\n—–]*?" + _CONNECTIVE + r")")

# 기각 표현: 결정 종류 `decision`·`decision_extracted`일 때만 문장에서 읽는다(확신도는 낮게).
_REJECT = re.compile(
    r"(?:기각|반려)(?:했|한다|하기로|됐|된다|되었|함|하였|처리)"
    r"|(?:채택|도입|적용|추가|승인|진행|허용|시행|반영)하지\s*(?:않기로|않았|않는다|않음)"
    r"|받아들이지\s*(?:않기로|않았|않는다)"
)
# 기각이 아닌 표현: 부정("기각하지 않고")·기각의 철회·다시 채택·"기각했던 안을 …"
_NOT_REJECT = re.compile(
    r"(?:기각|반려)(?:하지|되지)\s*(?:않|못)"
    r"|(?:기각|반려)[^.]{0,10}철회"
    r"|(?:기각|반려)(?:했|됐|되었)던"
    r"|다시\s*(?:채택|승인|도입)|재승인|재채택|번복"
)
_INFER_KINDS = frozenset({RecordKind.DECISION, RecordKind.DECISION_EXTRACTED})


@dataclass(frozen=True)
class DecisionInfo:
    decision: str
    reason: str | None
    alternatives: list[str]
    extracted: bool = False  # 규칙으로 요약 문장에서 이유를 뽑았다
    rejected: bool = False  # 요약 문장의 기각 표현에서 읽었다(결정 종류가 기각이 아닐 때)


def _clean(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        text = item.strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def extract_reason(summary: str) -> str | None:
    """요약 문장에서 이유 절을 규칙으로 뽑는다. 없으면 None."""
    marked = _MARKER.search(summary)
    if marked:
        clause = marked.group(1).strip(" ,")
        if clause and not clause.startswith(_NO_REASON_WORDS):  # "근거는 없지만 …"은 이유가 아니다
            return clause
    match = _REASON.search(summary)
    if not match:
        return None
    clause = match.group(1).strip(" ,")
    return clause or None


def infer_rejected(kind: RecordKind, summary: str) -> bool:
    """결정 종류가 `decision`·`decision_extracted`인 기록의 요약에서 기각 표현을 찾는다."""
    if kind not in _INFER_KINDS or _NOT_REJECT.search(summary):
        return False
    return _REJECT.search(summary) is not None


def detect_decision(record: RecordCase) -> DecisionInfo | None:
    alternatives = _clean(record.alternatives_considered)
    reason = (record.decision_reason or "").strip()
    rejected = infer_rejected(record.kind, record.summary)
    if reason:
        return DecisionInfo(record.summary.strip(), reason, alternatives, rejected=rejected)
    if record.kind in DECISION_KINDS:
        extracted = extract_reason(record.summary)
        return DecisionInfo(
            record.summary.strip(),
            extracted,
            alternatives,
            extracted=extracted is not None,
            rejected=rejected,
        )
    return None
