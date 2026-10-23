from data_agent.component_map import load_entries, lookup


def test_payment() -> None:
    match = lookup("shop-api/payment/gateway.py")
    assert match is not None
    assert (match.component, match.metrics) == ("payment", ("payment_fail_rate",))


def test_multiple_prefixes_same_component() -> None:
    for path in ("shop-api/cart/a.py", "shop-web/cart/b.tsx"):
        match = lookup(path)
        assert match is not None and match.component == "cart"
        assert match.metrics == ("order_fail_count",)


def test_checkout_has_two_metrics() -> None:
    match = lookup("shop-web/checkout/page.tsx")
    assert match is not None
    assert match.metrics == ("payment_fail_rate", "order_count")


def test_unknown_path_is_none() -> None:
    assert lookup("docs/readme.md") is None


def test_longest_prefix_wins() -> None:
    entries = load_entries()
    from data_agent.component_map import ComponentMatch

    entries.append(ComponentMatch("payment-sdk", ("order_count",), "shop-api/payment/sdk/"))
    match = lookup("shop-api/payment/sdk/x.py", entries)
    assert match is not None and match.component == "payment-sdk"


def test_metrics_are_known_names() -> None:
    known = {"payment_fail_rate", "order_fail_count", "order_count", "inquiry_count"}
    assert all(set(e.metrics) <= known for e in load_entries())
