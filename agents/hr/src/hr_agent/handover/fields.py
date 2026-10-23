"""인수인계가 갖춰야 할 필수 항목과 완성도 계산(코드 규칙, LLM이 정하지 않는다)."""

from __future__ import annotations

# (필드, 화면용 이름, 인계자에게 묻는 고정 질문의 끝 문구)
FIELDS: list[tuple[str, str]] = [
    ("in_progress", "진행 중인 일"),
    ("decisions", "내려진 결정과 이유"),
    ("rollback_criteria", "되돌리는 기준"),
    ("contacts", "연락처·후임이 물어볼 사람"),
    ("known_risks", "알려진 위험"),
]
FIELD_LABEL = dict(FIELDS)


def completeness(filled: int, total: int = len(FIELDS)) -> int:
    """채운 필드 수 / 전체 필드 수 × 100 (반올림)."""
    return round(filled * 100 / total) if total else 0


def question_id(handover_id: str, field: str) -> str:
    return f"qst_{handover_id}_{field}"
