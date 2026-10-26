"""공통 로그 저장소 읽기 클라이언트 (kpi_summary가 사용).

쓰기는 agent-template의 `log_event`만 한다. 여기서는 계약(`ax_contracts.data_access.common_log`)
형식의 읽기 조회만 한다.
- `DATA_MOCK=1`: 시드 로그(`seed_logs.py`)를 조건으로 걸러 돌려준다.
- 그 밖: agent-template 공통 로그 저장소(`AX_COMMON_LOG_DB`)를 읽는다. 비어 있으면 쓸 수 없다.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from typing import Protocol

from ax_agent_template import common_log
from ax_contracts.data_access.common_log import CommonLogQuery, CommonLogResult
from data_agent.seed_logs import SEED_LOGS


class LogSource(Protocol):
    async def query(self, query: CommonLogQuery) -> CommonLogResult: ...


class MockLogSource:
    """시드 로그를 조건(context_ids, 기간 양끝 포함, kinds, tenant)으로 걸러 돌려준다.

    저장소와 같게 결과는 시각 오름차순이고, 넘치면 최근 `limit`건만 남기고 `truncated`다.
    """

    async def query(self, query: CommonLogQuery) -> CommonLogResult:
        records = [
            r
            for r in SEED_LOGS
            if r.tenant_id == query.tenant_id
            and (not query.context_ids or r.context_id in query.context_ids)
            and (query.from_ is None or r.occurred_at >= query.from_)
            and (query.to is None or r.occurred_at <= query.to)
            and (not query.kinds or r.kind in query.kinds)
        ]
        records.sort(key=lambda r: (r.occurred_at, r.log_id))
        truncated = len(records) > query.limit
        return CommonLogResult(records=records[-query.limit :], truncated=truncated)


class CommonLogSource:
    """agent-template 공통 로그 저장소의 읽기 조회(`LogStore.query`)만 쓴다."""

    def __init__(self, store: common_log.LogStore) -> None:
        self.store = store

    async def query(self, query: CommonLogQuery) -> CommonLogResult:
        return await asyncio.to_thread(self.store.query, query)


def get_log_source(env: Mapping[str, str] = os.environ) -> LogSource:
    """`DATA_MOCK=1`이면 목, 아니면 공통 로그 저장소. 저장소 설정이 없으면 NotImplementedError."""
    if env.get("DATA_MOCK") == "1":
        return MockLogSource()
    store = common_log.get_store()
    if store is None:
        raise NotImplementedError(
            f"{common_log.DB_ENV}가 비어 있어 공통 로그 저장소를 읽을 수 없다"
        )
    return CommonLogSource(store)
