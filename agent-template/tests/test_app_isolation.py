"""앱을 여러 개 만들어도 이름이 섞이지 않는다 (전역 `_agent_name` 누수 방지)."""

from fastapi.testclient import TestClient

from ax_agent_template import config
from ax_agent_template.server import create_agent_app
from ax_contracts.schemas import AgentCard, AgentRequest, AgentResult, TaskStatus


def _card(name: str) -> AgentCard:
    return AgentCard.model_validate(
        {
            "name": name,
            "owner": "test",
            "description": "격리 테스트용 에이전트",
            "url": f"http://{name}:9999/a2a",
            "skills": [
                {
                    "id": "whoami",
                    "description": "자기 이름과 토큰을 돌려준다. 상태를 바꾸지 않는다.",
                    "input_schema": "x",
                    "output_schema": "x",
                    "grade": "read",
                    "allowed_requesters": ["orchestrator"],
                    "priority": "optional",
                }
            ],
        }
    )


async def _whoami(request: AgentRequest) -> AgentResult:
    name = config.get_agent_name()
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.COMPLETED,
        output={"name": name, "mock": config.is_mock(), "token": config.agent_token(name)},
    )


def _body(request_body) -> dict:
    return request_body("whoami")


def test_each_app_reads_its_own_prefixed_env(monkeypatch, real_mode, request_body) -> None:
    monkeypatch.setenv("ISO_ALPHA_MOCK", "1")  # alpha만 목 모드
    monkeypatch.setenv("ISO_BETA_TOKEN", "beta-secret")
    monkeypatch.delenv("ISO_BETA_MOCK", raising=False)

    alpha = TestClient(create_agent_app(_card("iso_alpha"), {"whoami": _whoami}))
    beta = TestClient(create_agent_app(_card("iso_beta"), {"whoami": _whoami}))
    # 마지막에 만든 앱은 beta이므로 전역 이름은 beta다. alpha 요청이 이를 따라가면 안 된다.
    assert config.get_agent_name() == "iso_beta"

    alpha_result = alpha.post("/a2a", json=_body(request_body)).json()
    assert alpha_result["status"] == "completed"
    assert alpha_result["output"] == {
        "name": "iso_alpha",
        "mock": True,
        "token": "mock:iso_alpha",
    }

    # beta는 목 모드가 아니므로 mock 토큰을 거부한다.
    beta_result = beta.post("/a2a", json=_body(request_body)).json()
    assert beta_result["status"] == "rejected"
    assert beta_result["outcome"] == "UNAUTHORIZED"


def test_app_holds_its_own_name() -> None:
    alpha = create_agent_app(_card("iso_alpha"), {"whoami": _whoami})
    beta = create_agent_app(_card("iso_beta"), {"whoami": _whoami})
    assert (alpha.state.agent_name, beta.state.agent_name) == ("iso_alpha", "iso_beta")


def test_reset_state_clears_default_name() -> None:
    # autouse `reset_state`가 이전 테스트의 이름을 지웠다.
    assert config._agent_name is None
    create_agent_app(_card("iso_alpha"), {"whoami": _whoami})
    assert config.get_agent_name() == "iso_alpha"
    from ax_agent_template.testing import clear_state

    clear_state()
    assert config._agent_name is None


def test_request_scope_does_not_leak_after_request(monkeypatch, real_mode, request_body) -> None:
    monkeypatch.setenv("ISO_ALPHA_MOCK", "1")
    alpha = TestClient(create_agent_app(_card("iso_alpha"), {"whoami": _whoami}))
    create_agent_app(_card("iso_beta"), {"whoami": _whoami})

    alpha.post("/a2a", json=_body(request_body))
    assert config.get_agent_name() == "iso_beta"  # 요청이 끝나면 전역 기본 이름 그대로
    assert config.is_mock() is False
