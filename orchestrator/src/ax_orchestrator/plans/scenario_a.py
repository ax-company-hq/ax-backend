"""시나리오 A: 결제 API 장애 (docs/planning.md 5장, docs/notes/scn-a 정리).

- 영향 고객 일괄 보상: 데이터 영향 분석의 고객 목록을 총괄이 고객지원
  `request_incident_compensation`에 한 번에 넘긴다(문의를 안 낸 고객 포함).
  문의를 낸 고객의 보상은 고객지원이 직통으로 처리한다(#228).
- 갈림길: 원인 미확정이면 롤백하지 않고 이관 큐로, 복구 확인이 실패하면
  정상화 공지 없이 이관 큐로 돌린다.
- 사건 후: 데이터 처리 시간 요약(`kpi_summary`)을 요청한다.
- 재무는 사건 중에 부르지 않는다(멘토 피드백, SCN-A). 장애 때 나간 쿠폰·환불은 이벤트로 재무 장부에
  기록되고, 비용은 평상시 결산·사후 감사에서 다룬다.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ax_orchestrator.models import Incident, Plan, Step, iso, now

# 고객지원 `request_incident_compensation`이 한 호출에 받는 구매자 수 상한
COMPENSATION_BATCH = 50


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
    prs = [c.get("pr_number") for c in _out(inc, "related_prs").get("candidates", [])]
    return {"incident_context_id": inc.context_id, "pr_numbers": [p for p in prs if p]}


def _notice(inc: Incident) -> dict[str, Any]:
    # 실제 marketing `DraftNoticeInput`: 사실만 넘기고 문장은 마케팅이 만든다
    return {
        "kind": "incident_opened",
        "facts": {"component": inc.component, "opened_at": iso(inc.opened_at)},
    }


def _publish_notice(inc: Incident) -> dict[str, Any]:
    return {"draft_id": _out(inc, "notice").get("draft_id")}


def _recovery(inc: Incident) -> dict[str, Any]:
    return {"component": inc.component, "since": iso(inc.opened_at)}


def _recovered_notice(inc: Incident) -> dict[str, Any]:
    return {"draft_id": _out(inc, "recovered_draft").get("draft_id")}


def _recovered_draft(inc: Incident) -> dict[str, Any]:
    return {
        "kind": "incident_resolved",
        "facts": {
            "component": inc.component,
            "opened_at": iso(inc.opened_at),
            "resolved_at": iso(now()),
        },
    }


RECOVERY_TIMEOUT_SECONDS = 180.0  # 인프라가 30초 간격 최대 5회 다시 확인하는 시간을 기다려 준다


def _rollback_gate(inc: Incident) -> tuple[str, str, str] | None:
    """원인이 정해지지 않았으면 롤백하지 않는다. 사람이 원인을 확인하게 이관 큐로 넘긴다."""
    diagnosis = _out(inc, "diagnose")
    if diagnosis.get("status") == "suspected" and diagnosis.get("suspects"):
        return None
    return (
        "transfer_queued",
        "no_rollback_target",
        "원인 미확정 — 롤백하지 않고 사람이 원인을 확인하도록 이관 큐로 넘김",
    )


def _recovery_verdict(output: dict[str, Any]) -> tuple[str, str] | None:
    """서버·결제 실패율이 정상으로 돌아오지 않았으면(인프라가 재시도한 뒤에도) 사람이 판단한다."""
    if output.get("recovered") is False:
        return (
            "not_recovered",
            "복구 확인 실패 — 정상화 공지 없이 사람이 판단하도록 이관 큐로 넘김",
        )
    return None


def _compensation(inc: Incident) -> dict[str, Any]:
    # 고객 목록은 데이터 영향 분석 결과 그대로(가명 ID). 단가·건수 상한은 고객지원 정책표가 정한다.
    return {
        "buyer_ids": list(_out(inc, "impact").get("affected_customer_ids") or []),
        "reason_code": "payment_failure",
        "impact_ref": f"{inc.context_id}:impact",
    }


def _merge_compensation(outputs: list[dict[str, Any]]) -> dict[str, Any]:
    """50명씩 나눠 부른 결과를 하나로 합친다. 숫자는 고객지원이 준 값을 더하기만 한다."""
    merged = {**outputs[0]}
    merged["requested"] = sum(o.get("requested", 0) for o in outputs)
    merged["results"] = [r for o in outputs for r in o.get("results", [])]
    merged["stopped_early"] = any(o.get("stopped_early") for o in outputs)
    merged["notes"] = [n for o in outputs for n in o.get("notes", [])]
    return merged


def _compensation_gate(inc: Incident) -> tuple[str, str, str] | None:
    if _out(inc, "impact").get("affected_customer_ids"):
        return None
    return ("skipped", "no_affected_customers", "영향 고객 목록이 없어 보상 요청 없음")


def _kpi(inc: Incident) -> dict[str, Any]:
    return {"context_ids": [inc.context_id]}


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
                gate=_rollback_gate,
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
                keep={"notice_id": "notice_id"},
            ),
            Step(
                "compensation",
                "request_incident_compensation",
                "영향 고객 일괄 보상 (support)",
                after=("impact",),
                build=_compensation,
                gate=_compensation_gate,
                split=("buyer_ids", COMPENSATION_BATCH),  # 고객지원이 한 번에 50명까지 받는다
                merge=_merge_compensation,
                required=False,
            ),
            Step(
                "recovery",
                "verify_recovery",
                "복구 검증 (infra)",
                after=("rollback",),
                build=_recovery,
                verdict=_recovery_verdict,
                timeout=RECOVERY_TIMEOUT_SECONDS,
            ),
            Step(
                "recovered_draft",
                "draft_notice",
                "정상화 공지 초안 (marketing)",
                after=("recovery",),
                build=_recovered_draft,
            ),
            Step(
                "recovered_notice",
                "publish_notice",
                "정상화 공지 (marketing)",
                after=("recovered_draft",),
                build=_recovered_notice,
                keep={"notice_id": "recovered_notice_id"},
            ),
            Step(
                "kpi",
                "kpi_summary",
                "처리 시간 요약 (data)",
                also_after=("recovered_notice",),
                build=_kpi,
                required=False,
            ),
            Step(
                "record",
                "record_case",
                "사건 기록 (hr)",
                also_after=(
                    "recovered_notice",
                    "link_incident",
                    "compensation",
                    "kpi",
                ),
                kind="record",
            ),
        ],
    )
