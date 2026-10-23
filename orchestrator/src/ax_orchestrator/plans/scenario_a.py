"""시나리오 A: 결제 API 장애 (docs/planning.md 5장, 통합에이전트기획안 4장)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ax_orchestrator.models import Incident, Plan, Step, iso


def _window(inc: Incident) -> dict[str, Any]:
    return {
        "window_from": iso(inc.opened_at - timedelta(minutes=15)),
        "window_to": iso(inc.opened_at + timedelta(minutes=15)),
    }


def _out(inc: Incident, step_id: str) -> dict[str, Any]:
    return inc.outputs.get(step_id, {})


def _diagnose(inc: Incident) -> dict[str, Any]:
    return {"component": inc.component}


def _impact(inc: Incident) -> dict[str, Any]:
    return {**_window(inc), "scope": "payment"}


def _rollback(inc: Incident) -> dict[str, Any]:
    suspect = (_out(inc, "diagnose").get("suspects") or [{}])[0]
    return {
        "service": suspect.get("service"),
        "from_version": suspect.get("version"),
        "to_version": suspect.get("previous_version"),
        "release_fault_injection": True,
    }


def _related_prs(inc: Incident) -> dict[str, Any]:
    return {**_window(inc), "component": inc.component}


def _link(inc: Incident) -> dict[str, Any]:
    prs = [c.get("number") for c in _out(inc, "related_prs").get("candidates", [])]
    return {"incident_context_id": inc.context_id, "pr_numbers": [p for p in prs if p]}


def _notice(inc: Incident) -> dict[str, Any]:
    return {
        "kind": "maintenance",
        "facts": {
            "component": inc.component,
            "since": iso(inc.opened_at),
            "cause_status": _out(inc, "diagnose").get("status", "undetermined"),
        },
    }


def _publish_notice(inc: Incident) -> dict[str, Any]:
    return {"notice_id": _out(inc, "notice").get("notice_id")}


def _compensation(inc: Incident) -> dict[str, Any]:
    impact = _out(inc, "impact")
    return {
        "customer_ids": impact.get("affected_customer_ids", []),
        "notice_id": _out(inc, "publish_notice").get("notice_id"),
        "reason": "payment_outage",
    }


def _coupon(inc: Incident) -> dict[str, Any]:
    comp = _out(inc, "compensation")
    return {
        "customer_ids": comp.get("eligible_customer_ids", []),
        "amount_per_customer": comp.get("amount_per_customer"),
        "idempotency_scope": inc.context_id,
    }


def _loss(inc: Incident) -> dict[str, Any]:
    # 재무는 영향 숫자를 data에서 직접 조회하고 보상액은 자기 장부에서 센다. 총괄은 구간만 준다.
    return {**_window(inc), "context_id": inc.context_id}


def _recovery(inc: Incident) -> dict[str, Any]:
    return {"component": inc.component, "since": iso(inc.opened_at)}


def _recovered_notice(inc: Incident) -> dict[str, Any]:
    return {"notice_id": _out(inc, "notice").get("notice_id"), "kind": "recovered"}


def scenario_a() -> Plan:
    return Plan(
        name="scenario_a",
        steps=[
            Step("diagnose", "diagnose_incident", "원인 진단 (infra)", build=_diagnose),
            Step("impact", "impact_analysis", "영향 분석 (data)", build=_impact),
            Step(
                "rollback",
                "rollback_deployment",
                "원인 배포 롤백 (B)",
                after=("diagnose", "impact"),
                build=_rollback,
            ),
            Step(
                "related_prs",
                "find_related_prs",
                "관련 PR 찾기 (planning)",
                after=("diagnose", "impact"),
                build=_related_prs,
                required=False,
            ),
            Step(
                "link_incident",
                "link_incident",
                "사건에 PR 연결 (planning)",
                after=("related_prs",),
                build=_link,
                required=False,
            ),
            Step(
                "notice",
                "draft_notice",
                "점검 공지 초안 (marketing)",
                after=("diagnose", "impact"),
                build=_notice,
            ),
            Step(
                "publish_notice",
                "publish_notice",
                "점검 공지 게시 (marketing)",
                after=("notice",),
                build=_publish_notice,
            ),
            Step(
                "compensation",
                "request_compensation",
                "보상 판정 (support)",
                after=("publish_notice",),
                build=_compensation,
            ),
            Step(
                "coupon",
                "issue_coupon",
                "보상 쿠폰 지급 (B, finance 검증)",
                after=("compensation",),
                build=_coupon,
                serial_group="coupon",
            ),
            Step(
                "loss",
                "loss_aggregation",
                "손실 리포트 (finance)",
                after=("impact",),
                also_after=("coupon",),
                build=_loss,
            ),
            Step(
                "recovery",
                "verify_recovery",
                "복구 검증 (infra)",
                after=("rollback",),
                build=_recovery,
            ),
            Step(
                "recovered_notice",
                "publish_notice",
                "정상화 공지 (marketing)",
                after=("recovery",),
                build=_recovered_notice,
            ),
            Step(
                "record",
                "record_case",
                "사건 기록 (hr)",
                also_after=(
                    "recovered_notice",
                    "link_incident",
                    "loss",
                ),
                kind="record",
            ),
        ],
    )
