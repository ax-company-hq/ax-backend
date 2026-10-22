import pytest

from ax_agent_template import config


@pytest.fixture(autouse=True)
def _own_agent_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """모든 support 테스트가 자기 에이전트 이름으로 돈다.

    `create_agent_app`은 에이전트 이름을 전역(`config._agent_name`)에 저장한다. `uv run pytest`로
    전체를 돌리면 다른 서비스 테스트가 수집될 때 이름이 마지막에 만든 앱(예: safety) 것으로
    바뀌어 남는다. 그러면 `SUPPORT_MOCK`·토큰 같은 `<이름>` 접두사 환경변수를 못 찾아, 이 폴더만
    따로 돌릴 때는 통과하던 테스트가 전체 실행에서만 실패한다 (UNAUTHORIZED).
    """
    monkeypatch.setattr(config, "_agent_name", "support")
