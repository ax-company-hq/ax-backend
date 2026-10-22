"""캣북스(쇼핑몰) 연결: 돈이 나갈 클레임을 읽어서 재무 AI가 확인한다.

캣북스 백엔드(`FINANCE_CATBOOKS_URL`, 기본 `http://host.docker.internal:8030`)에 **GET만** 보낸다.
캣북스의 상태는 바꾸지 않는다. 실제 지급은 캣북스의 지급 버튼(`POST /api/admin/queue/process`)이
하고, 재무 AI는 읽고 판단만 한다.

읽는 것
- `GET /api/admin/claims`: 클레임 전체 (상태, 예상 환불 `expected_refund`, 돌려줄 적립금
  `points_back`, 보상 적립금 `compensation`, 지급된 환불 `refund`, 계산 근거 `lines`·`deductions`,
  이상 신청 점수 `abuse.score`)
- `GET /api/admin/claims/{id}`: 주문 영수증 `receipt` (현금 결제 `paid_amount`=상품+배송비-적립금,
  배송비 `shipping_fee`, 줄별 결제 `items[].paid_amount`)
- `GET /api/admin/queue`: 반품이 도착해 지급을 기다리는 줄 (`position`, `ready`, `blockers`)

확인하는 것 (숫자는 모두 코드가 계산한다)
1. 환불 금액이 규정 계산과 맞는지: 줄별 환불 = 줄 결제 x 수량 비율(반올림 1원 허용),
   합계 = 줄 환불 + 배송비(0 또는 영수증 배송비) - 차감, 교환은 현금 환불 0, 보상 적립금은 규정 금액
2. 환불이 결제 금액 - 이미 환불한 금액을 넘지 않는지 (`recompute`, `verify_amount`와 같은 코드)
3. 안전 장치 기준(환불 10만 원 이상)을 넘으면 재무 AI 재량 한도(`ai_review_policy.toml`) 안에서만
   승인한다 (`review.decide`, `review_hold`와 같은 코드). 한도를 넘거나 자료가 없으면 사람 확인.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ax_contracts.schemas import AgentRequest, RequestMeta
from finance_agent.handlers import recompute, state
from finance_agent.review import decide
from finance_agent.schemas import ReviewHoldInput, VerifyAmountInput
from finance_agent.sources import Impact

logger = logging.getLogger("ax.finance.catbooks")

DEFAULT_URL = "http://host.docker.internal:8030"
DEFAULT_POLL_SECONDS = 45
CALL_SECONDS = 5.0

# 안전 장치 기준 (safety `thresholds.toml` refund_single: 환불 10만 원 이상은 사람 확인)
SAFETY_REFUND_THRESHOLD = 100_000
# 캣북스 규정의 보상 적립금 (캣북스 `rules/policy.py`)
# 불량·파손·오배송 2,000원, 배송 지연 1,000/3,000원
SELLER_FAULT = {"defect", "wrong_item", "damaged"}
DEFECT_COMPENSATION = 2_000
LATE_COMPENSATION = {1_000, 3_000}
ABUSE_REVIEW_SCORE = 60  # 캣북스가 이상 신청으로 보는 점수

# 돈이 나가기 전 단계의 클레임만 본다 (완료·거절·철회·자료 대기는 제외)
OPEN_STATUSES = {"needs_review", "awaiting_return", "return_received"}

TYPE_KO = {"cancel": "주문 취소", "return": "반품", "exchange": "교환", "compensation": "보상"}
STATUS_KO = {
    "needs_review": "쇼핑몰 상담원 확인 중",
    "awaiting_return": "반품 도착 기다림",
    "return_received": "반품 도착, 지급 대기",
}
RESULT_KO = {"pass": "통과", "blocked": "막음", "needs_human": "사람 확인 필요"}

_lock = asyncio.Lock()


def catbooks_url() -> str:
    return os.getenv("FINANCE_CATBOOKS_URL", DEFAULT_URL).rstrip("/")


def poll_seconds() -> int:
    """자동 가져오기 간격(초). 0이면 끈다."""
    try:
        return max(0, int(os.getenv("FINANCE_CATBOOKS_POLL_SECONDS", str(DEFAULT_POLL_SECONDS))))
    except ValueError:
        return DEFAULT_POLL_SECONDS


def _won(n: int) -> str:
    return f"{n:,}원"


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class CatbooksError(Exception):
    pass


class CatbooksClient:
    """캣북스 관리자 API 읽기 전용 클라이언트."""

    def __init__(self, base_url: str | None = None, http: httpx.AsyncClient | None = None) -> None:
        self.base_url = (base_url or catbooks_url()).rstrip("/")
        self._http = http

    async def _get(self, client: httpx.AsyncClient, path: str) -> Any:
        try:
            response = await client.get(f"{self.base_url}{path}", timeout=CALL_SECONDS)
        except httpx.HTTPError as exc:
            raise CatbooksError(f"캣북스에 닿지 않음 ({type(exc).__name__})") from exc
        if response.status_code != 200:
            raise CatbooksError(f"캣북스 {path} 응답 {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise CatbooksError(f"캣북스 {path} 응답 형식 오류") from exc

    async def fetch(self) -> tuple[list[dict], list[dict], dict[int, dict]]:
        """(클레임 전체, 지급 대기열, 열린 클레임의 영수증) 를 읽는다."""
        client = self._http or httpx.AsyncClient()
        try:
            claims = await self._get(client, "/api/admin/claims")
            queue = await self._get(client, "/api/admin/queue")
            if not isinstance(claims, list) or not isinstance(queue, dict):
                raise CatbooksError("캣북스 응답 형식 오류")
            receipts: dict[int, dict] = {}
            for c in claims:
                if isinstance(c, dict) and c.get("status") in OPEN_STATUSES:
                    try:
                        detail = await self._get(client, f"/api/admin/claims/{c['id']}")
                    except CatbooksError:
                        continue  # 영수증이 없으면 그 건만 "사람 확인"
                    if isinstance(detail, dict) and isinstance(detail.get("receipt"), dict):
                        receipts[c["id"]] = detail["receipt"]
            entries = queue.get("queue")
            return claims, entries if isinstance(entries, list) else [], receipts
        finally:
            if self._http is None:
                await client.aclose()


class _ReceiptSource:
    """`recompute`·`decide`에 캣북스 영수증의 현금 결제 금액을 넘겨주는 원천."""

    def __init__(self, paid: int | None) -> None:
        self.paid = paid

    async def impact(
        self, meta: RequestMeta, context_id: str, window: dict[str, Any] | None
    ) -> Impact | None:
        return None

    async def paid_amount(self, meta: RequestMeta, order_id: str) -> int | None:
        return self.paid

    async def fraud_score(self, meta: RequestMeta, seller_id: str) -> int | None:
        return None


def _meta(claim_id: int) -> RequestMeta:
    return RequestMeta(
        context_id=f"catbooks_claim_{claim_id}",
        token="catbooks-sync",
        deadline=datetime.now(UTC) + timedelta(seconds=10),
    )


def rule_problems(claim: dict, receipt: dict) -> list[str]:
    """캣북스 규정 계산을 다시 해 본다. 문제가 없으면 빈 목록."""
    problems: list[str] = []
    ctype = claim.get("type")
    cash = _int(claim.get("expected_refund")) or 0
    points = _int(claim.get("points_back")) or 0
    comp = _int(claim.get("compensation")) or 0
    items = {i.get("id"): i for i in receipt.get("items", []) if isinstance(i, dict)}

    line_cash = 0
    for ln in claim.get("lines") or []:
        item = items.get(ln.get("order_item_id"))
        qty, got = _int(ln.get("qty")), _int(ln.get("refund_cash"))
        if item is None or not qty or got is None:
            problems.append(f"'{ln.get('title', '?')}' 줄이 영수증에 없음")
            continue
        line_cash += got
        if ctype == "exchange":
            continue
        paid, total_qty = _int(item.get("paid_amount")), _int(item.get("qty"))
        if paid is None or not total_qty:
            problems.append(f"'{ln.get('title', '?')}' 결제 금액을 알 수 없음")
            continue
        expected = paid * qty / total_qty
        if abs(got - expected) > 1:
            problems.append(
                f"'{ln.get('title', '?')}' {qty}개 환불 {_won(got)}이 "
                f"결제 기준 {_won(round(expected))}과 다름"
            )

    deductions = sum(_int(d.get("amount")) or 0 for d in claim.get("deductions") or [])
    if ctype == "exchange":
        if cash or points:
            problems.append(f"교환인데 현금 환불 {_won(cash)}이 잡혀 있음")
    elif ctype in ("cancel", "return"):
        shipping = _int(receipt.get("shipping_fee")) or 0
        base = max(0, line_cash - deductions)
        allowed = {base, max(0, line_cash + shipping - deductions)}
        if cash not in allowed:
            problems.append(
                f"환불 {_won(cash)}이 줄 환불 {_won(line_cash)} - 차감 {_won(deductions)} "
                f"(+배송비 {_won(shipping)}) 계산과 다름"
            )
        payable = _int(claim.get("payable"))
        if payable is not None and payable != cash + points:
            problems.append(f"지급 합계 {_won(payable)}이 현금+적립금 {_won(cash + points)}과 다름")

    reason = claim.get("reason")
    if comp:
        defect = ctype in ("return", "exchange") and reason in SELLER_FAULT
        late = ctype == "compensation" and reason == "late_delivery"
        ok = (defect and comp == DEFECT_COMPENSATION) or (late and comp in LATE_COMPENSATION)
        if not ok:
            problems.append(f"보상 적립금 {_won(comp)}이 규정 금액과 다름")
    return problems


async def check_claim(
    claim: dict, receipt: dict | None, refunded_before: int, queue: dict | None
) -> dict[str, Any]:
    """클레임 한 건을 재무 규칙으로 확인한다.

    결과는 통과(pass)·막음(blocked)·사람 확인(needs_human).
    """
    claim_id = int(claim["id"])
    cash = _int(claim.get("expected_refund")) or 0
    points = _int(claim.get("points_back")) or 0
    comp = _int(claim.get("compensation")) or 0
    paid = _int(receipt.get("paid_amount")) if receipt else None
    policy = state().policy
    item: dict[str, Any] = {
        "id": f"catbooks:claim:{claim_id}",
        "claim_id": claim_id,
        "order_id": claim.get("order_id"),
        "order_no": claim.get("order_no"),
        "customer": claim.get("customer"),
        "type": claim.get("type"),
        "type_ko": TYPE_KO.get(str(claim.get("type")), str(claim.get("type"))),
        "reason": claim.get("reason"),
        "catbooks_status": claim.get("status"),
        "catbooks_status_ko": STATUS_KO.get(str(claim.get("status")), str(claim.get("status"))),
        "refund_amount": cash,
        "points_back": points,
        "compensation": comp,
        "paid_amount": paid,
        "refunded_before": refunded_before,
        "queue_position": queue.get("position") if queue else None,
        "queue_ready": bool(queue and queue.get("ready")),
        "needs_safety": False,
        "created_at": claim.get("created_at"),
        "checked_at": datetime.now(UTC).isoformat(),
    }

    def done(result: str, code: str, explanation: str) -> dict[str, Any]:
        return item | {
            "result": result,
            "result_ko": RESULT_KO[result],
            "reason_code": code,
            "explanation": explanation,
        }

    order = f"주문 {claim.get('order_no') or claim.get('order_id')}"
    if receipt is None or paid is None:
        return done(
            "needs_human",
            "no_receipt",
            f"{order}의 영수증을 받지 못해 결제 금액을 확인할 수 없습니다.",
        )

    problems = rule_problems(claim, receipt)
    if problems:
        text = "규정 계산과 다릅니다: " + "; ".join(problems) + "."
        return done("blocked", "amount_mismatch", text)

    if claim.get("type") == "compensation" and not comp:
        return done(
            "needs_human",
            "amount_undecided",
            "보상 금액이 정해지지 않은 신청이라 재무 AI가 판단할 수 없습니다.",
        )

    abuse = (claim.get("abuse") or {}).get("score") or 0
    if abuse >= ABUSE_REVIEW_SCORE and not claim.get("manager_approved"):
        return done(
            "needs_human",
            "abuse_suspected",
            f"쇼핑몰이 이상 신청 의심(점수 {abuse}점)으로 표시한 건이라 사람이 확인해 주세요. "
            f"금액 계산은 맞습니다 (환불 {_won(cash)}).",
        )

    extra = f" 보상 적립금 {_won(comp)}은 규정 금액입니다." if comp else ""
    if not cash:
        return done(
            "pass",
            "no_cash_refund",
            f"현금 환불이 없는 {item['type_ko']}입니다.{extra}".strip(),
        )

    key = f"catbooks_order_{claim.get('order_id')}"
    params = {"order_id": key, "refund_amount": cash}
    source = _ReceiptSource(paid)
    meta = _meta(claim_id)
    if cash < SAFETY_REFUND_THRESHOLD:
        out = await recompute(
            AgentRequest(meta=meta, skill="verify_amount"),
            VerifyAmountInput(
                execution_id=f"catbooks_claim_{claim_id}",
                action="refund_request",
                requester="catbooks",
                params=params,
                computed_amount=cash,
                threshold_version=0,
            ),
            source,  # type: ignore[arg-type]
            {key: refunded_before},
        )
        if out.decision == "approved":
            return done(
                "pass",
                "recomputed_match",
                f"{order} 결제 {_won(paid)} 중 이미 {_won(refunded_before)} 환불, "
                f"이번 환불 {_won(cash)}은 남은 금액 안입니다.{extra}",
            )
        return done(
            "blocked",
            out.reason_code,
            f"{order} 결제 {_won(paid)} 중 이미 {_won(refunded_before)}을 환불해서 "
            f"{_won(cash)}을 더 환불할 수 없습니다.",
        )

    review = await decide(
        ReviewHoldInput(
            review_id=f"catbooks_claim_{claim_id}",
            execution_id=f"catbooks_claim_{claim_id}",
            action="refund_request",
            requester="catbooks",
            params=params,
            computed_amount=cash,
            hold_reason="over_threshold",
        ),
        meta,
        source,  # type: ignore[arg-type]
        refunded_before,
        policy,
    )
    text = review.explanation.replace(key, order)
    if review.decision == "approve":
        item["needs_safety"] = True
        return done(
            "pass",
            "within_ai_limit",
            f"안전 장치 기준({_won(SAFETY_REFUND_THRESHOLD)}) 이상이지만 재무 AI 재량 한도 안: "
            f"{text}{extra} 실제 지급은 안전 장치를 거쳐야 합니다.",
        )
    if review.decision == "reject":
        return done("blocked", review.reason_code, text)
    return done("needs_human", review.reason_code, text)


def _refunded_by_order(claims: list[dict]) -> dict[Any, int]:
    """주문별로 이미 지급된 현금 환불 합계 (완료된 클레임의 `refund`)."""
    out: dict[Any, int] = {}
    for c in claims:
        if c.get("status") == "completed":
            out[c.get("order_id")] = out.get(c.get("order_id"), 0) + (_int(c.get("refund")) or 0)
    return out


async def sync(client: CatbooksClient | None = None) -> dict[str, Any]:
    """캣북스에서 지금 가져와서 확인한다. 실패하면 이전 목록을 그대로 두고 오류를 알려 준다."""
    client = client or CatbooksClient()
    async with _lock:
        ledger = state().ledger
        now = datetime.now(UTC).isoformat()
        try:
            claims, queue, receipts = await client.fetch()
        except CatbooksError as exc:
            logger.warning("캣북스 가져오기 실패: %s", exc)
            ledger.catbooks_sync = (ledger.catbooks_sync or {}) | {
                "ok": False,
                "error": str(exc),
                "tried_at": now,
                "source": client.base_url,
            }
            return ledger.catbooks_sync
        refunded = _refunded_by_order(claims)
        by_claim = {e.get("claim_id"): e for e in queue if isinstance(e, dict)}
        items: dict[str, dict[str, Any]] = {}
        for c in claims:
            if not isinstance(c, dict) or c.get("status") not in OPEN_STATUSES:
                continue
            row = await check_claim(
                c, receipts.get(c["id"]), refunded.get(c.get("order_id"), 0), by_claim.get(c["id"])
            )
            items[row["id"]] = row
        ledger.catbooks_items = items
        counts = {k: sum(1 for i in items.values() if i["result"] == k) for k in RESULT_KO}
        ledger.catbooks_sync = {
            "ok": True,
            "error": None,
            "synced_at": now,
            "tried_at": now,
            "source": client.base_url,
            "total": len(items),
            "counts": counts,
            "claims_read": len(claims),
            "queue_read": len(queue),
        }
        return ledger.catbooks_sync


def items_newest_first() -> list[dict[str, Any]]:
    rows = list(state().ledger.catbooks_items.values())
    return sorted(rows, key=lambda r: (str(r.get("created_at") or ""), r["claim_id"]), reverse=True)


async def poll_forever(interval: int) -> None:
    """앱이 도는 동안 `interval`초마다 가져온다."""
    while True:
        try:
            await sync()
        except Exception:  # 한 번 실패해도 다음 주기에 다시 시도한다
            logger.exception("캣북스 자동 가져오기 오류")
        await asyncio.sleep(interval)
