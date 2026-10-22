import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from ax_contracts.schemas import Evidence, ExecuteDecision, RequestMeta
from planning_agent import generate_pr as mod
from planning_agent.generate_pr import ChangedFile


def test_generate_pr_sends_change_size_to_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_execute(action, params, context, evidence):
        calls.append((action, params))
        return ExecuteDecision(execution_id="e1", grade="B", status="verifying")

    monkeypatch.setattr(mod, "execute", fake_execute)
    meta = RequestMeta(
        context_id="ctx_1",
        token="t",
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        idempotency_key="ctx_1:generate_pr:b1",
    )
    files = [
        ChangedFile("shop-api/a.py", 10, 5),
        ChangedFile("docs/b.md", 3, 0),
    ]
    asyncio.run(mod.generate_pr("t", "b1", files, meta, Evidence()))

    action, params = calls[0]
    assert action == "generate_pr"
    assert params["changed_lines"] == 18
    assert params["changed_files"] == 2
    assert params["changed_paths"] == ["docs/b.md", "shop-api/a.py"]
