"""사건별 재무 화면 (재설계 문서 12장).

| 메서드·경로 | 하는 일 |
| --- | --- |
| `GET /api/finance/incidents` | 재무가 아는 사건 목록 (장부·관문 기록의 `ctx_inc_`) |
| `GET /api/finance/incidents/{context_id}` | 영향·보상 쿠폰·한도·점검 |

- 재무는 장애 대응 흐름의 단계가 아니다 (요구사항 DEP-FIN-01, 공통 구성안 결정 22).
  2026-10-27에 손실 집계(`loss`)와 사건 단계 표시(`steps` 9·10·17)를 뺐다. 장애 비용은
  평상시 장부로 들어가 결산(`closing_report`)·사후 감사(`audit_report`)에서 드러난다.
- 영향(`impact`)은 data `impact_analysis` 결과 그대로다(실제 모드 A2A, 목 모드 시드).
  다시 계산하지 않는다. 실제 모드에서는 장애 구간을 모르므로 null이다.
- 한도는 기준표(`compensation_per_customer`, `compensation_per_context`)를 safety
  `GET /api/safety/thresholds`(목 모드 시드)에서 읽는다. 못 읽으면 null.
- 점검은 코드 규칙이다. 근거 자료가 없으면 `ok: null`(확인할 수 없음)이고 통과로 치지 않는다.
- 상태를 바꾸지 않는다.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ax_agent_template.config import is_mock
from ax_contracts.schemas import RequestMeta, VerifyAmountInput
from finance_agent import finance_config as fc
from finance_agent import ledger as lg
from finance_agent.audit import _BooksSource
from finance_agent.handlers import compensation_ledger, incident_impact, incident_meta, state
from finance_agent.recheck import recompute
from finance_agent.sources import Impact

INCIDENT_PREFIX = "ctx_inc_"
PER_CUSTOMER_KEY = "compensation_per_customer"
PER_INCIDENT_KEY = "compensation_per_context"
RECHECK_FAILURES = {"amount_mismatch", "invalid_params"}


# ---------------------------------------------------------------- 한도
def _rule(thresholds: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
    for r in (thresholds or {}).get("rules") or []:
        if isinstance(r, dict) and r.get("key") == key and isinstance(r.get("value"), int):
            return r
    return None


def _within(used: int, rule: dict[str, Any]) -> bool:
    """기준표 규칙에 걸리지 않으면 True. `op`가 `>=`(기본)면 한도와 같아도 걸린다."""
    limit = rule["value"]
    return used <= limit if rule.get("op") == ">" else used < limit


# ---------------------------------------------------------------- 조립
def _check(code: str, ok: bool | None, title: str, detail: str, refs: list[str]) -> dict:
    return {
        "code": code,
        "ok": ok,
        "title": title,
        "detail": detail,
        "refs": [r for r in dict.fromkeys(refs) if r],
    }


def _by_buyer(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(str(r["buyer_id"]), []).append(r)
    return out


async def _amount_recheck(
    context_id: str, executed: list[dict[str, Any]], executions: dict[str, dict]
) -> dict:
    title = "보상 금액 다시 계산"
    raw = [executions.get(str(r["execution_id"])) for r in executed]
    if any(e is None for e in raw):
        return _check(
            "amount_recheck", None, title, "실행 기록이 없는 쿠폰이 있어 확인할 수 없음", []
        )
    source = _BooksSource({})
    meta = RequestMeta(
        context_id=context_id, token="audit", deadline=fc.now() + timedelta(seconds=10)
    )
    bad = []
    for e in raw:
        assert e is not None
        data = VerifyAmountInput(
            execution_id=str(e.get("execution_id")),
            action="issue_coupon",
            requester=str(e.get("requester")),
            params=e.get("params") or {},
            computed_amount=e.get("computed_amount"),
        )
        result = await recompute(meta, data, source, {})
        if result.reason_code in RECHECK_FAILURES:
            bad.append(str(e.get("execution_id")))
    detail = (
        f"실행된 보상 쿠폰 {len(raw)}건 금액(장 수 × 단가)이 모두 맞음"
        if not bad
        else f"{len(bad)}건의 요청 금액이 다시 계산한 금액과 다름"
    )
    return _check("amount_recheck", not bad, title, detail, bad)


async def incident_view(
    context_id: str, feed: lg.ExecutionFeed, thresholds: dict[str, Any] | None
) -> tuple[dict[str, Any], list[str]]:
    """사건 상세 data와 unavailable 목록."""
    unavailable: list[str] = []
    info = incident_meta(context_id)
    impact: Impact | None = await incident_impact(context_id)
    rows, ledger_source = await compensation_ledger(context_id, feed)
    impact_ids = impact.affected_customer_ids if impact else frozenset()

    if impact is None:
        unavailable.append("impact")
    if rows is None:
        unavailable.append("compensation")
    if thresholds is None:
        unavailable.append("limits")

    coupons = []
    for r in rows or []:
        in_impact = None
        if impact is not None and impact_ids and r.get("buyer_id"):
            in_impact = r["buyer_id"] in impact_ids
        coupons.append(lg.public(r) | {"in_impact": in_impact})
    totals = lg.totals(rows) if rows is not None else None
    executed = [r for r in rows or [] if r["status"] == "executed"]
    buyers_known = all(r.get("buyer_id") for r in executed)

    # 한도
    per_customer_rule = _rule(thresholds, PER_CUSTOMER_KEY)
    per_incident_rule = _rule(thresholds, PER_INCIDENT_KEY)
    spent = {b: sum(x["amount"] or 0 for x in rs) for b, rs in _by_buyer(executed).items()}
    top_buyer = max(sorted(spent), key=lambda b: spent[b]) if spent and buyers_known else None
    max_used = spent[top_buyer] if top_buyer else (0 if rows is not None and not executed else None)
    used = totals["total_executed"] if totals else None
    inc_limit = per_incident_rule["value"] if per_incident_rule else None
    limits = {
        "per_customer": {
            "limit": per_customer_rule["value"] if per_customer_rule else None,
            "max_used": max_used,
            "buyer_id": top_buyer,
        },
        "per_incident": {
            "limit": inc_limit,
            "used": used,
            "remaining": inc_limit - used if inc_limit is not None and used is not None else None,
        },
    }

    # 점검
    checks = []
    exe_refs = [str(r["execution_id"]) for r in executed if r.get("execution_id")]
    if rows is None:
        ok, detail, refs = None, "보상 쿠폰 장부를 읽을 수 없음", []
    elif not executed:
        ok, detail, refs = True, "실행된 보상 쿠폰 없음", []
    elif impact is None or not impact_ids or not buyers_known:
        ok, detail, refs = (
            None,
            "데이터 AI 영향 고객 목록 또는 쿠폰 수령자를 확인할 수 없음",
            exe_refs,
        )
    else:
        outside = [r for r in executed if r["buyer_id"] not in impact_ids]
        ok = not outside
        names = ", ".join(sorted({str(r["buyer_id"]) for r in outside}))
        detail = (
            f"보상 받은 고객 {len(spent)}명 모두 영향 고객 {len(impact_ids)}명 안"
            if ok
            else f"영향 고객이 아닌 {names}에게 보상 쿠폰"
        )
        refs = [str(r["execution_id"]) for r in outside]
    checks.append(
        _check("recipients_in_impact", ok, "보상 받은 고객이 모두 영향 고객인지", detail, refs)
    )

    if rows is None or (executed and not buyers_known):
        ok, detail, refs = None, "쿠폰 수령자를 확인할 수 없음", []
    else:
        dup = {b: rs for b, rs in _by_buyer(executed).items() if len(rs) > 1}
        ok = not dup
        detail = (
            "같은 사건·같은 고객 보상 쿠폰은 1건씩"
            if ok
            else ", ".join(f"{b} {len(rs)}건" for b, rs in sorted(dup.items()))
        )
        refs = [str(r["execution_id"]) for rs in dup.values() for r in rs]
    checks.append(_check("one_coupon_per_buyer", ok, "고객당 쿠폰 1건", detail, refs))

    if per_customer_rule is None or max_used is None:
        ok, detail, refs = None, "기준표 또는 쿠폰 장부를 확인할 수 없음", []
    else:
        ok = _within(max_used, per_customer_rule)
        detail = f"고객 최대 {fc.won(max_used)} / 한도 {fc.won(per_customer_rule['value'])}" + (
            f" ({top_buyer})" if top_buyer else ""
        )
        refs = (
            [] if ok else [str(r["execution_id"]) for r in executed if r["buyer_id"] == top_buyer]
        )
    checks.append(_check("per_customer_limit", ok, "고객당 보상 한도", detail, refs))

    if per_incident_rule is None or used is None:
        ok, detail, refs = None, "기준표 또는 쿠폰 장부를 확인할 수 없음", []
    else:
        ok = _within(used, per_incident_rule)
        detail = f"사건 합계 {fc.won(used)} / 한도 {fc.won(per_incident_rule['value'])}"
        refs = [] if ok else exe_refs
    checks.append(_check("per_incident_limit", ok, "사건당 보상 한도", detail, refs))

    if rows is None:
        checks.append(_check("amount_recheck", None, "보상 금액 다시 계산", "장부 없음", []))
    elif not executed:
        checks.append(_check("amount_recheck", True, "보상 금액 다시 계산", "실행된 쿠폰 없음", []))
    else:
        by_id = {str(e.get("execution_id")): e for e in feed.rows or []}
        checks.append(await _amount_recheck(context_id, executed, by_id))

    window = None
    if info["window_from"] and info["window_to"]:
        window = {"from": fc.iso(info["window_from"]), "to": fc.iso(info["window_to"])}
    impact_out = None
    if impact is not None:
        impact_out = {
            "failed_orders": impact.failed_orders,
            "affected_customers": impact.affected_customers,
            "damage_amount": impact.lost_amount,
            "by_category": [
                {"category": c, "orders": n, "amount": a} for c, n, a in impact.by_category
            ],
            "affected_customer_ids": sorted(impact.affected_customer_ids),
            "source": "data.impact_analysis",
            "collected_at": fc.iso(fc.now()),
        }
    data = {
        "context_id": context_id,
        "component": info.get("component"),
        "window": window,
        "impact": impact_out,
        "compensation": {"coupons": coupons, **(totals or dict.fromkeys(_TOTAL_KEYS))},
        "limits": limits,
        "checks": checks,
        "ledger_source": ledger_source,
    }
    return data, unavailable


_TOTAL_KEYS = ("total_executed", "total_held", "count_executed", "count_held")


def checks_ok(checks: list[dict[str, Any]]) -> bool | None:
    values = [c["ok"] for c in checks]
    if False in values:
        return False
    return None if None in values else True


def known_incidents(feed: lg.ExecutionFeed) -> list[str]:
    """장부(이벤트)·안전 관문 실행 기록에 나온 사건 번호."""
    ledger = state().ledger
    ids = set(ledger.coupon_events) | set(ledger.coupons)
    ids |= {str(e.get("context_id")) for e in feed.rows or [] if e.get("context_id")}
    return sorted(i for i in ids if i.startswith(INCIDENT_PREFIX))


def overall_source(feed: lg.ExecutionFeed, used: list[str | None]) -> str | None:
    """목록 meta의 `ledger_source`: 실행 기록 출처 → 이벤트 → 둘 다 없으면 None."""
    if feed.rows is not None:
        return feed.source
    if lg.events_connected() or lg.LEDGER_EVENTS in used:
        return lg.LEDGER_EVENTS
    return None


def _opened_at(detail: dict[str, Any], feed: lg.ExecutionFeed) -> str | None:
    if detail["window"]:
        return detail["window"]["from"]
    times = [c["at"] for c in detail["compensation"]["coupons"] if c.get("at")]
    times += [
        fc.iso(e.get("created_at"))
        for e in feed.rows or []
        if e.get("context_id") == detail["context_id"] and e.get("created_at")
    ]
    times = [t for t in times if t]
    return min(times) if times else None


def summary_row(detail: dict[str, Any], feed: lg.ExecutionFeed) -> dict[str, Any]:
    comp = detail["compensation"]
    return {
        "context_id": detail["context_id"],
        "component": detail["component"],
        "opened_at": _opened_at(detail, feed),
        "damage_amount": (detail["impact"] or {}).get("damage_amount"),
        "compensation_total": comp["total_executed"],
        "coupon_count": comp["count_executed"],
        "held_count": comp["count_held"],
        "checks_ok": checks_ok(detail["checks"]),
    }


# ---------------------------------------------------------------- 라우트
def add_incident_api(app: FastAPI) -> None:
    @app.get("/api/finance/incidents")
    async def list_incidents() -> dict:
        feed = await state().executions_loader(None)
        thresholds = await state().thresholds_loader()
        rows, used = [], []
        for ctx in known_incidents(feed):
            detail, _ = await incident_view(ctx, feed, thresholds)
            used.append(detail["ledger_source"])
            rows.append(summary_row(detail, feed))
        rows.sort(key=lambda r: r["opened_at"] or "", reverse=True)
        meta: dict[str, Any] = {
            "mock": is_mock("finance"),
            "collected_at": fc.iso(fc.now()),
            "ledger_source": overall_source(feed, used),
            "unavailable": [] if feed.rows is not None else ["safety_executions"],
        }
        return {"data": rows, "meta": meta}

    @app.get("/api/finance/incidents/{context_id}", response_model=None)
    async def get_incident(context_id: str) -> dict | JSONResponse:
        feed = await state().executions_loader(context_id)
        if context_id not in known_incidents(feed):
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": f"재무가 아는 사건이 아니다: {context_id}",
                    }
                },
            )
        thresholds = await state().thresholds_loader()
        data, unavailable = await incident_view(context_id, feed, thresholds)
        meta = {
            "mock": is_mock("finance"),
            "collected_at": fc.iso(fc.now()),
            "unavailable": unavailable,
        }
        return {"data": data, "meta": meta}
