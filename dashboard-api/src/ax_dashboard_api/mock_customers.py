"""고객관리 탭의 목 모드 데이터 (읽기 전용). 실제 값은 support skill이 코드로 센다.

모양은 support의 출력(`signal_schemas`·`suggest_schemas`·`payment_schemas`·`insight_schemas`·
`change_schemas`)을 그대로 따른다. 부서끼리 import하지 않으므로 dict로 둔다.
"""

from __future__ import annotations

from typing import Any

from ax_dashboard_api.envelope import ApiError

_EVALUATED_AT = "2026-10-26T09:00:00Z"

_COUNTS = {
    "inquiries_30d": 0,
    "open_inquiries": 0,
    "unanswered_over_24h": 0,
    "escalated_inquiries": 0,
    "angry_messages_30d": 0,
    "frustrated_messages_30d": 0,
    "claims_90d": 0,
    "rejected_claims_90d": 0,
}

_SIGNALS: dict[str, dict[str, Any]] = {
    "buy_0001": {
        "buyer_id": "buy_0001",
        "level": "high",
        "score": 9,
        "reasons": [
            {"code": "unanswered_over_24h", "count": 1, "points": 3},
            {"code": "angry_messages_30d", "count": 2, "points": 4},
            {"code": "escalated_inquiries", "count": 1, "points": 2},
        ],
        "counts": {
            **_COUNTS,
            "inquiries_30d": 3,
            "open_inquiries": 1,
            "unanswered_over_24h": 1,
            "escalated_inquiries": 1,
            "angry_messages_30d": 2,
        },
        "last_contact_at": "2026-10-24T02:10:00Z",
        "recommended_action": "personal_followup",
        "claim_pattern_review": False,
    },
    "buy_0002": {
        "buyer_id": "buy_0002",
        "level": "medium",
        "score": 5,
        "reasons": [
            {"code": "frustrated_messages_30d", "count": 2, "points": 2},
            {"code": "claims_90d", "count": 3, "points": 3},
        ],
        "counts": {**_COUNTS, "inquiries_30d": 2, "frustrated_messages_30d": 2, "claims_90d": 3},
        "last_contact_at": "2026-10-23T11:30:00Z",
        "recommended_action": "proactive_check",
        "claim_pattern_review": True,
    },
}

_LEVEL_RANK = {"low": 0, "medium": 1, "high": 2}


def at_risk(min_level: str, limit: int) -> dict[str, Any]:
    items = [s for s in _SIGNALS.values() if _LEVEL_RANK[s["level"]] >= _LEVEL_RANK[min_level]]
    items.sort(key=lambda s: -s["score"])
    return {
        "evaluated_at": _EVALUATED_AT,
        "min_level": min_level,
        "total": len(items),
        "items": items[:limit],
    }


def signals(buyer_id: str) -> dict[str, Any]:
    found = _SIGNALS.get(buyer_id)
    if found is not None:
        return found
    return {
        "buyer_id": buyer_id,
        "level": "low",
        "score": 0,
        "reasons": [],
        "counts": dict(_COUNTS),
        "last_contact_at": None,
        "recommended_action": "none",
        "claim_pattern_review": False,
    }


def compensation_suggestion(inquiry_id: int) -> dict[str, Any]:
    if inquiry_id == 1:
        return {
            "eligible": True,
            "reason_code": "late_delivery",
            "message": "약속 배송일보다 3일 늦게 도착했다 (정책표 지연 보상 대상)",
            "unit_value": 2000,
            "count": 1,
            "total_value": 2000,
            "facts": {
                "order_id": "ord_0001",
                "promised_delivery_at": "2026-10-19T09:00:00Z",
                "shipped_at": "2026-10-18T09:00:00Z",
                "delivered_at": "2026-10-22T09:00:00Z",
                "days_late": 3,
            },
            "request": {
                "inquiry_id": 1,
                "buyer_id": "buy_0001",
                "reason_code": "late_delivery",
                "count": 1,
            },
            "notes": [],
        }
    if inquiry_id == 2:
        return {
            "eligible": False,
            "reason_code": None,
            "message": "약속 배송일 안에 도착해 지연 보상 대상이 아니다",
            "unit_value": 0,
            "count": 0,
            "total_value": 0,
            "facts": None,
            "request": None,
            "notes": [],
        }
    raise ApiError(404, "NOT_FOUND", f"문의 {inquiry_id}를 찾을 수 없다")


def payment_diagnosis(inquiry_id: int) -> dict[str, Any]:
    if inquiry_id == 1:
        return {
            "situation": "payment_failed",
            "next_action": "wait_and_retry",
            "message": "결제가 결제 서버 오류로 실패했다. 잠시 뒤 같은 주문으로 다시 시도하면 된다",
            "likely_cause": "payment_service_fault",
            "duplicate_amount": 0,
            "facts": {
                "order_id": "ord_0001",
                "order_status": "payment_failed",
                "total_amount": 18000,
                "attempts": [
                    {
                        "payment_id": "pay_0001",
                        "amount": 18000,
                        "method_code": "card",
                        "status": "failed",
                        "failure_code": "PG_UNAVAILABLE",
                        "created_at": "2026-10-22T09:00:00Z",
                    }
                ],
                "succeeded_count": 0,
                "failed_count": 1,
                "last_failure_code": "PG_UNAVAILABLE",
            },
            "notes": [],
        }
    if inquiry_id == 2:
        return {
            "situation": "paid",
            "next_action": "none",
            "message": "결제가 정상으로 끝났다",
            "likely_cause": None,
            "duplicate_amount": 0,
            "facts": None,
            "notes": [],
        }
    raise ApiError(404, "NOT_FOUND", f"문의 {inquiry_id}를 찾을 수 없다")


def inquiry_surge() -> dict[str, Any]:
    return {
        "window_minutes": 60,
        "baseline_hours": 24,
        "baseline_method": "mean",
        "evaluated_at": _EVALUATED_AT,
        "total_in_window": 9,
        "surges": [
            {
                "topic_code": "payment",
                "count": 7,
                "baseline_per_window": 1.5,
                "ratio": 4.7,
                "sample_inquiry_ids": [1, 3, 4],
            }
        ],
    }


def change_requests(inquiry_id: int | None, status: str | None) -> dict[str, Any]:
    items = [
        {
            "tenant_id": "shop-demo",
            "context_id": "ctx_req_0001",
            "inquiry_id": 1,
            "source_ref": "inq_1",
            "status": "requested",
            "request_id": "REQ-1",
            "duplicate_of": None,
            "result": None,
            "notice_id": None,
            "seq": 1,
            "created_at": "2026-10-23T01:00:00Z",
            "updated_at": "2026-10-23T01:00:00Z",
        }
    ]
    items = [
        i
        for i in items
        if (inquiry_id is None or i["inquiry_id"] == inquiry_id)
        and (status is None or i["status"] == status)
    ]
    return {"items": items}
