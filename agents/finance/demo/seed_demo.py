"""재무 AI 데모 데이터 넣기 (목 모드 전용).

로컬에서 safety(8001)·finance(8105)를 목 모드(`AGENT_TEMPLATE_MOCK=1`)로 띄운 뒤 실행한다.
마케팅·고객지원·통합 에이전트인 척 가짜 요청을 보내서 대시보드 `/finance` 화면을 채운다.
목 토큰(`mock:<이름>`)을 쓰므로 실제 운영 서버에서는 거부된다.

    uv run python agents/finance/demo/seed_demo.py
    uv run python agents/finance/demo/seed_demo.py --safety http://localhost:8001 --finance http://localhost:8105

가짜 데이터(finance `mock_seed.json`)
- 사건 ctx_inc_0002: 영향 고객 buy_0904~buy_0909 6명, 실패 주문 8건, 피해 214,500원
- 결제: ord_1003 32,000원, ord_1004 18,000원, ord_1005 150,000원
- 판매자 위험 점수: sel_1 12, sel_3 85, sel_4 30, sel_5 72
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

CTX = "ctx_inc_0002"
RUN = uuid.uuid4().hex[:6]  # 다시 실행해도 멱등성 키가 겹치지 않게


def execute(
    base: str, requester: str, action: str, key: str, params: dict[str, Any], context_id: str = CTX
) -> dict:
    body = {
        "action": action,
        "requester": requester,
        "context_id": context_id,
        "idempotency_key": f"{key}:{RUN}",
        "params": params,
    }
    r = httpx.post(
        f"{base}/api/safety/executions",
        json=body,
        headers={"Authorization": f"Bearer mock:{requester}"},
        timeout=40,
    )
    r.raise_for_status()
    return r.json()["data"]


def ask_finance(base: str, skill: str, data: dict[str, Any], key: str | None = None) -> dict:
    meta: dict[str, Any] = {
        "context_id": CTX,
        "token": "mock:orchestrator",
        "path": [],
        "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
    }
    if key:
        meta["idempotency_key"] = f"{key}:{RUN}"
    r = httpx.post(f"{base}/a2a", json={"meta": meta, "skill": skill, "input": data}, timeout=40)
    r.raise_for_status()
    return r.json()


def coupon(buyer: str, count: int, unit: int, total: int | None = None) -> dict:
    return {
        "purpose_code": "compensation",
        "buyer_id": buyer,
        "count": count,
        "unit_value": unit,
        "total_value": count * unit if total is None else total,
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔에서도 한글이 깨지지 않게
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--safety", default="http://localhost:8001")
    parser.add_argument("--finance", default="http://localhost:8105")
    args = parser.parse_args()
    s, f = args.safety, args.finance
    lines: list[str] = []

    def show(title: str, result: dict) -> None:
        brief = {
            k: result.get(k)
            for k in ("grade", "status", "hold_reason", "computed_amount")
            if k in result
        }
        lines.append(f"- {title}: {json.dumps(brief or result, ensure_ascii=False)}")

    # 0) 돈이 나가지 않는 일: 바로 처리
    show(
        "장애 안내 공지 올리기",
        execute(
            s,
            "marketing",
            "publish_notice",
            "ntc-1",
            {"draft_id": "drf_1", "channel_code": "banner"},
        ),
    )
    show("장애 기록 남기기", execute(s, "hr", "memory.card_write", "card-1", {"kind": "incident"}))

    # 1) 장애 보상 쿠폰 (마케팅 → 안전 레이어 → 재무가 다시 계산)
    for buyer in ("buy_0904", "buy_0905", "buy_0906"):
        show(
            f"보상 쿠폰 {buyer} 1장×5,000원",
            execute(s, "marketing", "issue_coupon", f"cpn-{buyer}", coupon(buyer, 1, 5000)),
        )
    show(
        "영향 고객이 아닌 buy_0999에게 쿠폰",
        execute(s, "marketing", "issue_coupon", "cpn-zz", coupon("buy_0999", 1, 5000)),
    )
    show(
        "총액을 틀리게 적은 쿠폰(3장×5,000원인데 5,000원)",
        execute(s, "marketing", "issue_coupon", "cpn-wrong", coupon("buy_0907", 3, 5000, 5000)),
    )
    show(
        "한 고객에게 6만 원어치 쿠폰(고객당 5만 원 기준 넘음)",
        execute(s, "marketing", "issue_coupon", "cpn-big", coupon("buy_0908", 3, 20000)),
    )

    show(
        "한 번에 50만 원 쿠폰(재무 AI 재량 한도 45만 원도 넘음 → 사람 확인)",
        execute(s, "marketing", "issue_coupon", "cpn-huge", coupon("buy_0909", 25, 20000)),
    )

    # 2) 환불 (고객지원 → 안전 레이어 → 재무가 결제 금액과 비교)
    show(
        "ord_1003 환불 20,000원(결제 32,000원)",
        execute(
            s,
            "support",
            "refund_request",
            "rfd-1003",
            {"order_id": "ord_1003", "refund_amount": 20000},
        ),
    )
    show(
        "ord_1004 환불 25,000원(결제 18,000원보다 큼)",
        execute(
            s,
            "support",
            "refund_request",
            "rfd-1004",
            {"order_id": "ord_1004", "refund_amount": 25000},
        ),
    )
    show(
        "ord_1005 환불 150,000원(환불 10만 원 기준 넘음)",
        execute(
            s,
            "support",
            "refund_request",
            "rfd-1005",
            {"order_id": "ord_1005", "refund_amount": 150000},
        ),
    )

    # 3) 마켓운영: 판매자 심사·정산 보류 (통합 에이전트 → 재무)
    for seller, docs in (("sel_4", True), ("sel_5", False), ("sel_9", True)):
        out = ask_finance(
            f,
            "seller_review",
            {"seller_id": seller, "category_codes": ["book"], "documents_verified": docs},
            key=f"rev-{seller}",
        )
        lines.append(
            f"- 판매자 심사 {seller}: {out['output'].get('decision')} "
            f"({out['output'].get('reason_code')})"
        )
    hold = ask_finance(
        f,
        "settlement_hold",
        {"seller_id": "sel_3", "reason_code": "fraud_risk", "hold_amount": 320000},
        key="hold-sel_3",
    )
    lines.append(f"- 정산 보류 sel_3: {hold['output'].get('status')}")

    # 5) 정책상 항상 사람이 보는 일: 판매자 정산 확정
    show(
        "판매자 sel_1 정산 확정(항상 사람 확인)",
        execute(
            s,
            "finance",
            "confirm_settlement",
            "stl-sel_1",
            {"settlement_id": "stl_1", "seller_id": "sel_1"},
        ),
    )

    print("\n".join(lines))
    print("\n대시보드 http://localhost:3001/finance 에서 확인하세요.")


if __name__ == "__main__":
    main()
