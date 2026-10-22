import re

from ax_memory.seed import POLICY_CONDITION_CODES, POLICY_SECTIONS, seed_policies
from ax_memory.store import InMemoryCardStore


def test_first_run_creates_one_card_per_section() -> None:
    store = InMemoryCardStore()
    report = seed_policies(store)
    assert report.created == len(POLICY_SECTIONS)
    assert report.existing == 0
    assert len(store.list()) == len(POLICY_SECTIONS)


def test_second_run_is_idempotent() -> None:
    store = InMemoryCardStore()
    seed_policies(store)
    report = seed_policies(store)
    assert report.created == 0
    assert report.existing == len(POLICY_SECTIONS)
    assert len(store.list()) == len(POLICY_SECTIONS)


def test_cards_are_policy_cards_of_default_tenant() -> None:
    store = InMemoryCardStore()
    seed_policies(store)
    for card in store.list():
        assert card.kind == "policy"
        assert card.tenant_id == "shop-demo"
        assert card.author == "memory-seed"
        assert card.confidence == "high"
        assert card.context_id.startswith("ctx_ntc_")
        assert card.extra["version"] == "2026-10-22"
        assert card.extra["doc_id"] in card.tags


def test_condition_codes() -> None:
    assert {"LATE_3D", "DEFECT_RETURN_OK", "PAYMENT_OUTAGE"} <= POLICY_CONDITION_CODES
    store = InMemoryCardStore()
    seed_policies(store)
    codes = {c.extra["condition_code"] for c in store.list() if "condition_code" in c.extra}
    assert codes == POLICY_CONDITION_CODES


def test_support_kb_facts_present() -> None:
    store = InMemoryCardStore()
    seed_policies(store)
    text = " ".join(c.body for c in store.list())
    assert "수령 후 7일 이내 단순 변심 환불은 왕복 배송비를 고객이 부담한다" in text
    assert "판매자 부담으로 환불한다" in text
    assert "영업일 기준 2~3일" in text


def test_no_personal_info() -> None:
    store = InMemoryCardStore()
    seed_policies(store)
    pattern = re.compile(r"[\w.]+@[\w.]+|\d{2,3}-\d{3,4}-\d{4}")
    for card in store.list():
        assert not pattern.search(card.title + card.body)
