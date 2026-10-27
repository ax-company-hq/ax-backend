import asyncio

import pytest

from ax_orchestrator.mock.harness import new_incident
from ax_orchestrator.models import Plan, Step
from ax_orchestrator.plans.scenario_a import scenario_a


async def run(harness, fetch=None):
    engine = await harness.start(fetch)
    inc = new_incident()
    await engine.run(inc, scenario_a())
    return inc


async def wait_hold(inc, step_id):
    for _ in range(300):
        await asyncio.sleep(0.01)
        if step_id in inc.holds:
            return
    raise AssertionError("보류가 올라오지 않음")


@pytest.mark.asyncio
async def test_scenario_a_completes(harness) -> None:
    inc = await run(harness)
    assert inc.status == "resolved"
    assert all(s.status == "completed" for s in inc.steps.values())
    assert harness.events == ["incident.opened", "incident.resolved"]
    assert inc.metrics["steps_total"] == 13


@pytest.mark.asyncio
async def test_calls_carry_context_and_unique_idempotency_keys(harness) -> None:
    inc = await run(harness)
    for _, req in harness.fakes.calls:
        assert req.meta.context_id == inc.context_id
        assert req.meta.idempotency_key.startswith(f"{inc.context_id}:{req.skill}:")
    for _, req in harness.fakes.calls:  # 새 요청은 path에 자기 이름을 붙이고 3홉 이내여야 한다
        assert "orchestrator" in req.meta.path and len(req.meta.path) <= 3
    keys = [r.meta.idempotency_key for _, r in harness.fakes.calls]
    assert len(keys) == len(set(keys))


@pytest.mark.asyncio
async def test_independent_steps_run_in_parallel(harness) -> None:
    harness.fakes.latency = 0.05
    inc = await run(harness)
    d, i = inc.steps["diagnose"], inc.steps["impact"]
    assert d.started_at <= i.finished_at and i.started_at <= d.finished_at


@pytest.mark.asyncio
async def test_order_dependencies_respected(harness) -> None:
    await run(harness)
    order = [r.skill for _, r in harness.fakes.calls]
    assert order.index("publish_notice") > order.index("draft_notice")
    assert order.index("verify_recovery") > order.index("rollback_deployment")
    assert order[-1] == "record_case"


@pytest.mark.asyncio
async def test_failed_is_retried_then_ok(harness) -> None:
    harness.fakes.script("marketing", "publish_notice", "fail", "fail")
    inc = await run(harness)
    assert inc.status == "resolved"
    assert inc.steps["publish_notice"].attempts == 3


@pytest.mark.asyncio
async def test_failed_after_retries_degrades_and_skips_dependents(harness) -> None:
    harness.fakes.script("marketing", "publish_notice", "fail", "fail", "fail")
    inc = await run(harness)
    assert inc.status == "degraded"
    assert inc.steps["publish_notice"].status == "failed"
    assert inc.steps["compensation"].status == "completed"  # 공지 실패와 무관하게 보상은 진행
    assert inc.steps["record"].status == "completed"  # 기록은 남긴다
    assert inc.steps["rollback"].status == "completed"  # 다른 갈래는 계속


@pytest.mark.asyncio
async def test_timeout_retried_once(harness) -> None:
    harness.fakes.script("data", "impact_analysis", "timeout")
    inc = await run(harness)
    assert inc.steps["impact"].attempts == 2
    assert inc.status == "resolved"


@pytest.mark.asyncio
async def test_missing_card_skips_step_and_incident_continues(harness) -> None:
    harness.fakes.empty_cards.add("infra")
    inc = await run(harness)
    assert inc.steps["diagnose"].status == "skipped"
    assert "확인할 수 없음" in inc.steps["diagnose"].note
    assert inc.steps["notice"].status == "skipped"  # diagnose가 선행
    assert inc.status == "degraded"
    assert inc.steps["record"].status == "completed"


@pytest.mark.asyncio
async def test_rejected_transfers_by_rule(harness) -> None:
    harness.fakes.script("marketing", "publish_notice", "forbidden")

    async def fetch(dept):
        card = await harness.fakes.fetch_card(dept)
        if dept == "support":
            card["skills"].append({**card["skills"][0], "id": "publish_notice", "grade": "A"})
        return card

    inc = await run(harness, fetch)
    step = inc.steps["publish_notice"]
    assert step.transferred_to == "support"
    assert step.status == "completed"
    assert inc.metrics["transfers"] >= 1


@pytest.mark.asyncio
async def test_transfer_without_target_fails(harness) -> None:
    harness.fakes.script("marketing", "publish_notice", "forbidden")
    inc = await run(harness)
    step = inc.steps["publish_notice"]
    assert step.status == "transfer_queued" and step.transferred_to is None
    assert "이관 큐로 넘김" in step.note
    assert inc.status == "degraded"
    assert "이관 큐로 넘김" in "".join(inc.summary)


@pytest.mark.asyncio
async def test_transfer_llm_only_when_rules_fail(harness) -> None:
    harness.fakes.script("marketing", "publish_notice", "forbidden")
    engine = await harness.start()
    asked = []

    async def pick(skill, names):
        asked.append(skill)
        return "support"

    engine.llm_pick = pick
    inc = new_incident()
    await engine.run(inc, scenario_a())
    assert asked == ["publish_notice"]
    assert inc.steps["publish_notice"].transferred_to == "support"
    assert "LLM" in inc.steps["publish_notice"].note


@pytest.mark.asyncio
async def test_hold_approved_resumes(harness) -> None:
    harness.fakes.script("infra", "rollback_deployment", "hold")
    harness.hold_timeout = 5
    engine = await harness.start()
    inc = new_incident()
    task = asyncio.create_task(engine.run(inc, scenario_a()))
    await wait_hold(inc, "rollback")
    assert inc.steps["rollback"].status == "held"
    assert engine.decide_hold(inc, "rollback", True)
    await task
    assert inc.steps["rollback"].status == "completed"
    assert inc.status == "resolved"


@pytest.mark.asyncio
async def test_hold_timeout_auto_rejects(harness) -> None:
    harness.fakes.script("infra", "rollback_deployment", "hold")
    inc = await run(harness)  # hold_timeout 0.2s
    assert inc.steps["rollback"].status == "rejected_by_human"
    assert "시간 초과" in inc.steps["rollback"].note
    assert inc.status == "degraded"
    assert inc.steps["recovery"].status == "skipped"  # 롤백이 선행
    assert inc.steps["compensation"].status == "completed"


@pytest.mark.asyncio
async def test_hold_human_reject_is_final(harness) -> None:
    harness.fakes.script("infra", "rollback_deployment", "hold")
    harness.hold_timeout = 5
    engine = await harness.start()
    inc = new_incident()
    task = asyncio.create_task(engine.run(inc, scenario_a()))
    await wait_hold(inc, "rollback")
    engine.decide_hold(inc, "rollback", False)
    await task
    assert inc.steps["rollback"].note == "사람이 거절"
    assert not engine.decide_hold(inc, "rollback", True)  # 이미 결정됨


@pytest.mark.asyncio
async def test_dept_input_is_relayed(harness) -> None:
    harness.fakes.script("planning", "find_related_prs", "dept_input")
    inc = await run(harness)
    assert inc.status == "resolved"
    skills = [r.skill for _, r in harness.fakes.calls]
    assert skills.count("impact_analysis") == 2  # 총괄이 필요한 부서에 질의
    again = [r for _, r in harness.fakes.calls if r.skill == "find_related_prs"][-1]
    assert "answers" in again.input


@pytest.mark.asyncio
async def test_coupon_is_sent_once_with_sequence(harness) -> None:
    """쿠폰은 총괄이 보내지 않는다(#228 결정 A): 총괄이 보낸 쿠폰 요청은 0번이다.

    예전에는 총괄이 `issue_coupon`을 한 번 보내며 `sequence`를 붙였다. 지금은 고객지원이
    사건별 `sequence`를 붙여 마케팅에 직접 요청한다. 총괄의 직렬 번호는 serial_group 테스트가 본다.
    """
    inc = await run(harness)
    coupons = [r for _, r in harness.fakes.calls if r.skill == "issue_coupon"]
    assert coupons == []
    assert "coupon" not in inc.steps


@pytest.mark.asyncio
async def test_plan_has_no_compensation_or_coupon_steps(harness) -> None:
    await run(harness)
    skills = {r.skill for _, r in harness.fakes.calls}
    assert not skills & {"request_compensation", "issue_coupon"}  # #228 결정 A


@pytest.mark.asyncio
async def test_serial_group_gets_increasing_sequence(harness) -> None:
    plan = Plan(
        "t",
        [
            Step("a", "publish_notice", "a", serial_group="g", build=lambda i: {"draft_id": "1"}),
            Step("b", "publish_notice", "b", after=("a",), serial_group="g"),
        ],
    )
    engine = await harness.start()
    inc = new_incident()
    await engine.run(inc, plan)
    seq = [r.sequence for _, r in harness.fakes.calls if r.skill == "publish_notice"]
    assert seq == [1, 2]


@pytest.mark.asyncio
async def test_finance_is_not_called_during_an_incident(harness) -> None:
    await run(harness)  # 재무는 사건 중에 불리지 않는다(멘토 피드백): 손실 집계·사후 감사 단계 없음
    assert [
        r.skill for _, r in harness.fakes.calls if r.skill in ("loss_aggregation", "audit_report")
    ] == []


@pytest.mark.asyncio
async def test_orchestrator_does_not_call_verify_amount(harness) -> None:
    await run(harness)
    assert "verify_amount" not in [r.skill for _, r in harness.fakes.calls]


@pytest.mark.asyncio
async def test_record_case_carries_facts_not_fixed_text(harness) -> None:
    harness.fakes.script("marketing", "publish_notice", "fail", "fail", "fail")
    await run(harness)
    req = next(r for _, r in harness.fakes.calls if r.skill == "record_case")
    assert "publish_notice" in req.input["decision_reason"]
    assert req.input["alternatives_considered"]


@pytest.mark.asyncio
async def test_marketing_notice_follows_draft_then_publish_contract(harness) -> None:
    await run(harness)
    calls = [r for _, r in harness.fakes.calls if r.skill in ("draft_notice", "publish_notice")]
    drafts = [r for r in calls if r.skill == "draft_notice"]
    assert [d.input["kind"] for d in drafts] == ["incident_opened", "incident_resolved"]
    assert set(drafts[0].input["facts"]) == {"component", "opened_at"}
    publishes = [r for r in calls if r.skill == "publish_notice"]
    assert [set(p.input) for p in publishes] == [{"draft_id"}, {"draft_id"}]
    assert publishes[0].input["draft_id"] != publishes[1].input["draft_id"]


@pytest.mark.asyncio
async def test_link_incident_uses_pr_number_from_related_prs(harness) -> None:
    await run(harness)
    link = next(r for _, r in harness.fakes.calls if r.skill == "link_incident")
    assert link.input["pr_numbers"] == [412, 418]


@pytest.mark.asyncio
async def test_human_approval_resume_is_not_counted_as_retry(harness) -> None:
    harness.fakes.script("infra", "rollback_deployment", "hold")
    harness.hold_timeout = 5
    engine = await harness.start()
    inc = new_incident()
    task = asyncio.create_task(engine.run(inc, scenario_a()))
    await wait_hold(inc, "rollback")
    engine.decide_hold(inc, "rollback", True)
    await task
    assert inc.steps["rollback"].attempts == 1
    assert inc.metrics["retries"] == 0
