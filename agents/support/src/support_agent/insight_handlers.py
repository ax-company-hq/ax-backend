"""문의 집계·급증 감지 skill 핸들러 (읽기 전용).

상태를 바꾸지 않으므로 `execute()`를 거치지 않는다. 숫자는 `insights`가 코드로 센다.
급증 신호를 `support.inquiry_surge` 이벤트로 발행하는 일은 contracts에 data 모델이 생긴 뒤에 한다.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ax_contracts.schemas import AgentRequest, AgentResult
from support_agent import handlers as h
from support_agent import insights
from support_agent.insight_schemas import SurgeInput


async def inquiry_overview(request: AgentRequest) -> AgentResult:
    """KPI·분류별 건수·답변 대기 문의."""
    overview = insights.overview(h.STORE, datetime.now(UTC))
    return h._done(request, overview.model_dump(mode="json"))


async def detect_inquiry_surge(request: AgentRequest) -> AgentResult:
    """분류별 문의 급증을 코드로 감지한다 (DEP-CS-05)."""
    parsed = h._parse(request, SurgeInput)
    if isinstance(parsed, AgentResult):
        return parsed
    report = insights.detect_surge(
        h.STORE,
        datetime.now(UTC),
        window_minutes=parsed.window_minutes,
        baseline_hours=parsed.baseline_hours,
        min_count=parsed.min_count,
        ratio=parsed.ratio,
    )
    return h._done(request, report.model_dump(mode="json"))


INSIGHT_HANDLERS = {
    "inquiry_overview": inquiry_overview,
    "detect_inquiry_surge": detect_inquiry_surge,
}
