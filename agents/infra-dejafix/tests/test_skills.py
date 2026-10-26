"""infra skill 고유 테스트: 진단 규칙, 롤백 경로(B/C)와 대상 버전, 복구 확인 재시도."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import ExecuteDecision
from infra_agent import handlers
from infra_agent.clients import MockData
from infra_agent.deployments import DeploymentHistory
from infra_agent.diagnosis import diagnose
from infra_agent.main import app

NOW = datetime(2026, 10, 21, 5, 10, tzinfo=UTC)
ROLLBACK = {"service": "shop-api", "from_version": "v2.4.1", "release_fault_injection": True}


def _history(tmp_path, monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> DeploymentHistory:
    path = tmp_path / "deployments.json"
    path.write_text(json.dumps({"deployments": rows}), encoding="utf-8")
    monkeypatch.setenv("INFRA_DEPLOYMENTS_FILE", str(path))
    return DeploymentHistory.load()


def _row(version: str, at: str, **extra) -> dict:
    return {
        "deploy_id": f"dep_{version}",
        "service": "shop-api",
        "version": version,
        "deployed_at": at,
        "components": ["payment"],
        "change_summary": f"{version} 변경",
        **extra,
    }


# ---- 진단 규칙 (실제 모드)


def test_real_mode_single_fresh_deploy_is_high() -> None:
    diag = diagnose(DeploymentHistory.load(), "payment", NOW, mock=False)
    assert diag.output.status == "suspected"
    assert [(s.version, s.confidence) for s in diag.output.suspects] == [("v2.4.1", "high")]
    assert diag.output.impact_scope == ["payment"]


def test_real_mode_old_single_deploy_is_mid() -> None:
    diag = diagnose(DeploymentHistory.load(), "payment", NOW + timedelta(minutes=40), mock=False)
    assert [s.confidence for s in diag.output.suspects] == ["mid"]


def test_real_mode_no_deploy_in_window_is_undetermined() -> None:
    diag = diagnose(DeploymentHistory.load(), "payment", NOW + timedelta(hours=3), mock=False)
    assert diag.output.status == "undetermined"
    assert diag.output.suspects == []


def test_real_mode_multiple_candidates(tmp_path, monkeypatch) -> None:
    history = _history(
        tmp_path,
        monkeypatch,
        [
            _row("v1.0.0", "2026-10-20T00:00:00Z"),
            _row("v1.0.1", "2026-10-21T04:50:00Z"),
            _row("v1.0.2", "2026-10-21T05:05:00Z"),
        ],
    )
    diag = diagnose(history, "payment", NOW, mock=False)
    assert [(s.version, s.confidence) for s in diag.output.suspects] == [
        ("v1.0.2", "mid"),
        ("v1.0.1", "low"),
    ]


def test_mock_mode_returns_latest_as_high() -> None:
    diag = diagnose(DeploymentHistory.load(), "payment", NOW + timedelta(days=30), mock=True)
    assert [(s.version, s.confidence) for s in diag.output.suspects] == [("v2.4.1", "high")]


def test_diagnose_has_no_recovery_eta(a2a_body) -> None:
    result = (
        TestClient(app)
        .post("/a2a", json=a2a_body("diagnose_incident", {"component": "payment"}))
        .json()
    )
    assert set(result["output"]) == {"status", "suspects", "impact_scope"}
    assert result["evidence"]["ref_ids"] == ["deploy:dep_0003"]


# ---- 롤백 대상 버전과 경로


def test_to_version_is_resolved_from_history(a2a_body, gate) -> None:
    fake = gate("executed")
    body = a2a_body("rollback_deployment", {**ROLLBACK, "to_version": None})
    result = TestClient(app).post("/a2a", json=body).json()
    assert result["output"]["rolled_back_to"] == "v2.4.0"
    assert fake.calls[0]["params"]["to_version"] == "v2.4.0"


def test_wrong_to_version_is_refused(a2a_body, gate, runtime) -> None:
    fake = gate("executed")
    body = a2a_body("rollback_deployment", {**ROLLBACK, "to_version": "v2.3.9"})
    result = TestClient(app).post("/a2a", json=body).json()
    assert (result["status"], result["outcome"]) == ("failed", "invalid_input")
    assert fake.calls == []
    assert runtime.shop.released == 0


def test_unknown_version_is_refused(a2a_body, gate) -> None:
    fake = gate("executed")
    body = a2a_body("rollback_deployment", {**ROLLBACK, "from_version": "v9.9.9"})
    result = TestClient(app).post("/a2a", json=body).json()
    assert (result["status"], result["outcome"]) == ("failed", "invalid_input")
    assert fake.calls == []


def test_irreversible_change_is_counted(tmp_path, monkeypatch, a2a_body, gate, runtime) -> None:
    runtime.history = _history(
        tmp_path,
        monkeypatch,
        [
            _row("v1.0.0", "2026-10-20T00:00:00Z"),
            _row("v1.0.1", "2026-10-21T05:02:00Z", irreversible=["db_migration"]),
        ],
    )
    fake = gate("held")
    body = a2a_body("rollback_deployment", {**ROLLBACK, "from_version": "v1.0.1"})
    TestClient(app).post("/a2a", json=body)
    assert fake.calls[0]["params"]["irreversible_count"] == 1


def test_low_confidence_when_suspect_is_not_high(a2a_body, gate, runtime, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    runtime.clock = lambda: NOW + timedelta(minutes=40)
    monkeypatch.setattr(handlers, "is_mock", lambda name=None: False)  # 실제 진단 규칙
    fake = gate("held")
    client = TestClient(app)
    client.post("/a2a", json=a2a_body("diagnose_incident", {"component": "payment"}))
    client.post("/a2a", json=a2a_body("rollback_deployment", ROLLBACK))
    assert fake.calls[0]["params"]["low_confidence"] == 1


def test_human_approval_then_same_request_executes(a2a_body, gate, runtime) -> None:
    fake = gate("held", "executed:C")
    client = TestClient(app)
    first = client.post("/a2a", json=a2a_body("rollback_deployment", ROLLBACK)).json()
    assert first["status"] == "input-required"
    second = client.post("/a2a", json=a2a_body("rollback_deployment", ROLLBACK)).json()
    assert second["status"] == "completed"
    assert second["output"]["path"] == "human_approved"
    assert fake.calls[0]["key"] == fake.calls[1]["key"]
    assert runtime.shop.released == 1


def test_prior_rollbacks_counts_executed_ones(
    tmp_path, monkeypatch, a2a_body, gate, runtime
) -> None:
    runtime.history = _history(
        tmp_path,
        monkeypatch,
        [
            _row("v1.0.0", "2026-10-20T00:00:00Z"),
            _row("v1.0.1", "2026-10-21T04:00:00Z"),
            _row("v1.0.2", "2026-10-21T05:02:00Z"),
        ],
    )
    fake = gate("executed")
    client = TestClient(app)
    client.post(
        "/a2a", json=a2a_body("rollback_deployment", {**ROLLBACK, "from_version": "v1.0.2"})
    )
    client.post(
        "/a2a", json=a2a_body("rollback_deployment", {**ROLLBACK, "from_version": "v1.0.1"})
    )
    assert [c["params"]["prior_rollbacks"] for c in fake.calls] == [0, 1]
    assert fake.calls[1]["params"]["to_version"] == "v1.0.0"


def test_reviewer_rejection_is_not_executed(a2a_body, gate, runtime) -> None:
    gate(
        ExecuteDecision(
            execution_id="exe_0003", grade="C", status="rejected", hold_reason="review_timeout"
        )
    )
    result = TestClient(app).post("/a2a", json=a2a_body("rollback_deployment", ROLLBACK)).json()
    assert (result["status"], result["outcome"]) == ("completed", "rejected")
    assert runtime.shop.released == 0


def test_release_failure_is_reported(a2a_body, gate, runtime) -> None:
    gate("executed")

    async def broken() -> dict:
        raise ConnectionError("down")

    runtime.shop.release_fault = broken  # type: ignore[method-assign]
    result = TestClient(app).post("/a2a", json=a2a_body("rollback_deployment", ROLLBACK)).json()
    assert (result["status"], result["outcome"]) == ("failed", "release_failed")


# ---- 복구 확인


RECOVERY = {"component": "payment", "since": "2026-10-21T05:04:00Z"}


def test_recovered_when_both_metrics_ok(a2a_body, runtime) -> None:
    runtime.shop.state = {"active": False, "degraded": False}
    result = TestClient(app).post("/a2a", json=a2a_body("verify_recovery", RECOVERY)).json()
    assert result["status"] == "completed"
    assert result["output"]["recovered"] is True
    assert result["output"]["infra_metrics_ok"] is True
    assert result["output"]["business_metrics_ok"] is True
    assert runtime.sleep.waits == []


def test_retries_five_times_then_not_recovered(a2a_body, runtime) -> None:
    result = TestClient(app).post("/a2a", json=a2a_body("verify_recovery", RECOVERY)).json()
    assert result["output"]["recovered"] is False
    assert result["outcome"] == "not_recovered"
    assert runtime.sleep.waits == [30.0, 30.0, 30.0, 30.0]


def test_missing_business_metrics_is_not_recovered(a2a_body, runtime) -> None:
    runtime.shop.state = {"active": False, "degraded": False}
    runtime.data = MockData(ok=None)
    result = TestClient(app).post("/a2a", json=a2a_body("verify_recovery", RECOVERY)).json()
    assert result["output"]["business_metrics_ok"] is None
    assert result["output"]["recovered"] is False


def test_retries_stop_before_deadline(a2a_body, runtime) -> None:
    clock = {"now": datetime.now(UTC)}
    runtime.clock = lambda: clock["now"]

    async def sleep(seconds: float) -> None:
        runtime.sleep_waits.append(seconds)
        clock["now"] += timedelta(seconds=seconds)

    runtime.sleep_waits = []  # type: ignore[attr-defined]
    runtime.sleep = sleep
    body = a2a_body("verify_recovery", RECOVERY, deadline_s=70)
    result = TestClient(app).post("/a2a", json=body).json()
    assert result["output"]["recovered"] is False
    assert runtime.sleep_waits == [30.0, 30.0]
