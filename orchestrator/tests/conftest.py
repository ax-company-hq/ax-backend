import pytest

from ax_orchestrator.mock.harness import Harness


@pytest.fixture
def harness() -> Harness:
    return Harness()
