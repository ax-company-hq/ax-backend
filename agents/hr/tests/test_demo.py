import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import RecordCase
from hr_agent.demo.server import demo_app, load_scenarios


@pytest.fixture
def client() -> TestClient:
    return TestClient(demo_app)


def test_every_scenario_input_is_a_valid_record_case() -> None:
    scenarios = load_scenarios()
    assert len(scenarios) >= 7
    for name, scenario in scenarios.items():
        assert RecordCase.model_validate(scenario["input"]), name
        assert scenario["expect"]["status"] in ("completed", "rejected"), name


def test_demo_page_and_scenario_list(client: TestClient) -> None:
    assert "인사 에이전트 데모" in client.get("/").text
    names = [s["name"] for s in client.get("/api/scenarios").json()]
    assert names == sorted(names)
    assert "01_incident_payment_outage" in names


def test_run_all_matches_every_expectation(client: TestClient) -> None:
    body = client.post("/api/run-all").json()
    assert body["passed"] == body["total"], [
        (r["name"], r["passed"], r["deterministic"]) for r in body["runs"]
    ]


def test_forbidden_requester_is_rejected_through_the_real_pipeline(client: TestClient) -> None:
    run = client.post("/api/run", json={"name": "07_forbidden_requester"}).json()
    assert run["result"]["status"] == "rejected"
    assert run["result"]["outcome"] == "FORBIDDEN_SKILL"


def test_unknown_scenario_is_404(client: TestClient) -> None:
    assert client.post("/api/run", json={"name": "nope"}).status_code == 404
