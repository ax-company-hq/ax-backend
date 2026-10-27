"""고객 경로 5단계 (단독·연결). 총괄이 부르지 않는 경로라 총괄 연결(12단계)과 따로 점수를 낸다.

단독: 단계마다 그 부서를 고정 입력으로 직접 호출한다.
연결: 고객 문의 하나를 support에 넣고, 이후 단계가 support의 A2A 호출로 이어지는지 공통 로그로 본다
     (support → marketing issue_coupon → 안전 레이어 코드 규칙으로 A 판정·바로 지급(재무 호출 없음),
     support → marketing get_notice).
쿠폰 등급 (#319 재무 재설계, 인수 기준 A-5·A-6): 쿠폰 지급은 교차검증 없이 안전 레이어 코드 규칙으로
판정한다. 한도 안이면 A 실행, 한도 초과·금액 불일치면 C. finance `verify_amount`는 부르지 않는다.
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
# 고객지원이 마케팅에 보내는 보상 쿠폰 (한도 안: 1장 × 2,000원)
COUPON = {
    "purpose_code": "compensation",
    "buyer_id": "buy_0001",
    "count": 1,
    "unit_value": 2000,
    "claimed_total": 2000,
}
GRADE_KEY = "coupon_grade"
EXPECTED_GRADE = ("A", "executed", None)  # (등급, 상태, 검증자): 교차검증 없이 바로 지급


def incident_link_ready() -> bool:
    """support Agent Card에 사건 조회 skill이 있는지 (없으면 고객 경로 연결 판정은 pending)."""
    from ax_e2e.scenario_a import paths

    return any(k in paths.card_skills("support") for k in INCIDENT_LOOKUP_SKILLS)


def _stage(stages: list[Stage], key: str) -> Stage:
    return next(s for s in stages if s.path == CUSTOMER and s.key == key)


def _absent(stage: Stage, apps: harness.Apps) -> Outcome | None:
    """쿠폰 등급 단계는 안전 레이어가 판정하므로 카드 대신 판정 대상(마케팅 issue_coupon)을 본다."""
    if stage.key != GRADE_KEY:
        return absent(stage, apps)
    from ax_e2e.scenario_a import paths

    if "issue_coupon" not in paths.card_skills("marketing"):
        return Outcome("absent", "marketing Agent Card에 issue_coupon 없음")
    if "marketing" in apps.errors:
        return Outcome("fail", apps.errors["marketing"])
    return None


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


def _coupon_params() -> dict[str, Any]:
    """마케팅 issue_coupon이 안전 관문에 보내는 params와 같은 모양 (총액은 코드로 계산)."""
    params = {k: v for k, v in COUPON.items() if k != "claimed_total"}
    params["total_value"] = COUPON["count"] * COUPON["unit_value"]
    params["claimed_total"] = COUPON["claimed_total"]
    return params


async def gate_decision(ctx: str) -> tuple[tuple[str, str, str | None], list[str]]:
    """안전 관문 코드 규칙으로 보상 쿠폰을 판정한다: ((등급, 상태, 검증자), 부른 검증자 skill 목록).

    mock e2e는 `AX_SAFETY_URL`이 없어 마케팅 execute()가 가짜 A를 받는다. 그래서 안전 관문
    판정기(Gate)를 프로세스 안에서 같은 요청으로 직접 부른다. 앱(create_app)은 에이전트 이름을
    바꾸므로 쓰지 않는다.
    검증자 호출은 가로채 기록만 한다 (재무 verify_amount가 불리면 목록에 남는다).
    """
    from ax_contracts.schemas import ExecuteRequest
    from ax_safety.gate import Gate
    from ax_safety.rules import load_registry, load_thresholds
    from ax_safety.store import Store

    called: list[str] = []

    async def verifier(task):  # noqa: ANN001 (safety VerifyTask)
        called.append(f"{task.verifier}.{task.skill}")
        raise RuntimeError("e2e: 쿠폰 판정에 검증자를 부르면 안 된다")

    async def publisher(*_: Any) -> None:
        return None

    gate = Gate(load_registry(), load_thresholds(), Store(), verifier, publisher)
    decision = await gate.execute(
        ExecuteRequest(
            action="issue_coupon",
            requester="marketing",
            context_id=ctx,
            idempotency_key=f"{ctx}:issue_coupon:{COUPON['buyer_id']}",
            params=_coupon_params(),
        )
    )
    return (decision.grade, decision.status, decision.verifier), called


async def _real_gate_decision(
    ctx: str, execution_id: str | None
) -> tuple[str, str, str | None] | None:
    """real 모드: 실서버 안전 관문의 실행 기록에서 이 쿠폰의 판정을 읽는다 (GET만)."""
    import httpx

    from ax_agent_template.config import agent_url

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{agent_url('safety')}/api/safety/executions",
            params={"context_id": ctx},
            timeout=10,
        )
    if resp.status_code != 200:
        return None
    for rec in resp.json().get("data") or []:
        req, dec = rec.get("request") or {}, rec.get("decision") or {}
        if req.get("action") == "issue_coupon" and (
            execution_id is None or rec.get("execution_id") == execution_id
        ):
            return dec.get("grade"), dec.get("status"), dec.get("verifier")
    return None


async def _unit_coupon_grade(coupon: Outcome | None, body: dict | None, ctx: str) -> Outcome:
    """issue_coupon 단독 결과가 안전 관문 A·실행(executed)으로 끝났는지 본다."""
    if coupon is None or coupon.state != "pass" or not body:
        return Outcome("blocked", "쿠폰 지급 단독 호출이 completed가 아님", ["issue_coupon"])
    if body.get("outcome"):  # held·verifying·rejected_by_review 등은 실행이 아니다
        return Outcome("fail", f"쿠폰이 바로 지급되지 않음: outcome={body.get('outcome')}")
    output = body.get("output") or {}
    if not output.get("coupon_id"):
        return Outcome("fail", "issue_coupon 결과에 coupon_id 없음 (실행되지 않음)")
    refs = (body.get("evidence") or {}).get("ref_ids") or []
    execution_id = refs[1] if len(refs) > 1 else None  # 마케팅: [coupon_id, execution_id]
    try:
        if harness.e2e_mode() == "real" and harness.real_host():
            got = await _real_gate_decision(ctx, execution_id)
            called: list[str] = []
            if got is None:
                return Outcome("fail", "안전 관문 실행 기록에서 이 쿠폰 판정을 찾지 못함")
        else:
            got, called = await gate_decision(ctx)
    except Exception as exc:  # 안전 관문 코드가 깨져도 다른 단계 점수는 낸다
        return Outcome("fail", f"안전 관문 판정 실패: {type(exc).__name__}: {exc}"[:300])
    if called:
        return Outcome("fail", f"쿠폰 판정에 검증자 호출이 있음: {', '.join(called)}")
    if got != EXPECTED_GRADE:
        return Outcome("fail", f"안전 관문 판정 {got} (기대: A·executed·검증자 없음)")
    return Outcome("pass", checks={"grade": "A", "status": "executed", "verifier": "none"})


async def run_unit(
    stages: list[Stage], apps: harness.Apps, notice_id: str | None
) -> dict[str, Outcome]:
    out: dict[str, Outcome] = {}
    inquiry_id: int | None = None
    coupon_body: dict | None = None
    for stage in (s for s in stages if s.path == CUSTOMER):
        skip = _absent(stage, apps)
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
            body = await _call(stage, dict(COUPON), ctx, sequence=1, key=f"{ctx}:coupon")
            coupon_body = body
        elif stage.key == GRADE_KEY:
            out[stage.key] = await _unit_coupon_grade(out.get("issue_coupon"), coupon_body, ctx)
            continue
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


def coupon_done(records: list | None) -> bool:
    """마케팅 issue_coupon이 바로 지급까지 끝났는지: 처리 완료(result_sent completed, 보류·거절
    outcome 없음) 또는 coupon.issued 발행 기록."""
    for r in records or []:
        if r.source != "marketing":
            continue
        payload = r.payload or {}
        if (
            r.kind == "result_sent"
            and r.skill == "issue_coupon"
            and r.task_status == "completed"
            and payload.get("outcome") in (None, "linked_duplicate")
        ):
            return True
        if r.kind == "event_published" and payload.get("event_type") == "coupon.issued":
            return True
    return False


def finance_verified(records: list | None) -> bool:
    """재무 verify_amount 호출(요청 받음·결과·실패 기록 무엇이든)이 있는지."""
    return any(r.skill == "verify_amount" for r in records or [])


def chain_coupon_grade(out: dict[str, Outcome], records: list | None) -> Outcome:
    """연결: 같은 사건 공통 로그에 쿠폰 지급 완료가 있고 재무 verify_amount 요청은 없는지."""
    if out.get("judge") and out["judge"].state != "pass":
        return Outcome("blocked", "보상 판정 실패", ["judge"])
    if records is None:
        return Outcome("pending", "공통 로그 기록기 없음")
    if finance_verified(records):
        return Outcome(
            "fail",
            "재무 verify_amount 호출이 공통 로그에 있음 (쿠폰은 A, 교차검증 없음 #319)",
            checks={"common_log": "fail"},
        )
    if not coupon_done(records):
        return Outcome(
            "fail",
            "마케팅 issue_coupon 처리 완료(또는 coupon.issued)가 공통 로그에 없음",
            checks={"common_log": "fail"},
        )
    return Outcome("pass", checks={"common_log": "pass"})


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
        skip = _absent(stage, apps)
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
    if GRADE_KEY not in out:
        out[GRADE_KEY] = chain_coupon_grade(out, records)
    return out
