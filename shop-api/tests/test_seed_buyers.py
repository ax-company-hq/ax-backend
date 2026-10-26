import re

from ax_contracts.schemas import IdPrefix
from ax_shop_api.db import Db
from ax_shop_api.seed import BUYER_COUNT, seed


def test_seeded_buyer_ids_use_the_contract_prefix_and_four_digits() -> None:
    db = Db(":memory:")
    assert seed(db) is True
    with db.tx() as conn:
        ids = [row[0] for row in conn.execute("SELECT buyer_id FROM buyers ORDER BY buyer_id")]
    assert len(ids) == BUYER_COUNT
    assert all(re.fullmatch(rf"{IdPrefix.BUYER}\d{{4}}", buyer_id) for buyer_id in ids)
    assert ids[0] == "buy_0001" and ids[-1] == f"buy_{BUYER_COUNT:04d}"


def test_seed_is_skipped_when_data_already_exists() -> None:
    db = Db(":memory:")
    assert seed(db) is True
    assert seed(db) is False
