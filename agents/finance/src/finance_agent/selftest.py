"""재무 AI 자체 점검 (대시보드 "재무 AI 테스트" 버튼).

담당자가 아침마다 눌러서 재무 AI의 계산 규칙이 맞는지, 안전 장치·원천 데이터와 연결돼 있는지 본다.

- 규칙 점검은 **테스트 전용 가짜 자료**로 돌린다. 실제 기록(결산 확정, 대사 메모, 정산 묶음,
  환불 누적)에는 아무것도 남기지 않는다. 숫자는 실제 리포트와 같은 코드가 계산한다.
  점검 대상: 금액 다시 계산(`recheck`, 사후 감사가 씀), ① 결산 공식, ② 대사 상태 분류,
  ③ 정산 공식, ⑤ 예산 경보, ⑦ 감사 규칙(시드 장부에서 6가지 의심 코드가 모두 잡히는지).
- 연결 점검은 실제 주소(`agent_url`)에 `GET`만 보낸다. 상태를 바꾸지 않는다.
- 결과 수준: `fail`(계산이 틀리거나 안전 장치가 끊김 → 바로 확인 필요), `warn`(원천 데이터에 닿지
  않음 → 리포트 일부가 "확인할 수 없음"이 될 수 있음).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx

from ax_agent_template.config import agent_url
from ax_contracts.schemas import RequestMeta, VerifyAmountInput
from finance_agent import finance_config as fc
from finance_agent.audit import audit_report
from finance_agent.books import Books, seed_books
from finance_agent.closing import compute_totals
from finance_agent.policy import _alert
from finance_agent.reconciliation import classify
from finance_agent.settlement import statements
from finance_agent.sources import MockSources

CTX = "ctx_selftest"
CALL_SECONDS = 3.0
SELF_TEST_TTL_HOURS = 12  # 마지막 결과를 보여 주는 시간. 지나면 기록을 지우고 다시 테스트하게 한다
AUDIT_CODES = {
    "split_under_limit",
    "repeat_refund",
    "duplicate_coupon",
    "refund_rate_jump",
    "amount_recheck_failed",
    "gate_hold_trend",
}


def is_fresh(result: dict[str, Any], now: datetime | None = None) -> bool:
    """`ran_at`이 12시간 안이면 True. 형식이 이상하면 False(오래된 것으로 본다)."""
    try:
        ran_at = datetime.fromisoformat(str(result["ran_at"]))
    except (KeyError, ValueError):
        return False
    return (now or datetime.now(UTC)) - ran_at < timedelta(hours=SELF_TEST_TTL_HOURS)


# 테스트 전용 가짜 자료: 장애 피해 고객 2명(피해 100,000원), 결제 30,000원 주문 하나
SELFTEST_SOURCES = MockSources(
    {
        "impacts": {
            CTX: {
                "failed_order_count": 2,
                "affected_customer_count": 2,
                "damage_amount": 100000,
                "affected_customer_ids": ["test_buyer_1", "test_buyer_2"],
            }
        },
        "payments": {"test_order_1": 30000},
        "fraud_scores": {},
    }
)
EMPTY_SOURCES = MockSources({"impacts": {}, "payments": {}, "fraud_scores": {}})

Level = Literal["fail", "warn"]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    level: Level = "fail"

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "level": self.level}


def _meta() -> RequestMeta:
    return RequestMeta(
        context_id=CTX,
        token="selftest",
        deadline=datetime.now(UTC) + timedelta(seconds=10),
    )


def _verify_input(action: str, params: dict[str, Any], computed: int | None) -> VerifyAmountInput:
    return VerifyAmountInput(
        execution_id="exe_selftest",
        action=action,
        requester="selftest",
        params=params,
        computed_amount=computed,
        threshold_version=1,
    )


def _coupon(buyer: str, count: int, unit: int, total: int | None = None) -> dict[str, Any]:
    return {
        "purpose_code": "compensation",
        "buyer_id": buyer,
        "count": count,
        "unit_value": unit,
        "total_value": count * unit if total is None else total,
    }


def _tiny_books() -> Books:
    """규칙 점검용 작은 장부: 지난주 월요일 도서 20,000원 주문(배송 완료), 같은 날 환불 5,000원."""
    monday = fc.today() - timedelta(days=fc.today().weekday() + 7)
    at = fc.day_start(monday) + timedelta(hours=10)
    order = {
        "order_id": "ord_selftest",
        "buyer_id": "test_buyer_1",
        "status": "paid",
        "total_amount": 20000,
        "refunded_amount": 5000,
        "shipping_fee": 0,
        "points_used": 0,
        "created_at": at,
        "delivered_at": at + timedelta(days=1),
        "items": [
            {
                "product_id": "prd_t",
                "seller_id": "sel_t",
                "category_code": "book",
                "qty": 2,
                "unit_price": 10000,
                "points_share": 0,
                "paid_amount": 20000,
            }
        ],
        "payments": [
            {"payment_id": "pay_selftest", "amount": 20000, "status": "succeeded", "created_at": at}
        ],
    }
    refund = {
        "refund_id": "rfd_selftest",
        "order_id": "ord_selftest",
        "buyer_id": "test_buyer_1",
        "seller_id": "sel_t",
        "amount": 5000,
        "created_at": at + timedelta(hours=2),
    }
    return Books(
        orders=[order],
        sellers=[{"seller_id": "sel_t", "name": "점검 판매자"}],
        refunds=[refund],
        claims=[],
        executions=[],
        mock=True,
        source="seed",
    )


async def _guard(name: str, run: Any, checks: list[Check]) -> None:
    try:
        ok, detail = await run()
        checks.append(Check(name, ok, detail))
    except Exception as exc:  # 점검 자체가 깨져도 결과로 보여 준다
        checks.append(Check(name, False, f"점검 중 오류: {type(exc).__name__}"))


async def _recompute_checks(recompute: Any) -> list[Check]:
    """금액 다시 계산 규칙. 사후 감사(`amount_recheck_failed`)가 이 코드를 쓴다."""
    meta = _meta()
    cases = [
        (
            "쿠폰 금액 다시 계산",
            "issue_coupon",
            _coupon("test_buyer_1", 2, 5000),
            10000,
            SELFTEST_SOURCES,
            ("approved", "recomputed_match"),
            "2장 × 5,000원 = 10,000원을 맞다고 봄",
        ),
        (
            "금액을 틀리게 적은 쿠폰 막기",
            "issue_coupon",
            _coupon("test_buyer_1", 3, 5000, 5000),
            15000,
            SELFTEST_SOURCES,
            ("rejected", "amount_mismatch"),
            "15,000원인데 5,000원이라 적은 기록을 잡아냄",
        ),
        (
            "피해 고객이 아닌 사람 막기",
            "issue_coupon",
            _coupon("someone_else", 1, 5000),
            5000,
            SELFTEST_SOURCES,
            ("rejected", "not_affected"),
            "피해 고객 목록에 없는 사람의 보상을 잡아냄",
        ),
        (
            "결제보다 큰 환불 막기",
            "refund_request",
            {"order_id": "test_order_1", "refund_amount": 40000},
            40000,
            SELFTEST_SOURCES,
            ("rejected", "exceeds_paid"),
            "결제 30,000원 주문에 40,000원 환불을 잡아냄",
        ),
        (
            "자료가 없으면 통과시키지 않기",
            "issue_coupon",
            _coupon("test_buyer_1", 1, 5000),
            5000,
            EMPTY_SOURCES,
            ("rejected", "source_unavailable"),
            "피해 자료가 없을 때 맞다고 보지 않음",
        ),
    ]
    checks: list[Check] = []
    for name, action, params, computed, sources, expected, good in cases:
        try:
            out = await recompute(meta, _verify_input(action, params, computed), sources, {})
            got = (out.decision, out.reason_code)
            checks.append(
                Check(
                    name,
                    got == expected,
                    good if got == expected else f"예상 {expected}, 실제 {got}",
                )
            )
        except Exception as exc:
            checks.append(Check(name, False, f"점검 중 오류: {type(exc).__name__}"))
    return checks


async def _module_checks() -> list[Check]:
    checks: list[Check] = []
    books = _tiny_books()

    async def closing_formula() -> tuple[bool, str]:
        order_day = books.orders[0]["created_at"].date()
        t = compute_totals(books, fc.day_start(order_day), fc.day_start(order_day + timedelta(1)))
        expected = (20000, 2000, 620, 5000, 1380)
        got = (t.gmv, t.commission_revenue, t.pg_fee, t.refund_total, t.net_result)
        return got == expected, (
            "총거래액 20,000원, 수수료 2,000원, PG 수수료 620원, 영업 결과 1,380원"
            if got == expected
            else f"예상 {expected}, 실제 {got}"
        )

    async def recon_classes() -> tuple[bool, str]:
        past, future = fc.today() - timedelta(days=1), fc.today() + timedelta(days=1)

        def rec(amount: int, fee: int, settled: bool = True) -> dict[str, Any]:
            return {"amount": amount, "fee": fee, "settled": settled, "paid_out_date": "x"}

        got = [
            classify(30000, rec(30000, 930), past, fc.today())[0],
            classify(30000, rec(28000, 868), past, fc.today())[0],
            classify(30000, None, past, fc.today())[0],
            classify(30000, None, future, fc.today())[0],
            classify(30000, rec(30000, 1050), past, fc.today())[0],
        ]
        expected = ["matched", "mismatch", "missing_in_pg", "pending", "fee_anomaly"]
        return got == expected, (
            "일치·금액 다름·PG에 없음·정산 전·수수료 이상을 나눔"
            if got == expected
            else f"예상 {expected}, 실제 {got}"
        )

    async def settlement_formula() -> tuple[bool, str]:
        row = statements(books, None)["statements"][0]
        ok = (row["gross"], row["commission"], row["refunds"], row["payout"], row["status"]) == (
            20000,
            2000,
            5000,
            13000,
            "ready",
        )
        return ok, (
            "판매액 20,000원 − 수수료 2,000원 − 환불 5,000원 = 13,000원"
            if ok
            else f"실제 {row['gross']}/{row['commission']}/{row['refunds']}/{row['payout']}"
        )

    async def budget_alerts() -> tuple[bool, str]:
        got = (_alert(0.2), _alert(0.85), _alert(1.0), _alert(0.2, True))
        expected = ("ok", "warn", "over", "warn")
        return got == expected, (
            "소진율 80% 이상·월말 예상 초과는 주의, 100% 이상은 초과"
            if got == expected
            else f"예상 {expected}, 실제 {got}"
        )

    async def audit_rules() -> tuple[bool, str]:
        report, _ = await audit_report(seed_books(), 7)
        found = {f["code"] for f in report["findings"]}
        missing = sorted(AUDIT_CODES - found)
        return not missing, (
            "쪼개기·반복 환불·중복 쿠폰·환불률 급변·재계산 실패·안전 장치 보류 추이를 모두 잡음"
            if not missing
            else f"못 잡은 규칙: {', '.join(missing)}"
        )

    async def policy_file() -> tuple[bool, str]:
        step = fc.load_config().proposal.max_step_ratio
        return 0 < step <= 1, f"한도 제안은 한 번에 {round(step * 100)}% 안에서만 바꿈"

    for name, run in (
        ("결산 계산 공식", closing_formula),
        ("대사 상태 나누기", recon_classes),
        ("판매자 정산 공식", settlement_formula),
        ("예산 경보 기준", budget_alerts),
        ("사후 감사 규칙", audit_rules),
        ("한도 제안 설정 읽기", policy_file),
    ):
        await _guard(name, run, checks)
    return checks


async def _get_json(client: httpx.AsyncClient, url: str) -> tuple[int | None, Any]:
    try:
        response = await client.get(url, timeout=CALL_SECONDS)
    except httpx.HTTPError:
        return None, None
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


async def _connection_checks(client: httpx.AsyncClient) -> list[Check]:
    checks: list[Check] = []
    safety = agent_url("safety")
    status, _ = await _get_json(client, f"{safety}/health")
    checks.append(
        Check(
            "안전 장치 연결",
            status == 200,
            "안전 장치가 응답함"
            if status == 200
            else "안전 장치에 닿지 않음. 정산 묶음 요청 등 돈이 나가는 일이 모두 멈춥니다",
        )
    )

    status, body = await _get_json(client, f"{safety}/api/safety/registry")
    actions = (
        {a.get("action"): a for a in (body or {}).get("data", {}).get("actions", [])}
        if status == 200
        else {}
    )
    rules_only = all(
        a in actions and actions[a].get("verifier") != "finance"
        for a in ("issue_coupon", "refund_request")
    )
    settle_c = actions.get("confirm_settlement", {}).get("base_grade") == "C"
    checks.append(
        Check(
            "쿠폰·환불은 규칙 검사, 정산 확정은 사람 승인",
            rules_only and settle_c,
            "안전 장치가 쿠폰·환불을 코드 규칙으로 검사하고 정산 확정은 사람 승인(C)으로 보냄"
            if rules_only and settle_c
            else "안전 장치 설정이 재무 재설계와 다름 (쿠폰·환불 검증자 또는 정산 확정 등급)",
        )
    )

    status, body = await _get_json(client, f"{safety}/api/safety/thresholds")
    rules = (body or {}).get("data", {}).get("rules") if status == 200 else None
    checks.append(
        Check(
            "기준표 읽기",
            isinstance(rules, list),
            "안전 장치 기준표를 읽음(예산·한도 화면)"
            if isinstance(rules, list)
            else "기준표를 못 읽어 한도 정책이 '확인할 수 없음'으로 보입니다",
            level="warn",
        )
    )

    for name, target, why in (
        ("쇼핑몰 연결", "shop-api", "주문·결제를 못 받아 결산·대사·정산이 비어 보일 수 있습니다"),
        ("데이터 AI 연결", "data", "장애 손실 리포트에 필요한 영향 분석을 못 받습니다"),
    ):
        status, _ = await _get_json(client, f"{agent_url(target)}/health")
        checks.append(
            Check(
                name,
                status == 200,
                "응답함" if status == 200 else f"닿지 않음. {why}",
                level="warn",
            )
        )
    return checks


async def run_self_test(
    recompute: Any, http_client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    started = time.perf_counter()
    checks = await _recompute_checks(recompute)
    checks += await _module_checks()
    client = http_client or httpx.AsyncClient()
    try:
        checks += await _connection_checks(client)
    finally:
        if http_client is None:
            await client.aclose()
    failed = [c for c in checks if not c.ok and c.level == "fail"]
    warned = [c for c in checks if not c.ok and c.level == "warn"]
    result = "fail" if failed else "warn" if warned else "pass"
    summary = {
        "pass": f"전부 정상입니다 ({len(checks)}개 항목).",
        "warn": f"계산은 정상이지만 {len(warned)}개 연결을 확인해 주세요.",
        "fail": f"{len(failed)}개 항목이 실패했습니다. 재무 담당자에게 알려 주세요.",
    }[result]
    return {
        "result": result,
        "summary": summary,
        "checks": [c.as_dict() for c in checks],
        "passed": sum(c.ok for c in checks),
        "total": len(checks),
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "ran_at": datetime.now(UTC).isoformat(),
    }
