"""더미 데이터(SHOP-07): 도서·문구 판매자·상품·구매자. 비어 있는 DB에만 한 번 넣는다.

개인정보는 만들지 않는다. 구매자는 가명 ID(`buy_xxxx`)뿐이다.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ax_contracts.schemas import IdPrefix
from ax_shop_api.db import TENANT_ID, Db

SELLERS = [
    ("sel_0001", "책마루", ["book"]),
    ("sel_0002", "종이나라", ["stationery"]),
    ("sel_0003", "북앤펜", ["book", "stationery"]),
]
# (product_id, seller_id, name, category_code, price, stock)
PRODUCTS = [
    ("prd_0001", "sel_0001", "파이썬 입문서", "book", 18000, 200),
    ("prd_0002", "sel_0001", "데이터 분석 실전", "book", 24000, 150),
    ("prd_0003", "sel_0001", "에세이 모음집", "book", 13000, 300),
    ("prd_0004", "sel_0002", "무지 노트 3권 세트", "stationery", 7500, 500),
    ("prd_0005", "sel_0002", "젤펜 12색", "stationery", 9800, 400),
    ("prd_0006", "sel_0003", "독서대", "stationery", 15000, 120),
    ("prd_0007", "sel_0003", "소설 베스트 3종", "book", 31000, 90),
    ("prd_0008", "sel_0003", "만년필 입문 세트", "stationery", 29000, 60),
]
BUYER_COUNT = 30


def seed_buyer_id(number: int) -> str:
    return f"{IdPrefix.BUYER}{number:04d}"


# 고정 데모 계정(`buy_0001`~). 보유기간 삭제·삭제 집행은 이 ID를 건드리지 않는다.
SEED_BUYER_IDS = frozenset(seed_buyer_id(number) for number in range(1, BUYER_COUNT + 1))


def seed(db: Db) -> bool:
    """비어 있을 때만 넣고 넣었으면 True를 돌려준다."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM sellers LIMIT 1").fetchone():
            return False
        for seller_id, name, categories in SELLERS:
            conn.execute(
                "INSERT INTO sellers VALUES (?, ?, ?, ?, NULL, ?)",
                (seller_id, name, ",".join(categories), TENANT_ID, now),
            )
        for product_id, seller_id, name, category, price, stock in PRODUCTS:
            conn.execute(
                "INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, 1, ?, NULL, ?, ?)",
                (product_id, seller_id, name, category, price, stock, TENANT_ID, now, now),
            )
        # 시드가 쓴 번호 다음부터 새 ID를 발급한다
        conn.execute("INSERT INTO counters VALUES ('product', ?)", (len(PRODUCTS),))
        for number in range(1, BUYER_COUNT + 1):
            conn.execute(
                "INSERT INTO buyers VALUES (?, ?, NULL, ?)",
                (seed_buyer_id(number), TENANT_ID, now),
            )
    return True
