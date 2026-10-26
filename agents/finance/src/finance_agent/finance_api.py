"""재무 대시보드 API (재무 재설계 6·11장). 응답은 `{data, meta}`, 실패는 `{error: {code, message}}`.

| 메서드·경로 | 하는 일 | 토큰 |
| --- | --- | --- |
| `GET /api/finance/overview` | 오늘 요약(KPI)과 사람이 할 일 | 없음 |
| `GET /api/finance/closing?period=&date=` | ① 손익·분개 미리보기·설명 | 없음 |
| `POST /api/finance/closing/confirm` | ① 결산 확정 기록 | **사람** |
| `GET /api/finance/reconciliation?date=` | ② 결제별 PG 대사 (생략 = 최근 7일) | 없음 |
| `POST /api/finance/reconciliation/{payment_id}/note` | ② 불일치 처리 메모 | **사람** |
| `GET /api/finance/settlements?period=YYYY-Www` | ③ 판매자 정산 내역서 (생략 = 지난주) | 없음 |
| `POST /api/finance/settlements/batch` | ③ 묶음 → `execute(confirm_settlement)` | 선택 |
| `GET /api/finance/policy` | ⑤ 정책표·소진율·한도 제안 | 없음 |
| `GET /api/finance/audit?days=7&context_id=` | ⑦ 사후 감사 (사건 하나로 좁히기 선택) | 없음 |

`meta`: `mock`, `collected_at`, `source`(`seed`|`shop-api`), `unavailable`(못 받은 자료 이름).
정산 묶음 지급은 안전 관문이 항상 C로 보류하고 사람이 승인한다. 안전 관문에 닿지 않으면 503
`safety_unavailable`이고 아무것도 기록하지 않는다(fail-closed). 사람 결정 기록은 메모리(데모용)다.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from ax_agent_template.config import agent_name_scope, agent_url, is_mock
from ax_agent_template.execute import execute
from ax_contracts.schemas import Evidence, RequestMeta
from finance_agent import audit, closing, policy, reconciliation, settlement
from finance_agent import finance_config as fc
from finance_agent.handlers import closing_key, incident_impact, state
from finance_agent.human import human_from_authorization

UNAVAILABLE_EXECUTION = "exe_unavailable"  # agent-template execute()가 safety에 못 닿았을 때
REVIEW_CHECK_SECONDS = 3.0


def ok(data: Any, meta: dict[str, Any]) -> dict[str, Any]:
    return {"data": data, "meta": meta}


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


async def _body(request: Request) -> dict[str, Any]:
    try:
        raw = await request.json()
    except ValueError:
        return {}
    return raw if isinstance(raw, dict) else {}


# ---------------------------------------------------------------- 정산 묶음 상태
async def refresh_batch(batch: dict[str, Any]) -> None:
    """요청 중인 묶음의 보류 건 결정을 안전 관문에서 읽어 반영한다. 못 읽으면 그대로 둔다."""
    if batch.get("status") != "requested" or not batch.get("review_id"):
        return
    if is_mock("finance") and not os.getenv("AX_SAFETY_URL"):
        return  # 목 모드에 safety가 없으면 물어볼 곳이 없다
    url = f"{agent_url('safety')}/api/safety/reviews/{batch['review_id']}"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=REVIEW_CHECK_SECONDS)
        status = response.json()["data"]["status"] if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return
    if status == "approved":
        batch["status"] = "approved"
    elif status in ("rejected", "expired"):
        batch["status"] = "rejected"


def _batch_status(decision_status: str) -> str:
    return {"held": "requested", "verifying": "requested", "executed": "approved"}.get(
        decision_status, "rejected"
    )


async def settlements_view(period: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    books = await state().books()
    result = settlement.statements(books, period, state().held_sellers())
    batch = state().records.batches.get(result["period"])
    if batch:
        await refresh_batch(batch)
    unavailable = ["hold_reasons.open_claim"] if books.claims is None else []
    return settlement.apply_batch(result, batch), books.meta(unavailable)


# ---------------------------------------------------------------- 사람이 할 일
async def overview_data() -> tuple[dict[str, Any], dict[str, Any]]:
    books = await state().books()
    today = fc.today()
    totals = closing.compute_totals(books, fc.day_start(today), fc.day_start(today + timedelta(1)))
    todo: list[dict[str, Any]] = []

    sett, _ = await settlements_view(None)
    batch = sett["batch"]
    if batch["status"] == "requested":
        todo.append(
            {
                "kind": "settlement_batch",
                "title": f"판매자 정산 {len(batch['seller_ids'])}곳 지급 승인 대기 (안전 장치)",
                "count": len(batch["seller_ids"]),
                "amount": batch["total_payout"],
                "link": "settlements",
            }
        )
    elif batch["status"] in ("none", "rejected"):
        ready = settlement.ready(sett)
        if ready:
            todo.append(
                {
                    "kind": "settlement_batch",
                    "title": f"판매자 정산 {len(ready)}곳 지급 승인 요청 대기",
                    "count": len(ready),
                    "amount": sum(r["payout"] for r in ready),
                    "link": "settlements",
                }
            )

    yesterday = today - timedelta(days=1)
    if closing_key("day", yesterday) not in state().records.closings:
        y = closing.compute_totals(books, fc.day_start(yesterday), fc.day_start(today))
        todo.append(
            {
                "kind": "closing_confirm",
                "title": f"어제({yesterday.isoformat()}) 결산 확정 대기",
                "count": 1,
                "amount": y.net_result,
                "link": "closing",
            }
        )

    recon = reconciliation.reconcile(books, None, state().records.recon_notes, today)
    open_items = [i for i in reconciliation.exceptions(recon) if not i["note"]]
    if open_items:
        todo.append(
            {
                "kind": "recon_exception",
                "title": f"매출·PG 대사 불일치 {len(open_items)}건",
                "count": len(open_items),
                "amount": sum(reconciliation.exception_amount(i) for i in open_items),
                "link": "reconciliation",
            }
        )

    report, _ = await audit.audit_report(books, 7, impact_lookup=incident_impact)
    serious = [f for f in report["findings"] if f["severity"] in ("high", "medium")]
    if serious:
        todo.append(
            {
                "kind": "audit_finding",
                "title": f"사후 감사 의심 {len(serious)}건",
                "count": len(serious),
                "amount": None,
                "link": "audit",
            }
        )

    pol, _ = policy.policy_report(books)
    alerts = [r for r in pol["rules"] if r["alert"] in ("warn", "over")]
    if alerts:
        labels = ", ".join(r["label"] for r in alerts)
        todo.append(
            {
                "kind": "policy_alert",
                "title": f"예산·한도 경보 {len(alerts)}건 ({labels})",
                "count": len(alerts),
                "amount": None,
                "link": "policy",
            }
        )
    return {"date": today.isoformat(), "kpis": totals.as_kpis(), "todo": todo}, books.meta()


# ---------------------------------------------------------------- 라우트
def add_finance_api(app: FastAPI) -> None:
    @app.get("/api/finance/overview")
    async def overview() -> dict:
        data, meta = await overview_data()
        return ok(data, meta)

    @app.get("/api/finance/closing", response_model=None)
    async def get_closing(period: str = "day", date: str | None = None) -> dict | JSONResponse:
        if period not in ("day", "month"):
            return error(400, "invalid_request", "period는 day 또는 month")
        try:
            d = fc.parse_date(date) or fc.today()
        except ValueError as exc:
            return error(400, "invalid_request", str(exc))
        books = await state().books()
        confirmation = state().records.closings.get(closing_key(period, d))
        report = await closing.closing_report(books, period, d, confirmation)  # type: ignore[arg-type]
        return ok(report, books.meta())

    @app.post("/api/finance/closing/confirm", response_model=None)
    async def confirm_closing(
        request: Request, authorization: str | None = Header(default=None)
    ) -> dict | JSONResponse:
        try:
            person = human_from_authorization(authorization)
        except PermissionError as exc:
            return error(401, "unauthorized", str(exc))
        body = await _body(request)
        period = body.get("period", "day")
        if period not in ("day", "month"):
            return error(400, "invalid_request", "period는 day 또는 month")
        try:
            d = fc.parse_date(body.get("date")) or fc.today()
        except (ValueError, TypeError) as exc:
            return error(400, "invalid_request", str(exc))
        key = closing_key(period, d)
        records = state().records.closings
        records.setdefault(key, {"by": person, "at": fc.iso(fc.now())})
        books = await state().books()
        report = await closing.closing_report(books, period, d, records[key])
        return ok(report, books.meta())

    @app.get("/api/finance/reconciliation", response_model=None)
    async def get_reconciliation(date: str | None = None) -> dict | JSONResponse:
        try:
            d = fc.parse_date(date)
        except ValueError as exc:
            return error(400, "invalid_request", str(exc))
        books = await state().books()
        result = reconciliation.reconcile(books, d, state().records.recon_notes)
        meta = books.meta() | {"pg_source": "mock_toss_settlement"}
        return ok(result, meta)

    @app.post("/api/finance/reconciliation/{payment_id}/note", response_model=None)
    async def note_reconciliation(
        payment_id: str, request: Request, authorization: str | None = Header(default=None)
    ) -> dict | JSONResponse:
        try:
            person = human_from_authorization(authorization)
        except PermissionError as exc:
            return error(401, "unauthorized", str(exc))
        note = (await _body(request)).get("note")
        if not isinstance(note, str) or not note.strip() or len(note) > 500:
            return error(400, "invalid_request", "note는 1~500자 문자열")
        books = await state().books()
        payment = next((p for p in books.payments() if p["payment_id"] == payment_id), None)
        if payment is None:
            return error(404, "not_found", f"결제 {payment_id}이 장부에 없다")
        records = state().records
        records.recon_notes[payment_id] = note.strip()
        records.recon_note_log.append(
            {"payment_id": payment_id, "note": note.strip(), "by": person, "at": fc.iso(fc.now())}
        )
        result = reconciliation.reconcile(books, payment["created_at"].date(), records.recon_notes)
        item = next(i for i in result["items"] if i["payment_id"] == payment_id)
        return ok(item, books.meta() | {"noted_by": person})

    @app.get("/api/finance/settlements", response_model=None)
    async def get_settlements(period: str | None = None) -> dict | JSONResponse:
        try:
            data, meta = await settlements_view(period)
        except ValueError as exc:
            return error(400, "invalid_request", str(exc))
        return ok(data, meta)

    @app.post("/api/finance/settlements/batch", response_model=None)
    async def request_batch(
        request: Request, authorization: str | None = Header(default=None)
    ) -> dict | JSONResponse:
        requested_by = None
        if authorization:  # 토큰은 선택. 보냈다면 사람 토큰이어야 한다
            try:
                requested_by = human_from_authorization(authorization)
            except PermissionError as exc:
                return error(401, "unauthorized", str(exc))
        period = (await _body(request)).get("period")
        if period is not None and not isinstance(period, str):
            return error(400, "invalid_request", "period는 YYYY-Www 문자열")
        try:
            data, meta = await settlements_view(period)
        except ValueError as exc:
            return error(400, "invalid_request", str(exc))
        name = data["period"]
        batches = state().records.batches
        current = batches.get(name)
        if current and current["status"] in ("requested", "approved"):
            return ok(data, meta)  # 같은 기간은 한 번만 요청한다
        rows = settlement.ready(data)
        if not rows:
            return error(409, "nothing_to_settle", "보류 없이 지급할 판매자가 없다")

        attempt = (current or {}).get("attempt", 0) + 1
        seller_ids = [r["seller_id"] for r in rows]
        total = sum(r["payout"] for r in rows)
        summary = f"{name} 판매자 정산 {len(rows)}곳 {fc.won(total)} 지급 승인 요청"
        params = {
            "period": name,
            "from": data["from"],
            "to": data["to"],
            "pay_date": data["pay_date"],
            "seller_ids": seller_ids,
            "total_payout": total,
            "statements": [
                {
                    k: r[k]
                    for k in (
                        "seller_id",
                        "name",
                        "orders",
                        "gross",
                        "commission",
                        "seller_coupon",
                        "refunds",
                        "payout",
                    )
                }
                for r in rows
            ],
            "summary": summary,
        }
        context_id = f"ctx_req_settlement_{name}"
        meta_req = RequestMeta(
            context_id=context_id,
            token="finance-dashboard",
            deadline=fc.now() + timedelta(seconds=40),
            idempotency_key=f"{context_id}:confirm_settlement:{name}:{attempt}",
        )
        evidence = Evidence(
            queries=["finance:settlements", "shop-api:/api/shop/orders"],
            data_summary=summary,
            collected_at=fc.now(),
            ref_ids=seller_ids,
        )
        with agent_name_scope("finance"):
            decision = await execute("confirm_settlement", params, meta_req, evidence)
        if decision.execution_id == UNAVAILABLE_EXECUTION or decision.status == "failed":
            return error(
                503,
                "safety_unavailable",
                "안전 장치에 닿지 않아 정산 묶음을 요청하지 않았다 (지급 기록 없음)",
            )
        batches[name] = {
            "status": _batch_status(decision.status),
            "review_id": decision.review_id,
            "execution_id": decision.execution_id,
            "total_payout": total,
            "seller_ids": seller_ids,
            "attempt": attempt,
            "requested_by": requested_by,
            "requested_at": fc.iso(fc.now()),
        }
        data, meta = await settlements_view(name)
        return ok(data, meta)

    @app.get("/api/finance/policy")
    async def get_policy() -> dict:
        books = await state().books()
        data, missing = policy.policy_report(books)
        return ok(data, books.meta(missing))

    @app.get("/api/finance/audit", response_model=None)
    async def get_audit(days: str = "7", context_id: str | None = None) -> dict | JSONResponse:
        try:
            n = int(days)
        except ValueError:
            n = 0
        if not 1 <= n <= 90:
            return error(400, "invalid_request", "days는 1~90")
        books = await state().books()
        data, missing = await audit.audit_report(
            books, n, impact_lookup=incident_impact, context_id=context_id or None
        )
        return ok(data, books.meta(missing))
