"""data 에이전트 skill 핸들러.

- impact_analysis, anomaly_detection은 읽기 전용(grade read)이라 `execute()`를 거치지 않는다.
- fraud_score, kpi_summary는 카드에는 있지만 아직 구현 전이다. 템플릿이 카드와 핸들러가
  같아야 시작하므로 `not_implemented` 실패를 돌려주는 자리 핸들러를 둔다(후속 PR에서 교체).
- 외부 입력(`request.untrusted`)은 읽지 않는다.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ValidationError

from ax_agent_template.llm import LLMClient
from ax_contracts.schemas import AgentRequest, AgentResult, TaskStatus
from ax_contracts.schemas.data import AnomalyDetectionInput, ImpactAnalysisInput
from data_agent.skills.anomaly_detection import detect_anomalies, get_metric_source
from data_agent.skills.impact_analysis import analyze_impact


def get_llm() -> LLMClient:
    """설명 문장용 LLM. 테스트는 이 함수를 바꿔 끼운다. 미설정이면 explain이 템플릿으로 대체한다."""
    return LLMClient("data")


def _result(request: AgentRequest, status: TaskStatus, **kwargs) -> AgentResult:
    return AgentResult(meta=request.meta, status=status, **kwargs)


def _parse[T: BaseModel](request: AgentRequest, model: type[T]) -> T | AgentResult:
    try:
        return model.model_validate(request.input)
    except ValidationError as error:
        return _result(
            request,
            TaskStatus.FAILED,
            outcome="invalid_input",
            output={
                "errors": [f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in error.errors()]
            },
        )


def _unavailable(request: AgentRequest, error: NotImplementedError) -> AgentResult:
    return _result(
        request, TaskStatus.FAILED, outcome="unavailable", output={"message": str(error)}
    )


async def impact_analysis(request: AgentRequest) -> AgentResult:
    data = _parse(request, ImpactAnalysisInput)
    if isinstance(data, AgentResult):
        return data
    if data.window_from > data.window_to:
        return _result(request, TaskStatus.FAILED, outcome="invalid_input")
    try:
        result = await analyze_impact(data, request.meta.tenant_id, get_llm(), os.environ)
    except NotImplementedError as error:
        return _unavailable(request, error)
    return _result(
        request,
        TaskStatus.COMPLETED,
        output=result.output.model_dump(mode="json"),
        evidence=result.evidence,
        fallback=result.fallback,
    )


async def anomaly_detection(request: AgentRequest) -> AgentResult:
    data = _parse(request, AnomalyDetectionInput)
    if isinstance(data, AgentResult):
        return data
    if data.window_from >= data.window_to:
        return _result(request, TaskStatus.FAILED, outcome="invalid_input")
    try:
        source = get_metric_source(os.environ)
    except NotImplementedError as error:
        return _unavailable(request, error)
    result = await detect_anomalies(data, source, get_llm())
    return _result(
        request,
        TaskStatus.COMPLETED,
        output=result.output.model_dump(mode="json"),
        evidence=result.evidence,
        fallback=result.fallback,
    )


def _not_implemented(skill: str):
    async def handler(request: AgentRequest) -> AgentResult:
        return _result(
            request,
            TaskStatus.FAILED,
            outcome="not_implemented",
            output={"message": f"{skill}은(는) 아직 구현되지 않았다"},
        )

    return handler


HANDLERS = {
    "impact_analysis": impact_analysis,
    "anomaly_detection": anomaly_detection,
    "fraud_score": _not_implemented("fraud_score"),  # TODO: 후속 PR
    "kpi_summary": _not_implemented("kpi_summary"),  # TODO: 후속 PR
}
