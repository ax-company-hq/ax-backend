"""프로세스 안의 상태 하나: 배포 이력, 사건별 진단·롤백 기록, 바깥 호출 클라이언트.

v0은 DB 없이 프로세스 메모리에만 둔다(재시작하면 배포 이력 파일 상태로 돌아간다).
테스트는 `set_runtime`으로 시계·대기·클라이언트를 바꾼다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from infra_agent.clients import DataClient, ShopClient, default_data, default_shop
from infra_agent.deployments import DeploymentHistory
from infra_agent.diagnosis import Diagnosis


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Runtime:
    history: DeploymentHistory = field(default_factory=DeploymentHistory.load)
    shop: ShopClient = field(default_factory=default_shop)
    data: DataClient = field(default_factory=default_data)
    clock: Callable[[], datetime] = _now
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep
    diagnoses: dict[str, Diagnosis] = field(default_factory=dict)  # context_id → 마지막 진단
    rollbacks: dict[str, dict[str, Any]] = field(default_factory=dict)  # idempotency_key → 결과

    def executed_rollbacks(self, context_id: str) -> int:
        return sum(
            1
            for r in self.rollbacks.values()
            if r["context_id"] == context_id and r["status"] == "executed"
        )


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = Runtime()
    return _runtime


def set_runtime(runtime: Runtime) -> Runtime:
    global _runtime
    _runtime = runtime
    return runtime
