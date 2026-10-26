"""사건별 KPI 계산 (kpi_summary). 순수 함수이며 LLM을 쓰지 않는다.

정의
- 사건: `ctx_inc_` context_id의 기록 묶음. 입력 `context_ids`를 주면 그 context만 사건으로 본다.
  사건이 아닌 기록(context_id 없음, `ctx_req_` 등)은 사건 목록과 합계에서 뺀다.
- detected_at: 사건의 가장 이른 기록 시각.
- recovered_at: `event_published`(event_type=incident.resolved) 첫 시각. 총괄은 서버·업무 지표가
  둘 다 정상일 때만 발행하므로 1순위다. 없으면 `verify_recovery` 완료(`result_sent`, completed)
  첫 시각(복구 여부는 로그에 없어 미확인), 그것도 없으면 None.
- resolved_notice_at: 두 번째로 완료된 `publish_notice`(정상화 공지) 시각. 없으면 None.
- mttr_seconds: recovered_at − detected_at (정수 초). recovered_at이 없으면 None.
- used_memory: 사건에 `memory_referenced` 기록이 있으면 True.
- auto_handled_rate: `escalated` 기록이 없는 사건 수 / 사건 수. 사건이 0이면 0.0.
- grade_counts: `execute_decided`의 grade별 건수.
- blocked_counts: `rejected`의 `payload.reason_code`별 건수 (없으면 "UNKNOWN").
- escalation_count: `escalated` 기록 수.

값을 정할 수 없으면 추측하지 않고 None·0으로 두고 `notes`에 "확인할 수 없음" 사유를 남긴다.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from ax_contracts.data_access.common_log import LogRecord
from ax_contracts.schemas.data import IncidentKpi, KpiSummaryOutput

INCIDENT_PREFIX = "ctx_inc_"
RESOLVED_EVENT = "incident.resolved"
RECOVERY_SKILL = "verify_recovery"
NOTICE_SKILL = "publish_notice"
UNVERIFIED_RECOVERY = "복구 여부 미확인(verify 완료 기준)"


@dataclass(frozen=True)
class KpiComputation:
    output: KpiSummaryOutput
    notes: list[str] = field(default_factory=list)
    # context_id → "incident.resolved" | "verify_recovery" | None
    recovery_basis: dict[str, str | None] = field(default_factory=dict)


def _times(records: list[LogRecord], predicate: Callable[[LogRecord], bool]) -> list[datetime]:
    return sorted(r.occurred_at for r in records if predicate(r))


def _is_resolved(r: LogRecord) -> bool:
    return r.kind == "event_published" and r.payload.get("event_type") == RESOLVED_EVENT


def _completed(skill: str) -> Callable[[LogRecord], bool]:
    def predicate(r: LogRecord) -> bool:
        return r.kind == "result_sent" and r.skill == skill and r.task_status == "completed"

    return predicate


def _recovery(records: list[LogRecord]) -> tuple[datetime | None, str | None]:
    if resolved := _times(records, _is_resolved):
        return resolved[0], RESOLVED_EVENT
    if verified := _times(records, _completed(RECOVERY_SKILL)):
        return verified[0], RECOVERY_SKILL
    return None, None


def _is_incident(context_id: str | None, explicit: set[str]) -> bool:
    if context_id is None:
        return False
    return context_id in explicit if explicit else context_id.startswith(INCIDENT_PREFIX)


def compute_kpi(records: list[LogRecord], context_ids: list[str] | None = None) -> KpiComputation:
    """로그 기록에서 사건별 KPI와 합계를 계산한다. 사건은 detected_at 순으로 정렬한다."""
    explicit = set(context_ids or [])
    by_context: dict[str, list[LogRecord]] = {}
    for r in records:
        if _is_incident(r.context_id, explicit):
            by_context.setdefault(r.context_id, []).append(r)  # type: ignore[arg-type]

    notes: list[str] = []
    basis: dict[str, str | None] = {}
    incidents: list[IncidentKpi] = []
    for context_id, recs in by_context.items():
        detected_at = min(r.occurred_at for r in recs)
        recovered_at, basis[context_id] = _recovery(recs)
        notices = _times(recs, _completed(NOTICE_SKILL))
        notice_at = notices[1] if len(notices) >= 2 else None
        if basis[context_id] == RECOVERY_SKILL:
            notes.append(f"{context_id}: {UNVERIFIED_RECOVERY}")
        elif recovered_at is None:
            notes.append(
                f"{context_id}: 복구 시각 확인할 수 없음 "
                f"({RESOLVED_EVENT} 발행·{RECOVERY_SKILL} 완료 로그 없음)"
            )
        if notice_at is None:
            notes.append(
                f"{context_id}: 정상화 공지 확인할 수 없음 "
                f"({NOTICE_SKILL} 완료 {len(notices)}건, 두 번째 없음)"
            )
        mttr = int((recovered_at - detected_at).total_seconds()) if recovered_at else None
        incidents.append(
            IncidentKpi(
                context_id=context_id,
                detected_at=detected_at,
                recovered_at=recovered_at,
                resolved_notice_at=notice_at,
                mttr_seconds=mttr,
                used_memory=any(r.kind == "memory_referenced" for r in recs),
            )
        )
    incidents.sort(key=lambda i: (i.detected_at, i.context_id))

    in_incident = [r for recs in by_context.values() for r in recs]
    decided = [r for r in in_incident if r.kind == "execute_decided"]
    rejected = [r for r in in_incident if r.kind == "rejected"]
    escalated = [r for r in in_incident if r.kind == "escalated"]
    for name, kind, found in (
        ("grade_counts", "execute_decided", decided),
        ("blocked_counts", "rejected", rejected),
        ("escalation_count", "escalated", escalated),
    ):
        if incidents and not found:
            notes.append(f"{name}: 확인할 수 없음: 해당 종류 로그 0건 ({kind})")

    escalated_contexts = {r.context_id for r in escalated}
    rate = (len(incidents) - len(escalated_contexts)) / len(incidents) if incidents else 0.0
    grades = Counter(r.grade for r in decided if r.grade)
    blocked = Counter(str(r.payload.get("reason_code") or "UNKNOWN") for r in rejected)
    output = KpiSummaryOutput(
        incidents=incidents,
        auto_handled_rate=rate,
        grade_counts=dict(grades),
        blocked_counts=dict(blocked),
        escalation_count=len(escalated),
    )
    return KpiComputation(output=output, notes=notes, recovery_basis=basis)
