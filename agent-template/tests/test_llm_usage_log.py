import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ax_agent_template import llm_usage
from ax_agent_template.llm import LLMClient, LLMSettings, MockLLMClient
from ax_agent_template.llm_usage import SqliteUsageStore
from ax_contracts.data_access.llm_usage import LlmUsageQuery


@pytest.fixture
def store() -> Iterator[SqliteUsageStore]:
    s = SqliteUsageStore(":memory:")
    llm_usage.set_store(s)
    yield s
    llm_usage.reset_store()
    s.close()


def _all(store: SqliteUsageStore, **query):
    return store.query(LlmUsageQuery.model_validate(query)).records


@pytest.mark.asyncio
async def test_mock_embed_records_usage_with_zero_tokens(store: SqliteUsageStore) -> None:
    await MockLLMClient(dept="memory").embed(["고객 원문"], context_id="ctx-1")
    [record] = _all(store)
    assert record.context_id == "ctx-1"
    assert record.tenant_id == "shop-demo"
    assert (record.source, record.department, record.model) == ("memory", "memory", "mock")
    assert (record.input_tokens, record.output_tokens, record.success) == (0, 0, True)
    assert "고객 원문" not in json.dumps(record.model_dump(mode="json"), ensure_ascii=False)


@pytest.mark.asyncio
async def test_real_embed_records_token_counts_per_context(
    monkeypatch: pytest.MonkeyPatch, store: SqliteUsageStore
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [1.0]}],
                "usage": {"prompt_tokens": 7, "total_tokens": 7},
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler))
    )
    client = LLMClient("memory", LLMSettings(base_url="http://llm/v1", embed_model="m"))
    await client.embed(["a"], context_id="ctx-2")
    await client.embed(["b"], context_id="ctx-2")
    records = _all(store, context_ids=["ctx-2"])
    assert len(records) == 2
    assert sum(r.input_tokens for r in records) == 14
    assert {r.context_id for r in records} == {"ctx-2"}


@pytest.mark.asyncio
async def test_failed_real_embed_records_failure(
    monkeypatch: pytest.MonkeyPatch, store: SqliteUsageStore
) -> None:
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    client = LLMClient("memory", LLMSettings(base_url="http://llm/v1", embed_model="m"))
    with pytest.raises(httpx.HTTPStatusError):
        await client.embed(["a"], context_id="ctx-3")
    [record] = _all(store)
    assert record.success is False


def test_query_filters_by_department_context_and_time(store: SqliteUsageStore) -> None:
    for dept, ctx in [("hr", "ctx-a"), ("finance", "ctx-a"), ("hr", None)]:
        llm_usage.record_call(
            source=dept,
            department=dept,
            model="m",
            input_tokens=1,
            output_tokens=2,
            success=True,
            context_id=ctx,
        )
    assert {r.department for r in _all(store, context_ids=["ctx-a"])} == {"hr", "finance"}
    assert len(_all(store, departments=["hr"])) == 2
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert _all(store, **{"from": future}) == []
    limited = store.query(LlmUsageQuery.model_validate({"limit": 2}))
    assert len(limited.records) == 2 and limited.truncated is True


def test_without_a_store_nothing_is_written_and_nothing_breaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm_usage.reset_store()
    monkeypatch.delenv(llm_usage.DB_ENV, raising=False)
    assert llm_usage.get_store() is None
    llm_usage.record_call(
        source="hr", department="hr", model=None, input_tokens=0, output_tokens=0, success=True
    )


def test_a_failing_store_does_not_stop_the_caller() -> None:
    class Broken:
        def write(self, record) -> None:
            raise RuntimeError("disk full")

    llm_usage.set_store(Broken())  # type: ignore[arg-type]
    try:
        llm_usage.record_call(
            source="hr", department="hr", model="m", input_tokens=1, output_tokens=1, success=True
        )
    finally:
        llm_usage.reset_store()


def test_file_store_is_shared_between_connections(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = str(tmp_path / "usage.db")
    monkeypatch.setenv(llm_usage.DB_ENV, path)
    llm_usage.reset_store()
    llm_usage.record_call(
        source="hr", department="hr", model="m", input_tokens=3, output_tokens=4, success=True
    )
    reader = SqliteUsageStore(path)
    [record] = reader.query(LlmUsageQuery()).records
    assert (record.input_tokens, record.output_tokens) == (3, 4)
    reader.close()
    llm_usage.reset_store()
