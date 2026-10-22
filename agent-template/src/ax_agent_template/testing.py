"""테스트 도우미. 부서 `conftest.py`에서 `from ax_agent_template.testing import reset_state`로 가져온다.

`reset_state`는 autouse 픽스처라 가져오기만 하면 모든 테스트 시작·끝에 내부 상태를 비운다.
한 프로세스에서 여러 부서 앱을 만들어도 이전 테스트의 이름·목 이벤트 버스·이관 기록이 남지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ax_agent_template import config, events, server


def clear_state() -> None:
    """에이전트 이름(프로세스 기본값), 목 이벤트 버스, 목 이관 기록을 초기화한다."""
    config.reset_agent_name()
    events.reset_mock_bus()
    server.MOCK_ESCALATIONS.clear()


@pytest.fixture(autouse=True)
def reset_state() -> Iterator[None]:
    clear_state()
    yield
    clear_state()
