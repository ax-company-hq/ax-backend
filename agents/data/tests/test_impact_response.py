from data_agent.impact import ImpactCounts, impact_to_response


def test_response_keys_and_values() -> None:
    assert impact_to_response(ImpactCounts(3, 2, 6000)) == {
        "failed_order_count": 3,
        "affected_customer_count": 2,
        "loss_amount": 6000,
    }


def test_response_empty() -> None:
    assert impact_to_response(ImpactCounts(0, 0, 0)) == {
        "failed_order_count": 0,
        "affected_customer_count": 0,
        "loss_amount": 0,
    }
