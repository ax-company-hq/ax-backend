"""data 테스트 공통 설정."""

import pytest

from data_agent import demo_anchor


@pytest.fixture(autouse=True)
def _reset_demo_anchor():
    """목 데모 anchor는 프로세스 안 상태라 테스트마다 비운다."""
    demo_anchor.reset_demo_anchor()
    yield
    demo_anchor.reset_demo_anchor()
