"""공지 배너(SHOP-08)와 쿠폰 지급(SHOP-10) 업무 규칙. 웹 프레임워크에 의존하지 않는다.

쓰기는 마케팅 관문 토큰만 호출한다(권한 확인은 API 계층). 이벤트는 여기서 발행하지 않는다:
`notice.published`·`coupon.issued`는 마케팅 에이전트가 `execute()` 뒤에 발행한다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ax_shop_api.db import TENANT_ID, Db
from ax_shop_api.service import ShopError


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Promotions:
    def __init__(self, db: Db, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.db = db
        self.now = now

    # --- 공지 배너 ----------------------------------------------------------
    def post_banner(
        self,
        notice_id: str,
        text: str,
        valid_from: datetime | None,
        valid_to: datetime | None,
    ) -> dict[str, Any]:
        """공지를 배너로 게시한다. 같은 `notice_id`를 다시 게시하면 이전 배너를 그대로 돌려준다."""
        with self.db.tx() as conn:
            existing = conn.execute(
                "SELECT * FROM banners WHERE notice_id = ?", (notice_id,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO banners VALUES (?, ?, 'active', ?, ?, ?, NULL, ?, NULL)",
                    (
                        notice_id,
                        text,
                        _iso(valid_from) if valid_from else None,
                        _iso(valid_to) if valid_to else None,
                        TENANT_ID,
                        _iso(self.now()),
                    ),
                )
            row = conn.execute("SELECT * FROM banners WHERE notice_id = ?", (notice_id,)).fetchone()
        return self._banner(row)

    def end_banner(self, notice_id: str) -> dict[str, Any]:
        """배너를 내린다(복구 공지 뒤 장애 공지를 내릴 때). 이미 내린 배너는 그대로 돌려준다."""
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM banners WHERE notice_id = ?", (notice_id,)).fetchone()
            if row is None:
                raise ShopError("BANNER_NOT_FOUND", "배너를 찾을 수 없다", 404)
            if row["status"] == "active":
                conn.execute(
                    "UPDATE banners SET status = 'ended', ended_at = ? WHERE notice_id = ?",
                    (_iso(self.now()), notice_id),
                )
            row = conn.execute("SELECT * FROM banners WHERE notice_id = ?", (notice_id,)).fetchone()
        return self._banner(row)

    def active_banners(self) -> list[dict[str, Any]]:
        """고객 화면에 보일 배너. 게시 중이고 기간 안인 것만 낸다."""
        now = _iso(self.now())
        with self.db.tx() as conn:
            rows = conn.execute(
                "SELECT * FROM banners WHERE status = 'active' "
                "AND (valid_from IS NULL OR valid_from <= ?) "
                "AND (valid_to IS NULL OR valid_to >= ?) ORDER BY created_at, notice_id",
                (now, now),
            ).fetchall()
        return [self._banner(row) for row in rows]

    @staticmethod
    def _banner(row: Any) -> dict[str, Any]:
        keys = ("notice_id", "text", "status", "valid_from", "valid_to", "created_at", "ended_at")
        return {key: row[key] for key in keys}

    # --- 쿠폰 ---------------------------------------------------------------
    def issue_coupon(
        self,
        idempotency_key: str,
        purpose_code: str,
        buyer_id: str,
        count: int,
        unit_value: int,
        campaign_id: str | None,
    ) -> tuple[dict[str, Any], bool]:
        """쿠폰을 발급한다. 총액은 건수×단가로 코드가 계산한다.

        같은 `idempotency_key`의 재요청은 이전 쿠폰을 돌려주고(`replayed=True`), 내용이 다르면
        409다. 보상 쿠폰에는 `campaign_id`가 없고 캠페인 쿠폰에는 필수다.
        """
        if purpose_code == "campaign" and not campaign_id:
            raise ShopError("INVALID_COUPON", "캠페인 쿠폰에는 campaign_id가 필요하다")
        if purpose_code == "compensation" and campaign_id:
            raise ShopError("INVALID_COUPON", "보상 쿠폰에는 campaign_id가 없어야 한다")
        total = count * unit_value
        with self.db.tx() as conn:
            existing = conn.execute(
                "SELECT * FROM coupons WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                same = (
                    existing["purpose_code"],
                    existing["buyer_id"],
                    existing["count"],
                    existing["unit_value"],
                    existing["campaign_id"],
                ) == (purpose_code, buyer_id, count, unit_value, campaign_id)
                if not same:
                    raise ShopError(
                        "IDEMPOTENCY_MISMATCH", "같은 키로 다른 내용을 요청할 수 없다", 409
                    )
                return self._coupon(existing), True
            if not conn.execute("SELECT 1 FROM buyers WHERE buyer_id = ?", (buyer_id,)).fetchone():
                raise ShopError("BUYER_NOT_FOUND", "구매자를 찾을 수 없다", 404)
            coupon_id = self.db.next_id(conn, "coupon", "cpn_")
            conn.execute(
                "INSERT INTO coupons VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    coupon_id,
                    buyer_id,
                    purpose_code,
                    campaign_id,
                    count,
                    unit_value,
                    total,
                    idempotency_key,
                    TENANT_ID,
                    _iso(self.now()),
                ),
            )
            row = conn.execute("SELECT * FROM coupons WHERE coupon_id = ?", (coupon_id,)).fetchone()
        return self._coupon(row), False

    def get_coupon(self, coupon_id: str) -> dict[str, Any]:
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM coupons WHERE coupon_id = ?", (coupon_id,)).fetchone()
        if row is None:
            raise ShopError("COUPON_NOT_FOUND", "쿠폰을 찾을 수 없다", 404)
        return self._coupon(row)

    def list_coupons(
        self, buyer_id: str | None = None, purpose_code: str | None = None
    ) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM coupons WHERE 1 = 1", []
        for column, value in (("buyer_id", buyer_id), ("purpose_code", purpose_code)):
            if value:
                sql += f" AND {column} = ?"
                args.append(value)
        with self.db.tx() as conn:
            rows = conn.execute(sql + " ORDER BY coupon_id", args).fetchall()
        return [self._coupon(row) for row in rows]

    @staticmethod
    def _coupon(row: Any) -> dict[str, Any]:
        keys = (
            "coupon_id",
            "buyer_id",
            "purpose_code",
            "campaign_id",
            "count",
            "unit_value",
            "total_value",
            "created_at",
        )
        return {key: row[key] for key in keys}
