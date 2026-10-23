"""실제 연동 뼈대: 가짜 상대 서버로 호출 형식과 오류 처리를 확인한다(상대가 없어도 돌아간다)."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import Card, ListInProgressInput, VerifyHandoverInput
from hr_agent.clients.errors import UpstreamError, bind_meta
from hr_agent.clients.real import A2APlanningClient, HttpMemoryClient
from hr_agent.main import app

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


def make_card(card_id: str, area: str = "payment", **extra: Any) -> Card:
    return Card(
        card_id=card_id,
        kind="decision",
        title=f"[결정] {card_id}",
        body="결정: 테스트",
        context_id="ctx_x_0001",
        author="planning",
        confidence="high",
        created_at=NOW,
        extra={"area": area, "decision": "테스트", **extra},
    )


class FakeMemory:
    """가정한 memory REST를 흉내 내는 서버."""

    def __init__(self) -> None:
        self.cards: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.fail_with: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"error": "x"})
        path, params = request.url.path, dict(request.url.params)
        if request.method == "POST" and path == "/api/memory/cards":
            self.cards[body["card_id"]] = body
            return httpx.Response(200, json={"data": {"card_id": body["card_id"]}})
        if request.method == "GET" and path == "/api/memory/cards/search":
            area = params.get("area")
            hits = [
                {"card": c, "score": 0.9}
                for c in self.cards.values()
                if area is None or c["extra"].get("area") == area
            ]
            return httpx.Response(200, json={"data": hits})
        if request.method == "GET" and path == "/api/memory/cards":
            area = params.get("area")
            items = [
                c for c in self.cards.values() if area is None or c["extra"].get("area") == area
            ]
            return httpx.Response(200, json={"data": items})
        if request.method == "GET" and path.startswith("/api/memory/cards/"):
            card = self.cards.get(path.rsplit("/", 1)[1])
            return httpx.Response(404) if card is None else httpx.Response(200, json={"data": card})
        if request.method == "PATCH" and path.endswith("/status"):
            card = self.cards[path.split("/")[-2]]
            card["status"], card["superseded_by"] = body["status"], body["superseded_by"]
            return httpx.Response(200, json={"data": {"ok": True}})
        if request.method == "PATCH" and path.endswith("/candidate"):
            self.cards[path.split("/")[-2]]["extra"]["candidate"] = body["candidate"]
            return httpx.Response(200, json={"data": {"ok": True}})
        return httpx.Response(404)


@pytest.fixture
def fake_memory() -> FakeMemory:
    return FakeMemory()


@pytest.fixture
def memory(fake_memory: FakeMemory) -> HttpMemoryClient:
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake_memory.handler))
    return HttpMemoryClient("http://memory.test", http_client=client)


# ---- memory --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_memory_client_round_trip(memory: HttpMemoryClient, fake_memory: FakeMemory) -> None:
    assert await memory.write_card(make_card("card_a")) == "card_a"
    await memory.write_card(make_card("card_b", area="search"))
    assert (await memory.get_card("card_a")).card_id == "card_a"
    assert await memory.get_card("card_zzz") is None
    assert [c.card_id for c in await memory.list_cards(area="search")] == ["card_b"]
    hits = await memory.search_cards("결제", area="payment", states=["active"], limit=3)
    assert [(h.card.card_id, h.score) for h in hits] == [("card_a", 0.9)]
    await memory.set_status("card_a", "superseded", "card_b")
    await memory.set_candidate("card_b", True)
    assert fake_memory.cards["card_a"]["status"] == "superseded"
    assert fake_memory.cards["card_b"]["extra"]["candidate"] is True
    search = next(r for r in fake_memory.requests if r[1].endswith("/search"))
    assert search[0] == "GET"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 503, 403])
async def test_memory_errors_are_raised_not_swallowed(
    memory: HttpMemoryClient, fake_memory: FakeMemory, status: int
) -> None:
    fake_memory.fail_with = status
    with pytest.raises(UpstreamError):
        await memory.write_card(make_card("card_a"))
    with pytest.raises(UpstreamError):
        await memory.search_cards("x")


@pytest.mark.asyncio
async def test_memory_bad_payloads_are_rejected() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"data": [{"card": {"nope": 1}, "score": 1}]})
        if request.method == "POST":
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(200, text="not json")

    client = httpx.AsyncClient(transport=httpx.MockTransport(broken))
    memory = HttpMemoryClient("http://memory.test", http_client=client)
    with pytest.raises(UpstreamError):
        await memory.search_cards("x")
    with pytest.raises(UpstreamError):
        await memory.write_card(make_card("card_a"))  # card_id 없는 응답
    with pytest.raises(UpstreamError):
        await memory.list_cards()


@pytest.mark.asyncio
async def test_memory_unreachable_is_an_upstream_error() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(down))
    with pytest.raises(UpstreamError):
        await HttpMemoryClient("http://memory.test", http_client=client).list_cards()


# ---- planning ------------------------------------------------------------------------
class FakePlanning:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.reply: dict[str, Any] | None = None
        self.status = "completed"

    def handler(self, request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        self.requests.append({"url": str(request.url), **sent})
        out = self.reply
        if out is None:
            out = (
                {"items": [], "collected_at": NOW.isoformat()}
                if sent["skill"] == "list_in_progress"
                else {
                    "approved": True,
                    "reason_code": "complete",
                    "missing": [],
                    "stale_item_ids": [],
                    "checked_at": NOW.isoformat(),
                }
            )
        return httpx.Response(
            200, json={"meta": sent["meta"], "status": self.status, "output": out, "evidence": {}}
        )


def bound_meta(context_id: str = "ctx_pl_0001", path: list[str] | None = None) -> None:
    from ax_contracts.schemas import RequestMeta

    bind_meta(
        RequestMeta(
            context_id=context_id,
            token="mock:orchestrator",
            path=path or ["orchestrator"],
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        )
    )


@pytest.mark.asyncio
async def test_planning_client_sends_a_contract_shaped_a2a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ax_agent_template import config as template_config

    # 같은 프로세스에서 다른 서비스 앱이 먼저 이름을 정했을 수 있어 hr로 고정한다
    monkeypatch.setattr(template_config, "_agent_name", "hr")
    fake = FakePlanning()
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    planning = A2APlanningClient("http://planning.test", http_client=http)
    bound_meta()
    out = await planning.list_in_progress(ListInProgressInput(owner_id="emp_01", area="payment"))
    assert out.items == []
    sent = fake.requests[0]
    assert sent["url"] == "http://planning.test/a2a"
    assert sent["skill"] == "list_in_progress"
    assert sent["input"] == {"owner_id": "emp_01", "area": "payment"}
    assert sent["meta"]["context_id"] == "ctx_pl_0001"
    assert sent["meta"]["path"] == ["orchestrator", "hr"]  # 자기 이름이 붙어서 나간다
    assert sent["meta"]["token"].startswith("mock:hr")
    verdict = await planning.verify_handover(
        VerifyHandoverInput(
            handover_id="hnd_1", owner_id="emp_01", area="payment", handover_item_ids=["pr_1"]
        )
    )
    assert verdict.approved is True and fake.requests[1]["skill"] == "verify_handover"


@pytest.mark.asyncio
async def test_planning_errors_and_odd_replies_are_upstream_errors() -> None:
    fake = FakePlanning()
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    planning = A2APlanningClient("http://planning.test", http_client=http)
    bound_meta()
    fake.status = "input-required"
    with pytest.raises(UpstreamError):
        await planning.list_in_progress(ListInProgressInput(owner_id="emp_01"))
    fake.status = "completed"
    fake.reply = {"unexpected": True}
    with pytest.raises(UpstreamError):
        await planning.list_in_progress(ListInProgressInput(owner_id="emp_01"))
    bind_meta(None)
    with pytest.raises(UpstreamError):
        await planning.list_in_progress(ListInProgressInput(owner_id="emp_01"))


# ---- hr 전체가 실제 연동 클라이언트 위에서 돈다 ------------------------------------------
def test_hr_runs_on_the_real_clients_against_fake_servers(svc, fake_memory) -> None:
    from hr_agent.services import set_services

    memory_http = httpx.AsyncClient(transport=httpx.MockTransport(fake_memory.handler))
    planning_fake = FakePlanning()
    planning_fake.reply = {
        "items": [
            {
                "item_id": "pr_9",
                "kind": "pr",
                "title": "결제 PR",
                "status": "open",
                "owner_id": "emp_01",
                "area": "payment",
            }
        ],
        "collected_at": NOW.isoformat(),
    }
    planning_http = httpx.AsyncClient(transport=httpx.MockTransport(planning_fake.handler))
    real = replace(
        svc,
        memory=HttpMemoryClient("http://memory.test", http_client=memory_http),
        planning=A2APlanningClient("http://planning.test", http_client=planning_http),
    )
    set_services(real)
    client = TestClient(app)

    def call(skill: str, inp: dict, key: str, token: str = "mock:orchestrator") -> dict:
        raw = {
            "meta": {
                "context_id": "ctx_real_01",
                "token": token,
                "path": [],
                "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
                "idempotency_key": key,
            },
            "skill": skill,
            "input": inp,
        }
        return client.post("/a2a", json=raw).json()

    rec = {
        "context_id": "ctx_real_01",
        "kind": "decision_rejected",
        "summary": "결제 서버 캐시 도입안을 기각했다",
        "decision_reason": "정합성 위험",
        "alternatives_considered": ["현행 유지"],
        "source_refs": ["req:1", "area:payment"],
        "author": "planning",
    }
    assert call("record_case", rec, "r", "mock:planning")["outcome"] == "card_created"
    assert len(fake_memory.cards) == 1  # 카드가 hr 로컬이 아니라 memory에 저장됐다
    assert svc.store.all("SELECT * FROM mock_cards") == []

    started = call("start_handover", {"owner_id": "emp_01"}, "s")
    assert {i["item_id"] for i in started["output"]["items"]} >= {"pr_9"}
    assert planning_fake.requests[0]["meta"]["context_id"] == "ctx_real_01"

    draft = {
        "title": "결제 서버 캐시 도입",
        "summary": "서버 캐시",
        "area": "payment",
        "kind": "spec",
    }
    plan = call("check_plan", {"draft": draft}, "p", "mock:planning")
    assert plan["output"]["verdict"] == "warn"


def test_an_upstream_failure_fails_closed_without_changing_state(svc, fake_memory) -> None:
    from hr_agent.services import set_services

    fake_memory.fail_with = 503
    memory_http = httpx.AsyncClient(transport=httpx.MockTransport(fake_memory.handler))
    set_services(
        replace(svc, memory=HttpMemoryClient("http://memory.test", http_client=memory_http))
    )
    client = TestClient(app)
    raw = {
        "meta": {
            "context_id": "ctx_real_02",
            "token": "mock:planning",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": "r",
        },
        "skill": "check_plan",
        "input": {"draft": {"title": "t", "summary": "s", "area": "payment", "kind": "spec"}},
    }
    res = client.post("/a2a", json=raw).json()
    assert (res["status"], res["outcome"]) == ("failed", "UPSTREAM_UNAVAILABLE")
    assert "memory" in res["output"]["message"] and "503" not in res["output"]["message"]
    assert svc.gate.calls == []


def test_settings_pick_the_real_clients_only_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    from hr_agent.config import HrSettings
    from hr_agent.services import build_services

    default = build_services(HrSettings.from_env({"HR_MOCK": "1"}))
    assert type(default.memory).__name__ == "MockMemoryClient"
    assert type(default.planning).__name__ == "MockPlanningClient"
    real = build_services(
        HrSettings.from_env({"HR_MOCK": "1", "HR_PLANNING_MODE": "a2a", "HR_MEMORY_MODE": "http"})
    )
    assert type(real.memory).__name__ == "HttpMemoryClient"
    assert type(real.planning).__name__ == "A2APlanningClient"


def test_an_unreachable_safety_layer_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from ax_agent_template import execute as template_execute
    from hr_agent.config import HrSettings, ManualClock
    from hr_agent.roster.seed import seed_roster
    from hr_agent.services import build_services, set_services

    async def down(_request):
        raise NotImplementedError("safety 서비스 호출")

    monkeypatch.setattr(template_execute, "_send_to_safety", down)
    svc = build_services(HrSettings(mock=False), clock=ManualClock())
    seed_roster(svc.store, svc.clock)  # 실제 모드는 시드를 넣지 않으므로 명부만 직접 채운다
    set_services(svc)
    assert type(svc.gate).__name__ == "TemplateGate"
    before = svc.store.all("SELECT * FROM handovers")
    raw = {
        "meta": {
            "context_id": "ctx_real_03",
            "token": "mock:orchestrator",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": "s",
        },
        "skill": "start_handover",
        "input": {"owner_id": "emp_01"},
    }
    res = TestClient(app).post("/a2a", json=raw)
    set_services(None)
    assert res.status_code == 200
    body = res.json()
    # 템플릿 execute()가 무응답을 거부(fail-closed)로 바꿔 주고, hr은 그 결과를 그대로 따른다
    assert body["outcome"] == "gate_rejected" and body["output"]["status"] == "rejected"
    assert svc.store.all("SELECT * FROM handovers") == before
