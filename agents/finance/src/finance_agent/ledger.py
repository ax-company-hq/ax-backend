"""사건별 보상 쿠폰 장부 (재설계 문서 12장).

재무는 쿠폰을 주지 않는다. 누구에게 얼마를 줄지는 고객지원이 정하고(`request_compensation`),
지급은 마케팅(`issue_coupon`)이 안전 관문을 거쳐 한다. 재무는 나간 것을 사건별로 적는다.

장부 출처(`ledger_source`)는 셋이다.

- `events`: 이벤트 버스의 `coupon.issued`(보상). 목 모드는 템플릿 목 버스로 받는다. 실제
  모드는 `REDIS_URL`이 있어야 받고, 없으면 구독이 꺼진 채로 돈다.
- `safety_executions`: 안전 관문 실행 기록 `GET /api/safety/executions`(읽기 API, 규칙 4)의
  `issue_coupon`·`purpose_code=compensation`. 실행됨·보류·거절이 모두 남는다.
- `seed`: 목 모드의 실행 기록 시드(`finance_books_seed.json` `executions`).

중복 계산 방지: 모든 쿠폰은 안전 관문을 지나므로 한 사건에 실행 기록이 있으면 실행 기록이
원본이고, 이벤트 줄은 `execution_id`가 맞을 때 `coupon_id`만 채운다(더하지 않는다).
실행 기록 안에서는 `execution_id`, 그다음 `idempotency_key`로 한 줄만 남긴다.
실행 기록이 없는 사건만 이벤트 줄을 쓴다.
두 출처를 모두 못 쓰면 None(확인할 수 없음)이고 0으로 바꾸지 않는다(fail-closed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from ax_agent_template.config import agent_url, is_mock
from finance_agent import books as bk
from finance_agent import finance_config as fc

LEDGER_EVENTS = "events"
LEDGER_SAFETY = "safety_executions"
LEDGER_SEED = "seed"
COUPON_STATUSES = ("executed", "held", "rejected", "failed")

# 이벤트 구독이 실제로 붙었는지 (`main.subscribe_events`가 바꾼다)
EVENTS = {"connected": False}


def events_connected() -> bool:
    return EVENTS["connected"]


@dataclass
class ExecutionFeed:
    """안전 관문 실행 기록. `rows`가 None이면 못 받음."""

    rows: list[dict[str, Any]] | None
    source: str  # LEDGER_SAFETY | LEDGER_SEED


def _pos(value: Any) -> int | None:
    ok = isinstance(value, int) and not isinstance(value, bool) and value > 0
    return value if ok else None


# ---------------------------------------------------------------- 원천 읽기
async def load_executions(
    context_id: str | None = None, http_client: httpx.AsyncClient | None = None
) -> ExecutionFeed:
    """목 모드는 시드, 실제 모드는 safety `GET /api/safety/executions`(주소는 `AX_SAFETY_URL`)."""
    if is_mock("finance"):
        return ExecutionFeed(bk.seed_books().executions, LEDGER_SEED)
    url = f"{agent_url('safety')}/api/safety/executions"
    if context_id:
        url += f"?context_id={context_id}"
    client = http_client or httpx.AsyncClient()
    try:
        raw = await bk._get(client, url)
    finally:
        if http_client is None:
            await client.aclose()
    if not isinstance(raw, list):
        return ExecutionFeed(None, LEDGER_SAFETY)
    rows = [bk.normalize_execution(r) for r in raw if isinstance(r, dict)]
    return ExecutionFeed([r for r in rows if r is not None], LEDGER_SAFETY)


async def load_thresholds(http_client: httpx.AsyncClient | None = None) -> dict[str, Any] | None:
    """기준표. 목 모드는 시드에 옮겨 둔 기본값, 실제 모드는 safety `GET /api/safety/thresholds`."""
    if is_mock("finance"):
        return bk.read_seed().get("thresholds")
    client = http_client or httpx.AsyncClient()
    try:
        raw = await bk._get(client, f"{agent_url('safety')}/api/safety/thresholds")
    finally:
        if http_client is None:
            await client.aclose()
    return raw if isinstance(raw, dict) else None


# ---------------------------------------------------------------- 줄 만들기
def coupon_from_execution(e: dict[str, Any]) -> dict[str, Any] | None:
    """실행 기록 한 줄 → 보상 쿠폰 장부 줄. 보상 쿠폰이 아니면 None."""
    params = e.get("params") or {}
    if e.get("action") != "issue_coupon" or params.get("purpose_code") != "compensation":
        return None
    if e.get("status") not in COUPON_STATUSES or not e.get("context_id"):
        return None
    count, unit = _pos(params.get("count")), _pos(params.get("unit_value"))
    computed = _pos(e.get("computed_amount"))
    amount = computed or (count * unit if count and unit else _pos(params.get("total_value")))
    ref = e.get("result_ref")
    return {
        "context_id": e["context_id"],
        "buyer_id": params.get("buyer_id"),
        "execution_id": e.get("execution_id"),
        "coupon_id": ref if isinstance(ref, str) and ref.startswith("cpn_") else None,
        "count": count,
        "unit_value": unit,
        "amount": amount,
        "purpose_code": "compensation",
        "reason_code": params.get("reason_code"),
        "grade": e.get("grade"),
        "status": e.get("status"),
        "review_id": e.get("review_id"),
        "at": fc.iso(e.get("created_at")),
        "idempotency_key": e.get("idempotency_key"),
    }


def coupon_from_event(context_id: str, envelope_data: dict[str, Any], occurred_at: Any) -> dict:
    """`coupon.issued`(보상) → 장부 줄. 이벤트에는 구매자·단가가 없을 수 있다."""
    count, total = _pos(envelope_data.get("count")), _pos(envelope_data.get("total_value"))
    unit = envelope_data.get("unit_value")
    if not _pos(unit):
        unit = total // count if count and total and total % count == 0 else None
    return {
        "context_id": context_id,
        "buyer_id": envelope_data.get("buyer_id"),
        "execution_id": envelope_data.get("execution_id"),
        "coupon_id": envelope_data.get("coupon_id"),
        "count": count,
        "unit_value": unit,
        "amount": total,
        "purpose_code": "compensation",
        "reason_code": envelope_data.get("reason_code"),
        "grade": None,
        "status": "executed",
        "review_id": None,
        "at": fc.iso(occurred_at) if hasattr(occurred_at, "astimezone") else None,
        "idempotency_key": None,
    }


def execution_coupons(rows: list[dict[str, Any]], context_id: str) -> list[dict[str, Any]]:
    """한 사건의 실행 기록 보상 쿠폰. `execution_id`·`idempotency_key`가 같으면 한 줄만."""
    out: dict[str, dict[str, Any]] = {}
    seen_keys: dict[str, str] = {}
    for e in rows:
        if e.get("context_id") != context_id:
            continue
        row = coupon_from_execution(e)
        if row is None:
            continue
        exe = str(row["execution_id"] or f"_{len(out)}")
        key = row["idempotency_key"]
        if key and key in seen_keys:
            out.pop(seen_keys[key], None)  # 같은 요청의 다시 보내기는 마지막 기록만
        out[exe] = row
        if key:
            seen_keys[key] = exe
    return sorted(out.values(), key=lambda r: (r["at"] or "", str(r["execution_id"])))


def merge(
    context_id: str,
    feed: ExecutionFeed,
    event_rows: list[dict[str, Any]],
    events_available: bool,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """(장부 줄, ledger_source). 둘 다 못 쓰면 (None, None)."""
    exe = execution_coupons(feed.rows, context_id) if feed.rows is not None else []
    if exe:
        by_exe = {r["execution_id"]: r for r in event_rows if r.get("execution_id")}
        for row in exe:
            match = by_exe.get(row["execution_id"])
            if row["coupon_id"] is None and match is not None:
                row["coupon_id"] = match.get("coupon_id")
        return exe, feed.source
    if event_rows:
        return [dict(r) for r in event_rows], LEDGER_EVENTS
    if feed.rows is not None:
        return [], feed.source
    if events_available:
        return [], LEDGER_EVENTS
    return None, None


def totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    """실행됨·보류 합계. 장 수는 줄의 `count` 합(없으면 1장)."""

    def pick(status: str) -> list[dict[str, Any]]:
        return [r for r in rows if r["status"] == status]

    executed, held = pick("executed"), pick("held")
    return {
        "total_executed": sum(r["amount"] or 0 for r in executed),
        "total_held": sum(r["amount"] or 0 for r in held),
        "count_executed": sum(r["count"] or 1 for r in executed),
        "count_held": sum(r["count"] or 1 for r in held),
    }


def public(row: dict[str, Any]) -> dict[str, Any]:
    """API에 내보내는 쿠폰 줄 (재설계 문서 12-1)."""
    return {
        k: row.get(k)
        for k in (
            "buyer_id",
            "execution_id",
            "coupon_id",
            "count",
            "unit_value",
            "amount",
            "purpose_code",
            "reason_code",
            "grade",
            "status",
            "review_id",
            "at",
        )
    }
