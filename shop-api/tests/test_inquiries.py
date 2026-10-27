"""#385: 고객 문의 접수 중계(shop-api → 고객지원 create_inquiry)."""

import logging

import pytest
from fastapi.testclient import TestClient

from ax_agent_template import events
from ax_contracts.schemas import AgentRequest, AgentResult, RequestMeta, TaskStatus
from ax_shop_api import inquiries, support_client
from ax_shop_api.db import Db
from ax_shop_api.main import create_app
from ax_shop_api.service import ShopError

SECRET = "내 전화번호는 010-1234-5678 입니다"
META = {"context_id": "ctx_shop_x", "token": "t", "deadline": "2030-01-01T00:00:00Z"}


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setenv("SHOP_MONITOR_INTERVAL_S", "0")
    events.reset_mock_bus()


class FakeSupport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, AgentRequest]] = []
        self.result: AgentResult | None = None

    async def __call__(self, target: str, request: AgentRequest) -> AgentResult:
        self.calls.append((target, request))
        if self.result is not None:
            return self.result
        meta: RequestMeta = request.meta
        output = {
            "id": 11,
            "subject": request.untrusted["subject"],
            "status": "open",
            "order_id": request.input["order_id"],
            "updated_at": "2026-10-27T00:00:00Z",
            "preview": "원문 미리보기",
            "messages": [{"role": "customer", "text": "원문"}],
        }
        return AgentResult(meta=meta, status=TaskStatus.COMPLETED, output=output)


@pytest.fixture
def support(monkeypatch: pytest.MonkeyPatch) -> FakeSupport:
    fake = FakeSupport()
    monkeypatch.setattr(support_client, "SEND", fake)
    return fake


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Db(":memory:")))


def _order(client: TestClient, buyer_id: str = "buy_0001") -> str:
    made = client.post(
        "/api/shop/orders",
        json={"buyer_id": buyer_id, "items": [{"product_id": "prd_0001", "qty": 1}]},
    )
    return made.json()["data"]["order_id"]


def _body(order_id: str, **extra) -> dict:  # noqa: ANN003
    return {"buyer_id": "buy_0001", "order_id": order_id, "body": SECRET} | extra


def _result(status: TaskStatus, outcome: str | None = None, output: dict | None = None):  # noqa: ANN202
    return AgentResult(
        meta=RequestMeta(**META), status=status, outcome=outcome, output=output or {}
    )


def test_정상_접수는_정수_user_id와_원문을_untrusted로_넘기고_접수_결과만_돌려준다(
    client: TestClient, support: FakeSupport
) -> None:
    order_id = _order(client)
    response = client.post("/api/shop/inquiries", json=_body(order_id, subject="결제 문의"))
    assert response.status_code == 201
    body = response.json()
    assert body["data"] == {
        "id": 11,
        "status": "open",
        "order_id": order_id,
        "updated_at": "2026-10-27T00:00:00Z",
    }
    assert body["meta"]["context_id"].startswith("ctx_shop_")
    target, request = support.calls[0]
    assert (target, request.skill) == ("support", "create_inquiry")
    assert request.meta.token == "mock:shop-api"  # 요청자는 토큰에서 꺼낸다
    assert request.input["user_id"] == 1 and request.input["order_id"] == order_id
    assert request.untrusted == {"subject": "결제 문의", "message": SECRET}
    assert SECRET not in str(request.input)


def test_제목이_없으면_주문_번호로_채운다(client: TestClient, support: FakeSupport) -> None:
    order_id = _order(client)
    assert client.post("/api/shop/inquiries", json=_body(order_id)).status_code == 201
    assert support.calls[0][1].untrusted["subject"] == f"주문 {order_id} 문의"


def test_없는_주문은_404(client: TestClient, support: FakeSupport) -> None:
    response = client.post("/api/shop/inquiries", json=_body("ord_9999"))
    assert (response.status_code, response.json()["error"]["code"]) == (404, "ORDER_NOT_FOUND")
    assert support.calls == []


def test_남의_주문도_없는_주문과_같은_404(client: TestClient, support: FakeSupport) -> None:
    other = _order(client, "buy_0002")
    mine = client.post("/api/shop/inquiries", json=_body(other))
    missing = client.post("/api/shop/inquiries", json=_body("ord_9999"))
    assert mine.status_code == 404 and mine.json() == missing.json()
    assert support.calls == []


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"buyer_id": "buyer7"}, "INVALID_BUYER_ID"),
        ({"buyer_id": "buy_٠٠٠٧"}, "INVALID_BUYER_ID"),
        ({"order_id": "ORD-1"}, "INVALID_ORDER_ID"),
        ({"order_id": "ord_"}, "INVALID_ORDER_ID"),
        ({"body": "   "}, "EMPTY_BODY"),
        ({"body": "가" * 2001}, "BODY_TOO_LONG"),
        ({"subject": "가" * 101}, "SUBJECT_TOO_LONG"),
    ],
)
def test_형식_오류와_빈_본문_길이_초과는_400(
    client: TestClient, support: FakeSupport, patch: dict, code: str
) -> None:
    order_id = _order(client)
    response = client.post("/api/shop/inquiries", json=_body(order_id) | patch)
    assert (response.status_code, response.json()["error"]["code"]) == (400, code)
    assert support.calls == []


def test_주문_번호가_없거나_모르는_필드면_422(client: TestClient, support: FakeSupport) -> None:
    missing = client.post("/api/shop/inquiries", json={"buyer_id": "buy_0001", "body": "x"})
    assert missing.status_code == 422
    order_id = _order(client)
    assert client.post("/api/shop/inquiries", json=_body(order_id, amount=1)).status_code == 422
    assert support.calls == []


def test_구매자별_속도_제한(
    client: TestClient, support: FakeSupport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHOP_INQUIRY_RATE_PER_BUYER", "2")
    order_id = _order(client)
    codes = [client.post("/api/shop/inquiries", json=_body(order_id)).status_code for _ in range(3)]
    assert codes == [201, 201, 429]
    assert len(support.calls) == 2
    other = _order(client, "buy_0002")  # 다른 구매자는 영향이 없다
    ok = client.post("/api/shop/inquiries", json=_body(other, buyer_id="buy_0002"))
    assert ok.status_code == 201


def test_전체_속도_제한(
    client: TestClient, support: FakeSupport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHOP_INQUIRY_RATE_GLOBAL", "1")
    first, second = _order(client), _order(client, "buy_0002")
    assert client.post("/api/shop/inquiries", json=_body(first)).status_code == 201
    blocked = client.post("/api/shop/inquiries", json=_body(second, buyer_id="buy_0002"))
    assert (blocked.status_code, blocked.json()["error"]["code"]) == (429, "RATE_LIMITED")


def test_속도_제한은_1분이_지나면_풀린다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOP_INQUIRY_RATE_PER_BUYER", "1")
    now = [0.0]
    limiter = inquiries.InquiryRateLimiter(clock=lambda: now[0])
    limiter.hit("buy_0001")
    with pytest.raises(ShopError):
        limiter.hit("buy_0001")
    now[0] = 61.0
    limiter.hit("buy_0001")


@pytest.mark.parametrize(
    ("status", "outcome", "code", "http"),
    [
        (TaskStatus.CANCELED, "TIMEOUT", "UPSTREAM_TIMEOUT", 504),
        (TaskStatus.FAILED, "unreachable", "UPSTREAM_ERROR", 502),
        (TaskStatus.REJECTED, "FORBIDDEN_SKILL", "UPSTREAM_REJECTED", 403),
    ],
)
def test_고객지원_장애는_오류로_답하고_주문은_그대로(
    client: TestClient, support: FakeSupport, status: TaskStatus, outcome: str, code: str, http: int
) -> None:
    order_id = _order(client)
    before = client.get(f"/api/shop/orders/{order_id}").json()
    support.result = _result(status, outcome)
    response = client.post("/api/shop/inquiries", json=_body(order_id))
    assert (response.status_code, response.json()["error"]["code"]) == (http, code)
    assert client.get(f"/api/shop/orders/{order_id}").json() == before


def test_문의가_아닌_응답은_502(client: TestClient, support: FakeSupport) -> None:
    order_id = _order(client)
    support.result = _result(TaskStatus.COMPLETED, output={"message": "?"})
    response = client.post("/api/shop/inquiries", json=_body(order_id))
    assert (response.status_code, response.json()["error"]["code"]) == (502, "UPSTREAM_ERROR")


def test_목_모드에서_고객지원이_꺼져_있으면_오류(client: TestClient) -> None:
    order_id = _order(client)  # SEND를 바꾸지 않아 실제 호출이 닿지 못한다
    response = client.post("/api/shop/inquiries", json=_body(order_id))
    assert response.status_code in (502, 504)
    assert response.json()["error"]["code"] in ("UPSTREAM_ERROR", "UPSTREAM_TIMEOUT")


def test_원문은_로그와_오류에_남지_않는다(
    client: TestClient, support: FakeSupport, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    order_id = _order(client)
    ok = client.post("/api/shop/inquiries", json=_body(order_id))
    bad = client.post("/api/shop/inquiries", json=_body(order_id, subject="가" * 101))
    assert SECRET not in ok.text and SECRET not in bad.text
    assert SECRET not in caplog.text


def test_같은_접수는_같은_멱등_키이고_키에_원문이_없다(
    client: TestClient, support: FakeSupport
) -> None:
    order_id = _order(client)
    client.post("/api/shop/inquiries", json=_body(order_id))
    client.post("/api/shop/inquiries", json=_body(order_id))
    client.post("/api/shop/inquiries", json=_body(order_id, body="다른 내용"))
    keys = [request.meta.idempotency_key for _, request in support.calls]
    assert keys[0] == keys[1] != keys[2]
    assert SECRET not in keys[0] and "전화" not in keys[0]
    assert support.calls[0][1].input["customer_name"] == "데모 구매자 0001"
