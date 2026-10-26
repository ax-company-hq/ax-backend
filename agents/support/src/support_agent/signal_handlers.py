"""고객 이탈·재문의 위험 신호 skill 핸들러 (읽기 전용).

상태를 바꾸지 않으므로 `execute()`를 거치지 않는다. 점수는 `customer_signals`가 코드로 센다.
고객 이름과 문의 원문은 읽지도 돌려주지도 않는다 (구매자 가명 ID와 건수만).
"""

from __future__ import annotations

from datetime import UTC, datetime

from ax_contracts.schemas import AgentRequest, AgentResult, Evidence
from support_agent import buyer_ids, customer_signals
from support_agent import claim_handlers as claim_h
from support_agent import handlers as h
from support_agent.signal_schemas import (
    AtRiskCustomers,
    CustomerSignalsInput,
    ListAtRiskInput,
)


def now() -> datetime:
    """테스트에서 바꿀 수 있다."""
    return datetime.now(UTC)


async def customer_signals_skill(request: AgentRequest) -> AgentResult:
    parsed = h._parse(request, CustomerSignalsInput)
    if isinstance(parsed, AgentResult):
        return parsed
    buyer = buyer_ids.normalize(parsed.buyer_id)
    groups = customer_signals.group_by_buyer(h.STORE.all(), claim_h.CLAIMS.list())
    conversations, claims = groups.get(buyer, ([], []))
    result = customer_signals.compute(buyer, conversations, claims, now())
    evidence = Evidence(
        queries=["inquiry_store", "claim_store"],
        ref_ids=[f"inquiry:{c.id}" for c in conversations[:20]]
        + [f"claim:{c.id}" for c in claims[:20]],
        data_summary=f"{buyer}: {result.level} ({result.score}점)",
    )
    outcome = result.level if conversations or claims else "no_history"
    return h._done(request, result.model_dump(mode="json"), outcome=outcome, evidence=evidence)


async def list_at_risk_customers(request: AgentRequest) -> AgentResult:
    parsed = h._parse(request, ListAtRiskInput)
    if isinstance(parsed, AgentResult):
        return parsed
    at = now()
    found = customer_signals.at_risk(
        h.STORE.all(), claim_h.CLAIMS.list(), at, min_level=parsed.min_level
    )
    report = AtRiskCustomers(
        evaluated_at=at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        min_level=parsed.min_level,
        total=len(found),
        items=found[: parsed.limit],
    )
    evidence = Evidence(
        queries=["inquiry_store", "claim_store"],
        data_summary=f"{parsed.min_level} 이상 {len(found)}명",
    )
    return h._done(request, report.model_dump(mode="json"), evidence=evidence)


SIGNAL_HANDLERS = {
    "customer_signals": customer_signals_skill,
    "list_at_risk_customers": list_at_risk_customers,
}
