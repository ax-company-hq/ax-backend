"""실제 모드에서 data `impact_analysis`를 부르는 모양: 계약(`ImpactAnalysisInput`)대로 보내는지.

목 모드 시드만으로는 data가 받는 입력 형식이 틀려도 통과해서,
실제 호출을 가짜 `call_agent`로 받아 확인한다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ax_contracts.schemas import (
    AgentRequest,
    AgentResult,
    ImpactAnalysisInput,
    RequestMeta,
    TaskStatus,
)
from finance_agent import sources
from finance_agent.sources import LiveSources, impact_input

META = RequestMeta(
    context_id="ctx_inc_0001", token="x", deadline=datetime.now(UTC) + timedelta(seconds=30)
)
WINDOW = {"window_from": "2026-10-22T05:00:00Z", "window_to": "2026-10-22T05:40:00Z"}
DATA_OUT = {
    "failed_order_count": 128,
    "affected_customer_count": 2,
    "affected_customer_ids": ["buyer_anon_1", "buyer_anon_2"],
    "damage_amount": 5_430_000,
    "summary": "결제 실패 128건",
}


@pytest.fixture
def data_calls(monkeypatch: pytest.MonkeyPatch) -> list[AgentRequest]:
    """data 호출을 가로채 계약 출력을 돌려준다. 받은 요청을 쌓아 둔다."""
    calls: list[AgentRequest] = []

    async def fake_call_agent(name: str, request: AgentRequest) -> AgentResult:
        assert name == "data"
        calls.append(request)
        return AgentResult(meta=request.meta, status=TaskStatus.COMPLETED, output=dict(DATA_OUT))

    monkeypatch.setattr(sources, "call_agent", fake_call_agent)
    return calls


@pytest.mark.asyncio
async def test_impact_request_matches_data_contract(data_calls: list[AgentRequest]) -> None:
    impact = await LiveSources().impact(META, "ctx_inc_0001", {**WINDOW, "category": "book"})
    assert impact is not None
    assert (impact.failed_orders, impact.affected_customers, impact.lost_amount) == (
        128,
        2,
        5_430_000,
    )
    sent = data_calls[0]
    assert sent.skill == "impact_analysis"
    ImpactAnalysisInput.model_validate(sent.input)  # data가 그대로 받을 수 있는 입력
    assert set(sent.input) == {"window_from", "window_to", "category"}
    assert sent.meta.context_id == "ctx_inc_0001"  # 사건 번호는 입력이 아니라 meta로


@pytest.mark.asyncio
async def test_without_window_finance_does_not_ask_data(data_calls: list[AgentRequest]) -> None:
    assert await LiveSources().impact(META, "ctx_inc_0001", None) is None
    assert impact_input({"from": WINDOW["window_from"], "to": WINDOW["window_to"]}) is None
    assert data_calls == []


@pytest.mark.asyncio
async def test_data_output_outside_contract_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def wrong_output(name: str, request: AgentRequest) -> AgentResult:
        out: dict[str, Any] = {**DATA_OUT, "affected_customer_count": 5}  # id 수와 다름
        return AgentResult(meta=request.meta, status=TaskStatus.COMPLETED, output=out)

    monkeypatch.setattr(sources, "call_agent", wrong_output)
    assert await LiveSources().impact(META, "ctx_inc_0001", WINDOW) is None
