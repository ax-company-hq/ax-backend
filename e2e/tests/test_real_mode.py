"""real 모드 호출부·real-result.json 형식 (실서버 없이: 응답은 목으로 대체)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ax_e2e.scenario_a import cli, harness, judge, once
from ax_e2e.scenario_a.result import Outcome
from ax_e2e.scenario_a.stages import build_stage_table

from ax_contracts.data_access.dev_status import DevStatus

NOW = datetime(2026, 10, 23, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_environ(monkeypatch):
    """prepare_real_env가 환경변수를 바꾸므로 테스트마다 복사본을 쓴다."""
    monkeypatch.setattr(harness.os, "environ", dict(harness.os.environ))


def runs_with(states: dict[str, str], default: str = "pass") -> list[dict]:
    stages = build_stage_table()
    unit = {f"{s.path}:{s.key}": Outcome(states.get(s.key, default)).to_json() for s in stages}
    chain = {k: Outcome("absent", once.CHAIN_PENDING).to_json() for k in unit}
    return [{"meta": {"mode": "real", "detect": "mock"}, "unit": unit, "chain": chain}]


def real_status(states: dict[str, str], prev: DevStatus | None = None, prev_real=None):
    stages = build_stage_table()
    status, _ = judge.judge(stages, runs_with(states), prev, "c1", NOW)
    return judge.to_real_status(status, prev_real)


def test_real_result_shape_has_real_stages_and_totals() -> None:
    out = real_status({"impact": "fail", "diagnose": "absent"})
    dumped = json.loads(out.model_dump_json(by_alias=True))
    assert len(dumped["stages"]) == 17
    assert all(s["real"] is not None for s in dumped["stages"])
    assert all(s["unit"]["state"] == "pending" and s["chain"]["state"] == "pending"
               for s in dumped["stages"])
    impact = next(s for s in dumped["stages"] if "impact" in s["name"])
    assert impact["real"]["state"] == "broken"
    assert dumped["totals"]["real_pass"] == 15 and dumped["totals"]["total"] == 17
    assert dumped["totals"]["real_runs_consecutive"] == 0
    assert dumped["totals"]["unit_pass"] == 0 and dumped["totals"]["chain_pass"] == 0
    DevStatus.model_validate(dumped)  # 계약 형식 그대로 읽힌다


def test_consecutive_counts_only_full_pass_runs() -> None:
    first = real_status({})
    assert first.totals.real_pass == 17 and first.totals.real_runs_consecutive == 1
    second = real_status({}, judge.real_as_unit(first), first)
    assert second.totals.real_runs_consecutive == 2
    broken = real_status({"loss": "fail"}, judge.real_as_unit(second), second)
    assert broken.totals.real_runs_consecutive == 0
    loss = next(s for s in broken.stages if "loss" in s.name)
    assert loss.real is not None and loss.real.state == "regressed"  # 통과하다 실패


def test_real_mode_without_host_stays_stub(monkeypatch) -> None:
    monkeypatch.delenv("AX_REAL_HOST", raising=False)
    out = once.run_once("real")
    assert {v["state"] for v in out["unit"].values()} == {"absent"}


def test_prepare_real_env_points_every_port_to_host(monkeypatch) -> None:
    from ax_agent_template.config import DEFAULT_PORTS, agent_url

    harness.prepare_real_env("localhost")
    for name, port in DEFAULT_PORTS.items():
        assert agent_url(name) == f"http://localhost:{port}"


def test_real_request_uses_requester_token_from_env(monkeypatch) -> None:
    monkeypatch.setenv("AX_E2E_MODE", "real")
    monkeypatch.setenv("SUPPORT_TOKEN", "tok-support")
    req = harness.build_request("create_inquiry", {}, requester="support", context_id="ctx_x")
    assert req.meta.token == "tok-support"
    other = harness.build_request("x", {}, requester="shop-api", context_id="ctx_x")
    assert other.meta.token == "mock:shop-api"  # 토큰이 없으면 실서버가 거부하도록 mock 토큰
    monkeypatch.setenv("AX_E2E_MODE", "mock")
    mock_req = harness.build_request("x", {}, requester="support", context_id="ctx_x")
    assert mock_req.meta.token == "mock:support"


def test_cli_real_mode_writes_real_result(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AX_E2E_MODE", "real")
    monkeypatch.setattr(cli, "run_subprocess", lambda n, tmp: runs_with({"impact": "fail"})[0])
    out = tmp_path / "out"
    assert cli.main(["run", "--out", str(out), "--runs", "1", "--commit", "c1"]) == 0
    assert (out / "real-result.json").exists() and not (out / "e2e-result.json").exists()
    data = json.loads((out / "real-result.json").read_text(encoding="utf-8"))
    assert data["totals"]["real_pass"] == 16
    detail = json.loads((out / "e2e-detail.json").read_text(encoding="utf-8"))
    assert detail["mode"] == "real"


@pytest.mark.parametrize("detect", ["mock", "real"])
def test_detect_mock_is_marked_on_diagnose_stage(monkeypatch, detect: str) -> None:
    monkeypatch.setenv("AX_REAL_HOST", "localhost")
    monkeypatch.setenv("AX_REAL_DETECT", detect)

    async def fake_units(stages, apps):
        return {s.key: Outcome("pass") for s in stages if s.path == "incident"}, {}

    async def fake_customer(stages, apps, notice_id):
        return {s.key: Outcome("pass") for s in stages if s.path == "customer"}

    monkeypatch.setattr(once.unit, "run_incident_units", fake_units)
    monkeypatch.setattr(once.customer, "run_unit", fake_customer)
    out = once.run_once("real")
    note = out["unit"]["incident:diagnose"]["note"]
    assert ("감지: 목" in note) == (detect == "mock")
    assert out["meta"]["detect"] == detect
