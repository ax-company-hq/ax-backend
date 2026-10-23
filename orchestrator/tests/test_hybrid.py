import pytest

from ax_contracts.schemas import AgentResult, TaskStatus
from ax_orchestrator.engine import Engine
from ax_orchestrator.hybrid import HybridDepartments, parse_real_depts
from ax_orchestrator.mock.fake_departments import FakeDepartments
from ax_orchestrator.mock.harness import DEPTS, new_incident
from ax_orchestrator.plans.scenario_a import scenario_a
from ax_orchestrator.registry import Registry


def test_parse_real_depts() -> None:
    assert parse_real_depts(" hr, finance ,", DEPTS) == {"hr", "finance"}
    assert parse_real_depts(None, DEPTS) == set()
    with pytest.raises(ValueError, match="알 수 없는 부서: nope"):
        parse_real_depts("hr,nope", DEPTS)


def _stub(seen: list):
    async def real(dept, req):
        seen.append((dept, req.skill, list(req.meta.path)))
        return AgentResult(
            meta=req.meta, status=TaskStatus.COMPLETED, output={"card_id": "card_real"}
        )

    async def fetch(dept):
        return {
            "name": dept,
            "owner": "x",
            "description": "실제",
            "url": f"http://{dept}/a2a",
            "skills": [
                {
                    "id": "record_case",
                    "description": "d",
                    "input_schema": "s",
                    "output_schema": "s",
                    "grade": "A",
                    "allowed_requesters": ["orchestrator"],
                    "priority": "required",
                }
            ],
        }

    return real, fetch


@pytest.mark.asyncio
async def test_only_chosen_dept_goes_to_real_caller() -> None:
    seen: list = []
    real, fetch = _stub(seen)
    hybrid = HybridDepartments(FakeDepartments(), {"hr"}, real_caller=real, real_fetch=fetch)
    registry = Registry()
    await registry.load(DEPTS, hybrid.fetch_card)
    assert registry.cards["hr"].description == "실제"  # 카드는 실제 부서에서
    assert registry.cards["data"].description.endswith("(목 카드)")
    engine = Engine(registry, hybrid, hold_timeout=0.2)
    inc = new_incident()
    await engine.run(inc, scenario_a())
    assert inc.status == "resolved"
    assert [(d, s) for d, s, _ in seen] == [("hr", "record_case")]
    assert "orchestrator" in seen[0][2]
    assert hybrid.modes(DEPTS)["hr"] == "real" and hybrid.modes(DEPTS)["data"] == "mock"


@pytest.mark.asyncio
async def test_unreachable_real_dept_is_unavailable_and_incident_continues() -> None:
    async def fetch(dept):
        return None

    hybrid = HybridDepartments(FakeDepartments(), {"finance"}, real_fetch=fetch)
    registry = Registry()
    await registry.load(DEPTS, hybrid.fetch_card)
    assert "finance" in registry.unavailable
    inc = new_incident()
    await Engine(registry, hybrid, hold_timeout=0.2).run(inc, scenario_a())
    assert inc.steps["loss"].status == "skipped"
    assert inc.status == "degraded"
