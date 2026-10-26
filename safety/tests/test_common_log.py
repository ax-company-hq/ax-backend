"""안전 관문 판정이 공통 로그 저장소(COM-08)에 남는지 본다.

대시보드 "안전 관문 로그"와 data `kpi_summary`(grade_counts·blocked_counts)는 공통 로그만 읽는다.
계약 `LogKind`에 없는 종류(예전 `safety_decision`)는 표준 로거에만 남고 저장소에서는 버려진다.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from ax_agent_template.common_log import SqliteLogStore, set_store
from ax_contracts.data_access.common_log import CommonLogQuery, LogRecord

MARKETING = {"Authorization": "Bearer mock:marketing"}


@pytest.fixture
def log_store() -> Iterator[SqliteLogStore]:
    store = SqliteLogStore(":memory:")
    set_store(store)
    yield store
    set_store(None)


def _records(store: SqliteLogStore) -> list[LogRecord]:
    return store.query(CommonLogQuery()).records


def test_decision_is_written_to_common_log(client: Any, coupon: Any, log_store: Any) -> None:
    response = client.post(
        "/api/safety/executions", json=coupon().model_dump(mode="json"), headers=MARKETING
    )
    assert response.status_code == 200
    data = response.json()["data"]

    decided = [r for r in _records(log_store) if r.kind == "execute_decided"]
    assert len(decided) == 1
    record = decided[0]
    assert record.source == "safety"
    assert record.context_id == "ctx_inc_0001"
    assert (record.grade, record.requester, record.skill) == ("A", "marketing", "issue_coupon")
    assert record.task_status == "executed"
    assert record.payload["execution_id"] == data["execution_id"]
    assert record.payload["status"] == "executed"
    assert "params" not in record.payload  # 요청 원문은 넣지 않는다


def test_held_decision_keeps_grade_c(client: Any, coupon: Any, log_store: Any) -> None:
    body = coupon(20, 20_000, purpose_code="campaign", key="big").model_dump(mode="json")
    client.post("/api/safety/executions", json=body, headers=MARKETING)
    (record,) = [r for r in _records(log_store) if r.kind == "execute_decided"]
    assert (record.grade, record.task_status) == ("C", "held")
    assert record.payload["review_id"]


@pytest.mark.parametrize(
    ("headers", "reason"),
    [({}, "UNAUTHORIZED"), ({"Authorization": "Bearer mock:support"}, "FORBIDDEN")],
)
def test_auth_refusal_is_logged_as_rejected(
    client: Any, coupon: Any, log_store: Any, headers: dict, reason: str
) -> None:
    client.post("/api/safety/executions", json=coupon().model_dump(mode="json"), headers=headers)
    records = _records(log_store)
    assert [r.kind for r in records] == ["rejected"]
    assert records[0].source == "safety"
    assert records[0].payload["reason_code"] == reason
