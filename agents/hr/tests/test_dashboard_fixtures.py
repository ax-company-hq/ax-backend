"""화면 개발용 응답 샘플: 계약 모델과 맞고, 코드와 어긋나면 알려 준다."""

import importlib.util
import json
from pathlib import Path

import pytest

from ax_contracts.schemas import (
    AuditEntry,
    Card,
    CheckPlanOutput,
    DecideCandidateCardOutput,
    GetRosterOutput,
    HandoverQuestion,
    HandoverView,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "docs" / "notes" / "hr" / "dashboard-fixtures"
SCRIPT = ROOT / "agents" / "hr" / "scripts" / "make_dashboard_fixtures.py"

MODELS = {
    "GET_handovers": ("list", HandoverView),
    "GET_handover-questions": ("list", HandoverQuestion),
    "GET_candidate-cards": ("list", Card),
    "GET_hr-activity": ("list", AuditEntry),
    "GET_roster": ("one", GetRosterOutput),
    "POST_candidate-cards_decision": ("one", DecideCandidateCardOutput),
    "POST_plan-checks": ("one", CheckPlanOutput),
}


def _load(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", sorted(MODELS))
def test_every_fixture_parses_with_its_contract_model(name: str) -> None:
    kind, model = MODELS[name]
    payload = _load(name)
    assert set(payload) == {"data", "meta"} and payload["meta"]["tenant_id"] == "shop-demo"
    if kind == "list":
        assert payload["meta"]["total"] == len(payload["data"]) > 0
        for item in payload["data"]:
            model.model_validate(item)
    else:
        model.model_validate(payload["data"])


def test_error_fixtures_follow_the_dashboard_error_shape() -> None:
    files = sorted(FIXTURES.glob("ERROR_*.json"))
    assert len(files) >= 5
    for path in files:
        err = json.loads(path.read_text(encoding="utf-8"))["error"]
        assert set(err) == {"code", "message"} and path.name.split("_")[1].isdigit()


def test_committed_fixtures_match_what_the_code_produces(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("make_dashboard_fixtures", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main(tmp_path)
    fresh = {p.name: p.read_text(encoding="utf-8") for p in tmp_path.glob("*.json")}
    committed = {p.name: p.read_text(encoding="utf-8") for p in FIXTURES.glob("*.json")}
    assert fresh == committed, (
        "코드가 바뀌었다: make_dashboard_fixtures.py를 다시 돌려 파일을 갱신한다"
    )
