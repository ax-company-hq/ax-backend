"""지식 공백 보고서 skill 핸들러 (읽기 전용). 상태를 바꾸지 않아 `execute()`를 거치지 않는다."""

from __future__ import annotations

from datetime import UTC, datetime

from ax_contracts.schemas import AgentRequest, AgentResult
from support_agent import gap_rules
from support_agent import handlers as h
from support_agent.gap_schemas import KnowledgeGapInput


async def knowledge_gap_report(request: AgentRequest) -> AgentResult:
    """분류별 지식 공백 순위를 코드로 센다 (설계 #441). 문서는 고치지 않고 알리기만 한다."""
    parsed = h._parse(request, KnowledgeGapInput)
    if isinstance(parsed, AgentResult):
        return parsed
    report = gap_rules.build_report(
        h.STORE,
        datetime.now(UTC),
        window_days=parsed.window_days,
        min_samples=parsed.min_samples,
        category=parsed.category,
    )
    return h._done(request, report.model_dump(mode="json"))


GAP_HANDLERS = {"knowledge_gap_report": knowledge_gap_report}
