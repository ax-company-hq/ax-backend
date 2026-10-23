"""부서별 소통 탭 조회 GET /api/dashboard/communications."""

import pytest
from fastapi.testclient import TestClient

from ax_contracts.data_access.common_log import CommonLogQuery, CommonLogResult
from ax_dashboard_api import communications
from ax_dashboard_api.main import app

URL = "/api/dashboard/communications"


@pytest.fixture(autouse=True)
def _reset():
    communications.set_reader(None)
    yield
    communications.set_reader(None)


def test_mock_lists_lines_with_code_fields_only(monkeypatch):
    monkeypatch.setenv("DASHBOARD_MOCK", "1")
    res = TestClient(app).get(URL)
    assert res.status_code == 200
    data = res.json()["data"]
    assert len(data) == 4
    assert set(data[0]) == {
        "log_id", "occurred_at", "from_agent", "to_agent", "skill",
        "outcome", "context_id", "execution_id",
    }
    assert (data[0]["from_agent"], data[0]["to_agent"]) == ("orchestrator", "support")


def test_dept_filter_and_outcomes(monkeypatch):
    monkeypatch.setenv("DASHBOARD_MOCK", "1")
    res = TestClient(app).get(URL, params={"dept": "finance"})
    (line,) = res.json()["data"]
    assert line["outcome"] == "held"
    assert line["execution_id"] == "exe_mock1"
    rejected = TestClient(app).get(URL, params={"dept": "marketing"}).json()["data"]
    assert rejected[0]["outcome"] == "rejected"


def test_context_id_goes_to_common_log_query(monkeypatch):
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    seen: list[CommonLogQuery] = []

    def reader(q: CommonLogQuery) -> CommonLogResult:
        seen.append(q)
        return CommonLogResult(truncated=True)

    communications.set_reader(reader)
    res = TestClient(app).get(URL, params={"context_id": "ctx_inc_1"})
    assert res.status_code == 200
    assert seen[0].context_ids == ["ctx_inc_1"]
    assert res.json()["meta"]["truncated"] is True


def test_not_connected_without_reader(monkeypatch):
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    res = TestClient(app).get(URL)
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "UPSTREAM_NOT_CONNECTED"
