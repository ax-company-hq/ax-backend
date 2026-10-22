import asyncio
from datetime import UTC, datetime, timedelta

from ax_contracts.schemas import AgentRequest, AgentResult, RequestMeta, TaskStatus
from ax_contracts.schemas.planning import FindRelatedPrsOutput, RelatedPr
from data_agent.clients.planning_client import find_related_prs, prs_to_evidence

FORBIDDEN = ("때문", "원인", "탓", "야기", "초래")
W_FROM = datetime(2026, 10, 21, 5, 0, tzinfo=UTC)
W_TO = datetime(2026, 10, 21, 5, 30, tzinfo=UTC)


def _meta() -> RequestMeta:
    return RequestMeta(
        context_id="ctx_inc_1",
        token="mock:data",
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )


def _output() -> FindRelatedPrsOutput:
    return FindRelatedPrsOutput(
        candidates=[
            RelatedPr(
                pr_number=42,
                title="결제 모듈 변경",
                merged_at=datetime(2026, 10, 21, 4, 40, tzinfo=UTC),
            ),
            RelatedPr(
                pr_number=7,
                title="재고 조회 개선",
                merged_at=datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
            ),
        ]
    )


def test_success_to_evidence() -> None:
    seen: list[AgentRequest] = []

    async def send(target: str, req: AgentRequest) -> AgentResult:
        seen.append(req)
        return AgentResult(
            meta=req.meta, status=TaskStatus.COMPLETED, output=_output().model_dump(mode="json")
        )

    out = asyncio.run(find_related_prs(W_FROM, W_TO, "payment", _meta(), send=send))
    assert out is not None
    assert seen[0].skill == "find_related_prs"
    assert seen[0].input["component"] == "payment"
    evidence = prs_to_evidence(out)
    assert evidence.ref_ids == ["pr:42", "pr:7"]
    assert "PR #42(병합 2026-10-21T04:40:00Z) 이후 구간" in evidence.data_summary


def test_timeout_returns_none() -> None:
    async def send(target: str, req: AgentRequest) -> AgentResult:
        await asyncio.sleep(10)
        raise AssertionError("도달하면 안 된다")

    out = asyncio.run(
        find_related_prs(W_FROM, W_TO, None, _meta(), send=send, timeout_seconds=0.05)
    )
    assert out is None


def test_rejected_and_error_return_none() -> None:
    async def rejected(target: str, req: AgentRequest) -> AgentResult:
        return AgentResult(meta=req.meta, status=TaskStatus.REJECTED, outcome="FORBIDDEN_SKILL")

    async def boom(target: str, req: AgentRequest) -> AgentResult:
        raise RuntimeError("연결 실패")

    assert asyncio.run(find_related_prs(W_FROM, W_TO, None, _meta(), send=rejected)) is None
    assert asyncio.run(find_related_prs(W_FROM, W_TO, None, _meta(), send=boom)) is None


def test_no_causal_words() -> None:
    for output in (_output(), FindRelatedPrsOutput()):
        evidence = prs_to_evidence(output)
        assert not any(word in evidence.data_summary for word in FORBIDDEN)
