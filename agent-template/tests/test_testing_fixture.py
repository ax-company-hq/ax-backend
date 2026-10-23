"""`ax_agent_template.testing.reset_state`가 테스트 사이 상태를 비운다."""

import pytest

from ax_agent_template import config
from ax_agent_template.testing import clear_state


def test_clear_state_drops_process_agent_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_NAME", raising=False)
    config.set_agent_name("leaky")
    assert config.get_agent_name() == "leaky"

    clear_state()

    with pytest.raises(RuntimeError):
        config.get_agent_name()


def test_name_is_empty_at_test_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_NAME", raising=False)
    with pytest.raises(RuntimeError):
        config.get_agent_name()
