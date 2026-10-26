"""시나리오 A의 갈림길(계획대로 안 될 때)과 일괄 보상·사건 후 단계."""

import asyncio

import pytest

from ax_orchestrator.mock import fake_departments
from ax_orchestrator.mock.harness import new_incident
from ax_orchestrator.plans.scenario_a import RECOVERY_TIMEOUT_SECONDS, scenario_a


async def run(harness):
    engine = await harness.start()
    inc = new_incident()
    await engine.run(inc, scenario_a())
    return inc


def called(harness, skill: str) -> list:
    return [req for _, req in harness.fakes.calls if req.skill == skill]


# ---------------------------------------------------------------- 영향 고객 일괄 보상
@pytest.mark.asyncio
async def test_all_affected_customers_are_sent_to_support_in_one_request(harness) -> None:
    inc = await run(harness)
    requests = called(harness, "request_incident_compensation")
    assert len(requests) == 1
    impact_ids = inc.outputs["impact"]["affected_customer_ids"]
    assert requests[0].input["buyer_ids"] == impact_ids
    assert requests[0].input["reason_code"] == "payment_failure"
    assert requests[0].input["impact_ref"] == f"{inc.context_id}:impact"
    assert inc.steps["compensation"].dept == "support"
    assert inc.steps["compensation"].status == "completed"


@pytest.mark.asyncio
async def test_loss_report_waits_for_compensation(harness) -> None:
    await run(harness)
    order = [r.skill for _, r in harness.fakes.calls]
    assert order.index("loss_aggregation") > order.index("request_incident_compensation")


@pytest.mark.asyncio
async def test_no_affected_customers_means_no_compensation_request(harness, monkeypatch) -> None:
    empty_impact = {
        "failed_order_count": 0,
        "affected_customer_count": 0,
        "affected_customer_ids": [],
        "damage_amount": 0,
    }
    monkeypatch.setitem(fake_departments.DEFAULT_OUTPUTS, "impact_analysis", lambda r: empty_impact)
    inc = await run(harness)
    step = inc.steps["compensation"]
    assert step.status == "skipped" and step.reason_code == "no_affected_customers"
    assert called(harness, "request_incident_compensation") == []
    assert inc.status == "resolved"  # 보상 대상이 없는 것은 실패가 아니다


@pytest.mark.asyncio
async def test_compensation_failure_does_not_break_the_incident(harness) -> None:
    harness.fakes.script("support", "request_incident_compensation", "fail", "fail", "fail")
    inc = await run(harness)
    assert inc.steps["compensation"].status == "failed"
    assert inc.status == "resolved"  # 일괄 보상은 필수 단계가 아니다
    assert inc.steps["loss"].status == "completed"  # 재무 손실 리포트는 끝나면 계속 진행


# ---------------------------------------------------------------- 사건 후 단계
@pytest.mark.asyncio
async def test_post_incident_audit_and_kpi_are_requested_for_this_incident(harness) -> None:
    inc = await run(harness)
    audit = called(harness, "audit_report")
    kpi = called(harness, "kpi_summary")
    assert audit[0].input == {"context_id": inc.context_id}
    assert kpi[0].input == {"context_ids": [inc.context_id]}
    order = [r.skill for _, r in harness.fakes.calls]
    assert order.index("audit_report") > order.index("request_incident_compensation")
    assert order.index("kpi_summary") > order.index("publish_notice")
    assert order[-1] == "record_case"  # 사건 기록은 모든 단계가 끝난 뒤 한 번


@pytest.mark.asyncio
async def test_audit_failure_is_visible_but_not_fatal(harness) -> None:
    harness.fakes.script("finance", "audit_report", "fail", "fail", "fail")
    inc = await run(harness)
    assert inc.steps["audit"].status == "failed"
    assert inc.status == "resolved"


# ---------------------------------------------------------------- 갈림길
@pytest.mark.asyncio
async def test_undetermined_cause_skips_rollback_and_queues_for_a_human(harness) -> None:
    harness.fakes.script("infra", "diagnose_incident", "undetermined")
    inc = await run(harness)
    rollback = inc.steps["rollback"]
    assert rollback.status == "transfer_queued" and rollback.reason_code == "no_rollback_target"
    assert called(harness, "rollback_deployment") == []  # 원인을 모르면 되돌리지 않는다
    # 공지·영향 분석·보상·손실 리포트는 계속 진행한다
    for step_id in ("notice", "publish_notice", "impact", "compensation", "loss"):
        assert inc.steps[step_id].status == "completed", step_id
    # 롤백이 없으니 복구 확인과 정상화 공지는 건너뛴다
    assert inc.steps["recovery"].status == "skipped"
    assert inc.steps["recovered_notice"].status == "skipped"
    assert inc.status == "degraded"


@pytest.mark.asyncio
async def test_recovery_not_confirmed_queues_for_a_human_without_resolved_notice(harness) -> None:
    harness.fakes.script("infra", "verify_recovery", "not_recovered")
    inc = await run(harness)
    recovery = inc.steps["recovery"]
    assert recovery.status == "transfer_queued" and recovery.reason_code == "not_recovered"
    assert inc.steps["recovered_draft"].status == "skipped"
    assert inc.steps["recovered_notice"].status == "skipped"  # 아직 정상화가 아니다
    assert len(called(harness, "publish_notice")) == 1  # 점검 공지만 게시했다
    assert inc.status == "degraded"
    assert inc.steps["record"].status == "completed"  # 그래도 사건 기록은 남긴다


@pytest.mark.asyncio
async def test_recovery_gets_a_longer_deadline_for_infra_retries(harness) -> None:
    await run(harness)
    recovery = called(harness, "verify_recovery")[0]
    other = called(harness, "diagnose_incident")[0]
    longer = (recovery.meta.deadline - other.meta.deadline).total_seconds()
    assert longer > RECOVERY_TIMEOUT_SECONDS - 60


@pytest.mark.asyncio
async def test_rollback_hold_waits_for_a_human_and_resumes_after_approval(harness) -> None:
    harness.fakes.script("infra", "rollback_deployment", "hold")
    engine = await harness.start()
    inc = new_incident()
    task = asyncio.create_task(engine.run(inc, scenario_a()))
    for _ in range(300):
        await asyncio.sleep(0.01)
        if "rollback" in inc.holds:
            break
    assert inc.steps["rollback"].status == "held"
    assert engine.decide_hold(inc, "rollback", True)
    await task
    assert inc.steps["rollback"].status == "completed"
    assert inc.steps["recovery"].status == "completed"
    assert inc.status == "resolved"


@pytest.mark.asyncio
async def test_rollback_hold_rejected_never_runs_recovery(harness) -> None:
    harness.fakes.script("infra", "rollback_deployment", "hold")
    engine = await harness.start()
    inc = new_incident()
    task = asyncio.create_task(engine.run(inc, scenario_a()))
    for _ in range(300):
        await asyncio.sleep(0.01)
        if "rollback" in inc.holds:
            break
    engine.decide_hold(inc, "rollback", False)
    await task
    assert inc.steps["rollback"].status == "rejected_by_human"
    assert inc.steps["recovery"].status == "skipped"
    assert inc.status == "degraded"
