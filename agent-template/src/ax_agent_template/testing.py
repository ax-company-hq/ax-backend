"""테스트 도우미: 내부 상태 초기화 픽스처.

부서 테스트는 conftest에서 `from ax_agent_template.testing import reset_state`로 가져온다.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ax_agent_template import common_log, config, events, execute


def clear_state() -> None:
    """에이전트 이름, 목 이벤트 버스·실행 카운터, 공통 로그 저장소를 처음 상태로 되돌린다."""
    config.clear_agent_name()
    events.reset_mock_bus()
    execute.reset_mock_counter()
    common_log.reset_store()


@pytest.fixture(autouse=True)
def reset_state() -> Iterator[None]:
    """매 테스트 시작·끝에 내부 상태를 초기화한다."""
    clear_state()
    yield
    clear_state()
