import pytest

from data_agent.metrics.fraud import SellerFacts, score_seller


def test_low_risk():
    r = score_seller(SellerFacts(400, 0.0, False, 3, 0.05))
    assert r.level == "low" and 0 <= r.score < 30
    assert [x.rule_code for x in r.reasons] == ["FRAUD_SIMILAR"]


def test_mid_risk():
    r = score_seller(SellerFacts(3, 0.2, True, 10, 0.2))
    assert r.level == "mid" and 30 <= r.score < 70
    codes = {x.rule_code for x in r.reasons}
    assert codes == {"NEW_ACCOUNT", "LOW_PRICE", "DUP_BUSINESS", "FRAUD_SIMILAR"}


def test_high_risk_and_cap():
    r = score_seller(SellerFacts(1, 0.9, True, 120, 0.95))
    assert r.level == "high" and 70 <= r.score <= 100
    assert {x.rule_code for x in r.reasons} == {
        "NEW_ACCOUNT",
        "LOW_PRICE",
        "DUP_BUSINESS",
        "BULK_LISTING",
        "FRAUD_SIMILAR",
    }
    assert score_seller(SellerFacts(0, 1.0, True, 50, 1.0)).score == 100


def test_zero_point_rules_excluded_and_boundaries():
    r = score_seller(SellerFacts(7, 0.0, False, 49, 0.0))
    assert r.score == 0 and r.reasons == []
    r2 = score_seller(SellerFacts(6, 0.0, False, 50, 0.0))
    assert {x.rule_code for x in r2.reasons} == {"NEW_ACCOUNT", "BULK_LISTING"}
    assert r2.score == 30 and r2.level == "mid"


def test_deterministic():
    f = SellerFacts(2, 0.33, True, 55, 0.61)
    assert score_seller(f) == score_seller(f)
    assert isinstance(score_seller(f).score, int)


@pytest.mark.parametrize(
    "facts",
    [
        SellerFacts(-1, 0.0, False, 0, 0.0),
        SellerFacts(1, -0.1, False, 0, 0.0),
        SellerFacts(1, 1.1, False, 0, 0.0),
        SellerFacts(1, 0.0, False, -5, 0.0),
        SellerFacts(1, 0.0, False, 0, 1.5),
        SellerFacts(1, 0.0, False, 0, -0.2),
    ],
)
def test_out_of_range_rejected(facts):
    with pytest.raises(ValueError):
        score_seller(facts)
