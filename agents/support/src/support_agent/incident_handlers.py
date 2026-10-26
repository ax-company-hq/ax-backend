"""`request_incident_compensation`: 사건(장애)으로 피해를 본 고객 전원에게 보상 쿠폰을 요청한다.

SCN-A 8단계. 영향 고객(데이터분석 영향 분석)은 문의를 안 낸 사람도 있어서
`request_compensation`(문의가 있어야 한다)으로는 보낼 수 없다. 총괄만 사건(`ctx_inc_`) 안에서
부를 수 있고, 단가·건수 상한은 정책표가 정한다. 고객 한 명마다 마케팅 `issue_coupon`(B 등급,
재무 검증·한도 초과는 C 보류)에 한 건씩 직렬로 요청하며, 지급은 마케팅이 한다.
같은 사건·구매자·사유는 한 번만 요청한다 (`request_compensation`과 같은 키라 문의 경로로
이미 보상한 고객은 다시 보내지 않는다).
"""

from __future__ import annotations

import re

from ax_agent_template.server import error_result
from ax_contracts.schemas import AgentRequest, AgentResult, Evidence, TaskStatus
from support_agent import buyer_ids, compensation
from support_agent import handlers as h
from support_agent.incident_schemas import (
    BUYER_PATTERN,
    IncidentCompensationOutput,
    IncidentCouponResult,
    RequestIncidentCompensationInput,
)

INCIDENT_CONTEXT_PREFIX = "ctx_inc_"
NOT_REACHED = ("failed", "canceled")  # 마케팅이 응답하지 않으면(output 없음) 남은 요청을 멈춘다


def clean_buyers(values: list[str]) -> list[str] | None:
    """구매자 ID를 `buy_<4자리>`로 맞추고 중복을 뺀다. 형식이 틀린 값이 있으면 None이다."""
    cleaned: list[str] = []
    for value in values:
        buyer = buyer_ids.normalize(value)
        if not re.fullmatch(BUYER_PATTERN, buyer):
            return None
        if buyer not in cleaned:
            cleaned.append(buyer)
    return cleaned


def _key(context_id: str, buyer_id: str, reason_code: str) -> str:
    return f"{context_id}:request_compensation:{buyer_id}:{reason_code}"


async def request_incident_compensation(request: AgentRequest) -> AgentResult:
    parsed = h._parse(request, RequestIncidentCompensationInput)
    if isinstance(parsed, AgentResult):
        return parsed
    context_id = request.meta.context_id
    if not context_id.startswith(INCIDENT_CONTEXT_PREFIX):
        return error_result(
            request.meta, TaskStatus.FAILED, "invalid_input", "사건(ctx_inc_) 요청에서만 받는다"
        )
    buyers = clean_buyers(parsed.buyer_ids)
    if buyers is None:
        return error_result(
            request.meta, TaskStatus.FAILED, "invalid_input", "구매자 ID 형식이 맞지 않는다"
        )
    decision = compensation.decide(parsed.reason_code, parsed.count)
    if not decision.eligible:
        return h._done(
            request, {"eligible": False, "reason": decision.reason}, outcome="not_eligible"
        )

    ref_ids = [f"buyer:{b}" for b in buyers]
    if parsed.impact_ref:
        ref_ids.append(parsed.impact_ref)
    evidence = Evidence(
        queries=["compensation_policy", "impact_analysis"],
        data_summary=(
            f"{parsed.reason_code}: {len(buyers)}명 × {decision.count}건 × {decision.unit_value}원"
        ),
        ref_ids=ref_ids,
    )

    results: list[IncidentCouponResult] = []
    stopped = False
    async with h.LEDGER.lock(context_id):  # 같은 사건의 요청은 한 건씩 (sequence가 이어지게)
        for buyer in buyers:
            if stopped:
                results.append(IncidentCouponResult(buyer_id=buyer, status="skipped"))
                continue
            key = _key(context_id, buyer, parsed.reason_code)
            known = key in h.LEDGER.results
            sent = await h._request_coupon(request, buyer, decision, key, evidence)
            output = sent.output
            marketing = output.get("marketing_output") or {}
            marketing_status = output.get("marketing_status")
            coupon_id = marketing.get("coupon_id") if isinstance(marketing, dict) else None
            reached = not (marketing_status in NOT_REACHED and not marketing)
            if known:
                status = "duplicate"
            elif marketing_status == "completed" and sent.outcome in (None, "linked_duplicate"):
                status = "requested"
            elif marketing_status == "input-required":  # 한도 초과로 사람 승인을 기다린다 (C 보류)
                status = "held"
            else:
                status = "failed"
            results.append(
                IncidentCouponResult(
                    buyer_id=buyer,
                    status=status,
                    sequence=output.get("sequence"),
                    marketing_status=marketing_status,
                    marketing_outcome=output.get("marketing_outcome"),
                    coupon_id=coupon_id,
                    total_value=decision.total_value if status == "requested" else 0,
                )
            )
            if not reached:
                stopped = True

    bad = [r for r in results if r.status in ("failed", "skipped")]
    body = IncidentCompensationOutput(
        reason_code=parsed.reason_code,
        unit_value=decision.unit_value,
        count=decision.count,
        requested=sum(1 for r in results if r.status == "requested"),
        results=results,
        stopped_early=stopped,
        notes=["지급은 마케팅 issue_coupon이 재무 검증과 한도 검사를 거쳐 한다"],
    )
    return h._done(
        request,
        body.model_dump(mode="json"),
        outcome="partial" if bad else "requested",
        evidence=evidence,
    )


INCIDENT_HANDLERS = {"request_incident_compensation": request_incident_compensation}
