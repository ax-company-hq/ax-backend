"""고객 경로 5단계 (단독·연결). 총괄이 부르지 않는 경로라 총괄 연결(12단계)과 따로 점수를 낸다.

단독: 단계마다 그 부서를 고정 입력으로 직접 호출한다.
연결: 고객 문의 하나를 support에 넣고, 이후 단계가 support의 A2A 호출로 이어지는지 공통 로그로 본다
     (support → marketing issue_coupon → finance verify_amount, support → marketing get_notice).
contextId (결정 #258 9번 = ①): 고객 문의가 진행 중 사건의 contextId(`ctx_inc_`)를 이어받는다.
연결 판정은 "각 단계가 사건 contextId 하나로 이어지는지"다.
support가 사건을 조회해 ctx를 붙이는 기능
(#272 사건 조회 skill, #274 support 요청)이 머지되기 전에는 연결 판정을 pending으로 둔다.
단독 호출은 단계마다 부서를 직접 부르므로 `ctx_ui_` 고정 입력을 그대로 쓴다.
"""

from __future__ import annotations

from typing import Any

from ax_e2e.scenario_a import chain, harness
from ax_e2e.scenario_a.result import Outcome
from ax_e2e.scenario_a.stages import CUSTOMER, Stage
from ax_e2e.scenario_a.unit import absent, classify

BLOCKED_NOTE = "blocked-by #272 #274"
# support가 사건 조회 skill로 사건 contextId를 붙일 수 있게 되면 카드에 이 skill 중 하나가 생긴다
# (이름은 #272/#274가 정한다. 정해지면 이 목록을 고친다)
INCIDENT_LOOKUP_SKILLS = ("get_incident", "find_open_incident", "lookup_incident")
FALLBACK_CTX = "ctx_ui_e2e0001"
UNIT_CTX = "ctx_ui_e2e_unit01"
INQUIRY = {"user_id": 1, "customer_name": "홍길동"}
INQUIRY_TEXT = {"subject": "결제가 안돼요", "message": "결제 오류로 주문을 못했어요"}
COMPENSATION = {"buyer_id": "buy_0001", "reason_code": "late_delivery", "count": 1}
VERIFY = {
    "execution_id": "exe_e2e0001",
    "action": "issue_coupon",
    "requester": "marketing",
    "params": {
        "purpose_code": "compensation",
        "buyer_id": "buy_0001",
        "count": 1,
        "unit_value": 2000,
        "claimed_total": 2000,
    },
    "computed_amount": 2000,
    "threshold_version": 0,
}


def incident_link_ready() -> bool:
    """support Agent Card에 사건 조회 skill이 있는지 (없으면 고객 경로 연결 판정은 pending)."""
    from ax_e2e.scenario_a import paths

    return any(k in paths.card_skills("support") for k in INCIDENT_LOOKUP_SKILLS)


def _stage(stages: list[Stage], key: str) -> Stage:
    return next(s for s in stages if s.path == CUSTOMER and s.key == key)


async def _call(
    stage: Stage,
    data: dict,
    ctx: str,
    *,
    untrusted: dict | None = None,
    sequence: int | None = None,
    key: str | None = None,
) -> dict:
    req = harness.build_request(
        stage.skill,
        data,
        requester=stage.requester,
        context_id=ctx,
        untrusted=untrusted,
        sequence=sequence,
        idempotency_key=key,
    )
    return await harness.call_dept(stage.dept, req)


async def run_unit(
    stages: list[Stage], apps: harness.Apps, notice_id: str | None
) -> dict[str, Outcome]:
    out: dict[str, Outcome] = {}
    inquiry_id: int | None = None
    for stage in (s for s in stages if s.path == CUSTOMER):
        skip = absent(stage, apps)
        if skip:
            out[stage.key] = skip
            continue
        ctx = UNIT_CTX
        if stage.key == "inquiry":
            body = await _call(stage, INQUIRY, ctx, untrusted=INQUIRY_TEXT)
            inquiry_id = (body.get("output") or {}).get("id")
        elif stage.key == "judge":
            if inquiry_id is None:
                out[stage.key] = Outcome("blocked", "문의 접수 단독 호출이 실패", ["inquiry"])
                continue
            body = await _call(
                stage, {"inquiry_id": inquiry_id, **COMPENSATION}, ctx, key=f"{ctx}:comp"
            )
        elif stage.key == "issue_coupon":
            data = {
                "purpose_code": "compensation",
                "buyer_id": "buy_0001",
                "count": 1,
                "unit_value": 2000,
                "claimed_total": 2000,
            }
            body = await _call(stage, data, ctx, sequence=1, key=f"{ctx}:coupon")
        elif stage.key == "finance_check":
            body = await _call(stage, VERIFY, ctx)
        else:  # notice_reply: get_notice
            if not notice_id:
                out[stage.key] = Outcome(
                    "blocked", "공지 게시 단독 호출이 실패", ["publish_notice"]
                )
                continue
            body = await _call(stage, {"notice_id": notice_id}, ctx)
        res = classify(stage, apps, body, ctx)
        if (
            stage.key == "notice_reply"
            and res.state == "pass"
            and not (body.get("output") or {}).get("found")
        ):
            res = Outcome("fail", "get_notice가 공지를 찾지 못함")
        out[stage.key] = res
    return out


def _has(records: list | None, dept: str, skill: str, requester: str, kind: str) -> bool:
    return any(
        r.source == dept and r.skill == skill and r.requester == requester and r.kind == kind
        for r in records or []
    )


async def run_chain(
    stages: list[Stage], apps: harness.Apps, notice_id: str | None, incident_ctx: str | None = None
) -> dict[str, Outcome]:
    """고객 문의 하나를 support에 넣고 5단계가 사건 contextId 하나로 이어지는지 본다."""
    if not incident_link_ready() or not incident_ctx:
        return {s.key: Outcome("pending", BLOCKED_NOTE) for s in stages if s.path == CUSTOMER}
    CUSTOMER_CTX = incident_ctx  # noqa: N806 (사건 contextId를 이어받는다)
    out: dict[str, Outcome] = {}
    cs = {s.key: s for s in stages if s.path == CUSTOMER}
    for key, stage in cs.items():
        skip = absent(stage, apps)
        if skip:
            out[key] = skip
    sup = cs["inquiry"]
    inquiry_id: int | None = None
    if "inquiry" not in out:
        body = await _call(sup, INQUIRY, CUSTOMER_CTX, untrusted=INQUIRY_TEXT)
        res = classify(sup, apps, body, CUSTOMER_CTX)
        inquiry_id = (body.get("output") or {}).get("id") if res.state == "pass" else None
        out["inquiry"] = res
    judged: dict[str, Any] = {}
    if "judge" not in out:
        if inquiry_id is None:
            out["judge"] = Outcome("blocked", "문의 접수 실패", ["inquiry"])
        else:
            body = await _call(
                cs["judge"],
                {"inquiry_id": inquiry_id, **COMPENSATION},
                CUSTOMER_CTX,
                key=f"{CUSTOMER_CTX}:comp",
            )
            res = classify(cs["judge"], apps, body, CUSTOMER_CTX)
            judged = body.get("output") or {}
            if res.state == "pass" and not judged.get("eligible"):
                res = Outcome("fail", f"보상 대상 아님: {judged.get('reason')}")
            out["judge"] = res
    if inquiry_id is not None and "notice_reply" not in out:
        if notice_id:
            req = harness.build_request(
                "add_customer_message",
                {"inquiry_id": inquiry_id, "notice_id": notice_id},
                requester="shop-api",
                context_id=CUSTOMER_CTX,
                untrusted={"message": "공지 보고 문의합니다"},
            )
            await harness.call_dept("support", req)
    records = chain.read_log(CUSTOMER_CTX)
    for key, dept, skill, req, kind, why in (
        (
            "issue_coupon",
            "marketing",
            "issue_coupon",
            "support",
            "request_received",
            "support의 issue_coupon 요청이 공통 로그에 없음",
        ),
        (
            "finance_check",
            "finance",
            "verify_amount",
            "safety",
            "request_received",
            "재무 교차검증(verify_amount) 호출이 공통 로그에 없음 (B 등급 검증이 이어지지 않음)",
        ),
        (
            "notice_reply",
            "marketing",
            "get_notice",
            "support",
            "request_received",
            "support의 get_notice 호출이 공통 로그에 없음",
        ),
    ):
        if key in out:
            continue
        if out.get("judge") and out["judge"].state != "pass" and key != "notice_reply":
            out[key] = Outcome("blocked", "보상 판정 실패", ["judge"])
        elif key == "notice_reply" and not notice_id:
            out[key] = Outcome("blocked", "게시된 공지가 없음", ["publish_notice"])
        elif records is None:
            out[key] = Outcome("pending", "공통 로그 기록기 없음")
        elif _has(records, dept, skill, req, kind):
            out[key] = Outcome("pass", checks={"common_log": "pass"})
        else:
            out[key] = Outcome("fail", why, checks={"common_log": "fail"})
    return out
