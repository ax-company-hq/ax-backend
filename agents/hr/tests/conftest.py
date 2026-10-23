"""hr 테스트 공통 부품: 고정 시계와 가상 시드를 가진 `Services`를 테스트마다 새로 만든다."""

from __future__ import annotations

import pytest

from hr_agent.config import HrSettings, ManualClock
from hr_agent.services import Services, build_services, set_services


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setenv("HR_MOCK", "1")


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def svc(clock: ManualClock) -> Services:
    services = build_services(HrSettings(mock=True), clock=clock)
    set_services(services)
    yield services
    set_services(None)
    services.store.close()
