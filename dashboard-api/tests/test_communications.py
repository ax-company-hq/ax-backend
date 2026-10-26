"""부서별 소통 탭 조회 GET /api/dashboard/communications."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from ax_contracts.data_access.common_log import CommonLogQuery, CommonLogResult, LogRecord
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
        "log_id",
        "occurred_at",
        "from_agent",
        "to_agent",
        "skill",
        "outcome",
        "context_id",
        "execution_id",
        "kind",
        "event_type",
    }
    assert (data[0]["kind"], data[0]["event_type"]) == ("message", None)
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


def test_escalated_records_are_read_and_shown_as_needs_human(monkeypatch):
    from datetime import UTC, datetime

    from ax_contracts.data_access.common_log import LogRecord

    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    seen: list[CommonLogQuery] = []
    record = LogRecord.model_validate(
        {
            "log_id": "log_esc_1",
            "context_id": "ctx_inc_1",
            "occurred_at": datetime(2026, 10, 22, 9, 0, tzinfo=UTC),
            "source": "support",
            "kind": "escalated",
            "skill": "create_claim",
            "requester": "orchestrator",
        }
    )

    def reader(q: CommonLogQuery) -> CommonLogResult:
        seen.append(q)
        return CommonLogResult(records=[record])

    communications.set_reader(reader)
    res = TestClient(app).get(URL)
    assert "escalated" in seen[0].kinds
    (line,) = res.json()["data"]
    assert line["outcome"] == "needs_human"


def _record(n: int, **fields):
    from datetime import UTC, datetime

    from ax_contracts.data_access.common_log import LogRecord

    return LogRecord.model_validate(
        {
            "log_id": f"log_t{n}",
            "context_id": "ctx_inc_1",
            "occurred_at": datetime(2026, 10, 22, 9, n, tzinfo=UTC),
            "source": "support",
            "requester": "orchestrator",
            **fields,
        }
    )


def test_failed_and_canceled_results_are_not_shown_as_rejected(monkeypatch):
    """정책 거절(rejected)과 실행 오류·시간 초과(failed)는 다른 결과로 보인다."""
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    records = [
        _record(1, kind="result_sent", skill="a", task_status="failed"),
        _record(2, kind="result_sent", skill="b", task_status="canceled"),
        _record(3, kind="result_sent", skill="c", task_status="rejected"),
        _record(4, kind="rejected", skill="d"),
    ]
    communications.set_reader(lambda q: CommonLogResult(records=records))
    outcomes = [x["outcome"] for x in TestClient(app).get(URL).json()["data"]]
    assert outcomes == ["failed", "failed", "rejected", "rejected"]


def test_it_reads_the_real_common_log_file(monkeypatch, tmp_path):
    """`AX_COMMON_LOG_DB`가 있으면 reader를 꽂지 않아도 그 파일의 기록을 한 줄로 보여 준다."""
    from ax_agent_template import common_log

    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    monkeypatch.setenv("AX_COMMON_LOG_DB", str(tmp_path / "common.db"))
    common_log.reset_store()
    try:
        store = common_log.get_store()
        assert store is not None
        store.write(_record(1, kind="request_received", skill="get_claim", task_status="working"))
        store.write(
            _record(
                2,
                kind="result_sent",
                skill="get_claim",
                task_status="completed",
                payload={"execution_id": "exe_9", "output": "고객 원문"},
            )
        )
        store.write(_record(3, kind="event_published", skill="x"))  # 대화가 아니라서 뺀다
        res = TestClient(app).get(URL, params={"context_id": "ctx_inc_1"})
        assert res.status_code == 200
        lines = res.json()["data"]
        assert [(x["from_agent"], x["to_agent"], x["outcome"]) for x in lines] == [
            ("orchestrator", "support", "in_progress"),
            ("support", "orchestrator", "completed"),
        ]
        assert lines[1]["execution_id"] == "exe_9" and "고객 원문" not in str(lines)
        assert TestClient(app).get(URL, params={"dept": "finance"}).json()["data"] == []
    finally:
        common_log.reset_store()


def test_dejafix_can_be_asked_by_any_of_its_names(monkeypatch):
    """팀 기준 이름 `dejafix`·`infra-dejafix`·에이전트 이름 `infra`가 같은 줄을 돌려준다."""
    from datetime import UTC, datetime

    from ax_contracts.data_access.common_log import LogRecord

    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)

    def rec(n, source, requester):
        return LogRecord.model_validate(
            {
                "log_id": f"log_d{n}",
                "context_id": "ctx_inc_1",
                "occurred_at": datetime(2026, 10, 22, 9, n, tzinfo=UTC),
                "source": source,
                "kind": "result_sent",
                "skill": "rollback_deployment",
                "task_status": "completed",
                "requester": requester,
            }
        )

    records = [rec(1, "infra", "orchestrator"), rec(2, "support", "orchestrator")]
    communications.set_reader(lambda q: CommonLogResult(records=records))
    client = TestClient(app)
    for name in ("dejafix", "infra-dejafix", "infra"):
        data = client.get(URL, params={"dept": name}).json()["data"]
        assert [x["from_agent"] for x in data] == ["infra"], name  # 응답은 에이전트 이름 그대로
    support = client.get(URL, params={"dept": "support"}).json()["data"]
    assert [x["from_agent"] for x in support] == ["support"]
    assert communications.agent_name("hr") == "hr"  # 이름이 같은 부서는 그대로


def test_events_are_hidden_by_default_and_shown_only_when_asked(monkeypatch):
    monkeypatch.setenv("DASHBOARD_MOCK", "1")
    plain = TestClient(app).get(URL).json()["data"]
    assert [x["kind"] for x in plain] == ["message"] * 4
    with_events = TestClient(app).get(URL, params={"include_events": "true"}).json()["data"]
    assert len(with_events) == 5
    event = with_events[-1]
    assert (event["kind"], event["event_type"]) == ("event", "support.inquiry_received")
    assert (event["from_agent"], event["to_agent"], event["skill"]) == ("support", None, None)
    assert event["outcome"] == "completed" and event["execution_id"] is None


def test_include_events_adds_the_event_kind_to_the_common_log_query(monkeypatch):
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    seen: list[CommonLogQuery] = []

    def reader(q: CommonLogQuery) -> CommonLogResult:
        seen.append(q)
        return CommonLogResult()

    communications.set_reader(reader)
    TestClient(app).get(URL)
    TestClient(app).get(URL, params={"include_events": "true"})
    assert "event_published" not in seen[0].kinds
    assert "event_published" in seen[1].kinds


def test_event_lines_are_filtered_by_dept_and_never_leak_payload_details(monkeypatch):
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    records = [
        LogRecord.model_validate(
            {
                "log_id": f"log_{n}",
                "context_id": "ctx_ui_1",
                "occurred_at": datetime(2026, 10, 26, 9, n, tzinfo=UTC),
                "source": source,
                "kind": "event_published",
                "payload": {
                    "event_type": event_type,
                    "event_id": f"evt_{n}",
                    "customer": "김하늘 010-1234-5678",
                },
            }
        )
        for n, (source, event_type) in enumerate(
            [("support", "support.inquiry_received"), ("marketing", "coupon.issued")]
        )
    ]
    communications.set_reader(lambda q: CommonLogResult(records=records))
    both = TestClient(app).get(URL, params={"include_events": "true"}).json()["data"]
    assert [x["event_type"] for x in both] == ["support.inquiry_received", "coupon.issued"]
    marketing = TestClient(app).get(URL, params={"include_events": "true", "dept": "marketing"})
    assert [x["event_type"] for x in marketing.json()["data"]] == ["coupon.issued"]
    assert "010-1234-5678" not in str(both) and "evt_" not in str(both)


def test_an_event_line_with_an_odd_payload_still_renders(monkeypatch):
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    record = LogRecord.model_validate(
        {
            "log_id": "log_x",
            "occurred_at": datetime(2026, 10, 26, 9, 0, tzinfo=UTC),
            "source": "support",
            "kind": "event_published",
            "payload": {"event_type": 123},
        }
    )
    communications.set_reader(lambda q: CommonLogResult(records=[record]))
    (line,) = TestClient(app).get(URL, params={"include_events": "true"}).json()["data"]
    assert line["kind"] == "event" and line["event_type"] is None


def test_a_log_reader_that_returns_other_kinds_cannot_add_hidden_lines(monkeypatch):
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    memory = LogRecord.model_validate(
        {
            "log_id": "log_m",
            "occurred_at": datetime(2026, 10, 26, 9, 0, tzinfo=UTC),
            "source": "memory",
            "kind": "memory_referenced",
        }
    )
    communications.set_reader(lambda q: CommonLogResult(records=[memory]))
    assert TestClient(app).get(URL, params={"include_events": "true"}).json()["data"] == []
