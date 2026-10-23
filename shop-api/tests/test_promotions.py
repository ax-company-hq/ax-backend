from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ax_agent_template import events
from ax_shop_api.db import Db
from ax_shop_api.main import create_app

MARKETING = {"Authorization": "Bearer mock:marketing"}
SUPPORT = {"Authorization": "Bearer mock:support"}
FINANCE = {"Authorization": "Bearer mock:finance"}
OTHER = {"Authorization": "Bearer mock:data"}


class Now:
    def __init__(self) -> None:
        self.moment = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.moment


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setenv("SHOP_MONITOR_INTERVAL_S", "0")
    events.reset_mock_bus()


@pytest.fixture
def now() -> Now:
    return Now()


@pytest.fixture
def client(now: Now) -> TestClient:
    app = create_app(Db(":memory:"))
    app.state.promotions.now = now
    return TestClient(app)


def _banner(notice_id: str = "ntc_0001", **extra) -> dict:
    return {"notice_id": notice_id, "text": "결제 이용이 원활하지 않습니다.", **extra}


def _coupon(key: str = "ctx_inc_0001:issue_coupon:buy_0001", **extra) -> dict:
    body = {
        "idempotency_key": key,
        "purpose_code": "compensation",
        "buyer_id": "buy_0001",
        "count": 1,
        "unit_value": 2000,
    }
    return body | extra


# --- 권한 ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("post", "/api/shop/banners", _banner()),
        ("delete", "/api/shop/banners/ntc_0001", None),
        ("post", "/api/shop/coupons", _coupon()),
    ],
)
def test_writes_are_marketing_only(client: TestClient, method: str, url: str, body) -> None:  # noqa: ANN001
    send = getattr(client, method)
    kwargs = {"json": body} if body is not None else {}
    assert send(url, **kwargs).status_code == 401  # 토큰 없음
    for caller in (SUPPORT, FINANCE, OTHER):  # 고객지원도 쿠폰·공지는 못 쓴다
        assert send(url, headers=caller, **kwargs).status_code == 403
    assert client.get("/api/shop/coupons").status_code == 401  # 쿠폰 조회도 토큰이 필요하다


def test_coupon_reads_are_limited_to_departments_that_verify_or_deduplicate(
    client: TestClient,
) -> None:
    issued = client.post("/api/shop/coupons", json=_coupon(), headers=MARKETING).json()["data"]
    url = f"/api/shop/coupons/{issued['coupon_id']}"
    for caller in (MARKETING, SUPPORT, FINANCE):
        assert client.get(url, headers=caller).status_code == 200
    assert client.get(url, headers=OTHER).status_code == 403


# --- 공지 배너 -------------------------------------------------------------


def test_banner_is_visible_to_customers_until_it_is_ended(client: TestClient) -> None:
    assert client.get("/api/shop/banners").json()["data"] == []  # 고객 화면은 토큰 없이 읽는다
    posted = client.post("/api/shop/banners", json=_banner(), headers=MARKETING)
    assert posted.status_code == 201 and posted.json()["data"]["status"] == "active"
    assert [b["notice_id"] for b in client.get("/api/shop/banners").json()["data"]] == ["ntc_0001"]
    ended = client.delete("/api/shop/banners/ntc_0001", headers=MARKETING).json()["data"]
    assert ended["status"] == "ended" and ended["ended_at"]
    assert client.get("/api/shop/banners").json()["data"] == []


def test_posting_the_same_notice_twice_keeps_one_banner(client: TestClient) -> None:
    first = client.post("/api/shop/banners", json=_banner(), headers=MARKETING).json()["data"]
    again = client.post(
        "/api/shop/banners", json=_banner(text="다른 문구"), headers=MARKETING
    ).json()["data"]
    assert again == first  # 첫 게시 내용이 기준이다
    assert len(client.get("/api/shop/banners").json()["data"]) == 1


def test_banner_respects_its_validity_window(client: TestClient, now: Now) -> None:
    start = now() + timedelta(hours=1)
    end = now() + timedelta(hours=2)
    body = _banner(valid_from=start.isoformat(), valid_to=end.isoformat())
    client.post("/api/shop/banners", json=body, headers=MARKETING)
    assert client.get("/api/shop/banners").json()["data"] == []  # 아직 시작 전
    now.moment = start + timedelta(minutes=1)
    assert len(client.get("/api/shop/banners").json()["data"]) == 1
    now.moment = end + timedelta(minutes=1)
    assert client.get("/api/shop/banners").json()["data"] == []  # 기간 지남


def test_banner_validation_and_unknown_banner(client: TestClient) -> None:
    too_long = client.post("/api/shop/banners", json=_banner(text="가" * 201), headers=MARKETING)
    assert too_long.status_code == 422
    assert (
        client.post("/api/shop/banners", json=_banner(text=""), headers=MARKETING).status_code
        == 422
    )
    missing = client.delete("/api/shop/banners/ntc_9999", headers=MARKETING)
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "BANNER_NOT_FOUND")


# --- 쿠폰 ------------------------------------------------------------------


def test_coupon_total_is_computed_by_the_server(client: TestClient) -> None:
    body = _coupon(count=3, unit_value=1500, total_value=999999)  # 요청이 보낸 총액은 무시한다
    response = client.post("/api/shop/coupons", json=body, headers=MARKETING)
    data = response.json()["data"]
    assert (response.status_code, data["total_value"]) == (201, 4500)
    assert response.json()["meta"] == {"replayed": False}
    assert data["coupon_id"].startswith("cpn_") and data["campaign_id"] is None


def test_same_idempotency_key_returns_the_previous_coupon(client: TestClient) -> None:
    first = client.post("/api/shop/coupons", json=_coupon(), headers=MARKETING)
    again = client.post("/api/shop/coupons", json=_coupon(), headers=MARKETING)
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["data"] == first.json()["data"]
    assert again.json()["meta"] == {"replayed": True}
    assert len(client.get("/api/shop/coupons", headers=MARKETING).json()["data"]) == 1


def test_same_key_with_different_content_is_rejected(client: TestClient) -> None:
    client.post("/api/shop/coupons", json=_coupon(), headers=MARKETING)
    other = client.post("/api/shop/coupons", json=_coupon(unit_value=5000), headers=MARKETING)
    assert (other.status_code, other.json()["error"]["code"]) == (409, "IDEMPOTENCY_MISMATCH")
    assert len(client.get("/api/shop/coupons", headers=MARKETING).json()["data"]) == 1


def test_idempotency_key_is_required(client: TestClient) -> None:
    body = _coupon()
    del body["idempotency_key"]
    assert client.post("/api/shop/coupons", json=body, headers=MARKETING).status_code == 422
    assert (
        client.post("/api/shop/coupons", json=_coupon(key=""), headers=MARKETING).status_code == 422
    )


def test_purpose_code_rules_and_unknown_buyer(client: TestClient) -> None:
    def post(**extra):
        return client.post("/api/shop/coupons", json=_coupon(**extra), headers=MARKETING)

    assert post(key="k1", purpose_code="campaign").json()["error"]["code"] == "INVALID_COUPON"
    assert post(key="k2", campaign_id="cmp_0001").json()["error"]["code"] == "INVALID_COUPON"
    assert post(key="k3", purpose_code="gift").status_code == 422
    assert post(key="k4", buyer_id="buy_xxxx").status_code == 404
    campaign = post(key="k5", purpose_code="campaign", campaign_id="cmp_0001")
    assert campaign.status_code == 201 and campaign.json()["data"]["campaign_id"] == "cmp_0001"
    assert (
        post(key="k6", count=0).status_code == 422
        and post(key="k7", unit_value=-1).status_code == 422
    )


def test_coupon_filters(client: TestClient) -> None:
    client.post("/api/shop/coupons", json=_coupon(key="a"), headers=MARKETING)
    client.post("/api/shop/coupons", json=_coupon(key="b", buyer_id="buy_0002"), headers=MARKETING)
    by_buyer = client.get("/api/shop/coupons?buyer_id=buy_0002", headers=SUPPORT).json()["data"]
    assert [c["buyer_id"] for c in by_buyer] == ["buy_0002"]
    assert client.get("/api/shop/coupons/cpn_9999", headers=SUPPORT).status_code == 404


def test_issuing_coupons_and_banners_publishes_no_events_from_shop_api(client: TestClient) -> None:
    client.post("/api/shop/banners", json=_banner(), headers=MARKETING)
    client.post("/api/shop/coupons", json=_coupon(), headers=MARKETING)
    assert not events.MOCK_PUBLISHED  # notice.published·coupon.issued는 마케팅 에이전트가 발행한다


def test_context_id_is_stored_when_given(client: TestClient) -> None:
    banner = client.post(
        "/api/shop/banners", json=_banner(context_id="ctx_inc_0001"), headers=MARKETING
    ).json()["data"]
    coupon = client.post(
        "/api/shop/coupons", json=_coupon(context_id="ctx_inc_0001"), headers=MARKETING
    ).json()["data"]
    assert banner["context_id"] == coupon["context_id"] == "ctx_inc_0001"
    plain = client.post("/api/shop/banners", json=_banner("ntc_0002"), headers=MARKETING)
    assert plain.json()["data"]["context_id"] is None  # 선택 필드라 없으면 비어 있다
