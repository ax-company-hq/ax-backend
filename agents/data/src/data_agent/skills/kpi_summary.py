"""kpi_summary skill (DSH-04 지원, SCN-L 학습 효과, 이슈 #61).

공통 로그 저장소를 읽기 전용으로 조회해 사건별 KPI를 코드로 계산한다(`metrics/kpi.py`).
LLM은 1차·2차 사건 MTTR 비교 설명 한 줄만 쓰며, 숫자가 어긋나면 고정 템플릿으로 대체한다.
계약 출력(`KpiSummaryOutput`)에 설명 필드가 없어 설명은 `evidence.data_summary` 첫 줄에 둔다.
입력은 context_ids 또는 기간이며, 둘 다 없으면 전체를 본다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from ax_agent_template.llm import LLMClient
from ax_contracts.data_access.common_log import CommonLogQuery
from ax_contracts.schemas import Evidence
from ax_contracts.schemas.data import IncidentKpi, KpiSummaryInput, KpiSummaryOutput
from data_agent.clients.log_client import LogSource, get_log_source
from data_agent.explain import explain
from data_agent.metrics.kpi import compute_kpi

QUERY_LIMIT = 5000  # 계약 상한. 넘치면 최근 기록만 오고 truncated다


@dataclass(frozen=True)
class KpiResult:
    output: KpiSummaryOutput
    evidence: Evidence
    fallback: bool
    summary: str | None


def _minutes(seconds: int) -> int | float:
    value = round(seconds / 60, 1)
    return int(value) if value == int(value) else value


def _comparison_facts(incidents: list[IncidentKpi]) -> dict[str, int | float | str] | None:
    """MTTR이 있는 사건 중 가장 이른 것(1차)과 가장 늦은 것(2차)을 비교한다. 2건 미만이면 None."""
    timed = [(i, i.mttr_seconds) for i in incidents if i.mttr_seconds is not None]
    if len(timed) < 2:
        return None
    (first, first_mttr), (second, second_mttr) = timed[0], timed[-1]
    return {
        "first": "1차",
        "second": "2차",
        "first_mttr_minutes": _minutes(first_mttr),
        "second_mttr_minutes": _minutes(second_mttr),
        "diff_minutes": _minutes(first_mttr - second_mttr),
        "first_used_memory": "예" if first.used_memory else "아니오",
        "second_used_memory": "예" if second.used_memory else "아니오",
    }


async def summarize_kpi(
    data: KpiSummaryInput,
    tenant_id: str,
    llm: LLMClient,
    env: Mapping[str, str] | None = None,
    source: LogSource | None = None,
) -> KpiResult:
    client = source or (get_log_source(env) if env is not None else get_log_source())
    query = CommonLogQuery(
        context_ids=data.context_ids,
        from_=data.period_from,
        to=data.period_to,
        limit=QUERY_LIMIT,
        tenant_id=tenant_id,
    )
    result = await client.query(query)
    computed = compute_kpi(result.records, data.context_ids)
    output = computed.output

    summary: str | None = None
    fallback = False
    facts = _comparison_facts(output.incidents)
    notes = list(computed.notes)
    if facts is not None:
        explanation = await explain("kpi", facts, llm)
        summary, fallback = explanation.summary, explanation.fallback
    elif output.incidents:
        notes.append("1차·2차 비교 확인할 수 없음: MTTR이 있는 사건 2건 미만")
    if not result.records:
        notes.append("확인할 수 없음: 조건에 맞는 공통 로그 0건")
    if result.truncated:
        notes.append(f"조회 한도 {QUERY_LIMIT}건에 걸려 최근 기록만 반영됨(truncated)")

    condition = (
        f"common_log from={data.period_from.isoformat() if data.period_from else 'all'} "
        f"to={data.period_to.isoformat() if data.period_to else 'all'} "
        f"context_ids={','.join(data.context_ids) or 'ctx_inc_*'}"
    )
    lines = [summary] if summary else []
    lines.append(f"로그 {len(result.records)}건, 사건 {len(output.incidents)}건")
    lines += notes
    evidence = Evidence(
        queries=[condition],
        data_summary="\n".join(lines),
        collected_at=datetime.now(UTC),
        ref_ids=[i.context_id for i in output.incidents],
    )
    return KpiResult(output=output, evidence=evidence, fallback=fallback, summary=summary)
