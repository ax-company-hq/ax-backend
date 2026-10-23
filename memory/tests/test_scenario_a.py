"""시나리오 A 단독 테스트: 통합 에이전트 없이 A2A 요청만으로 사건 카드 저장 → 유사 사건 검색.

기획서 5장 시나리오 A 6단계("인사 — 사건 기록을 회사 기억에 저장")와 9장 7단계
("2차 장애에서 회사 기억 참조")를 memory 쪽에서 증명한다. 실제 `agent-card.json`과 실제 앱
(`ax_memory.main`)을 쓰고, 요청은 agent-template 테스트 클라이언트(`ax-test-call`) 형식으로
보낸다. 저장소는 메모리, 임베더는 목, safety는 목 모드(A 등급 즉시 executed)다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ax_agent_template import testclient
from ax_contracts.schemas.record import CardDraft, RecordCase, RecordKind
from ax_memory import handlers
from ax_memory.main import app

INCIDENT_CTX = "ctx_inc_scn_a_0001"

# 시나리오 A가 끝난 뒤 인사(record_case)가 받는 사건 기록
RECORD = RecordCase(
    context_id=INCIDENT_CTX,
    kind=RecordKind.INCIDENT,
    summary=(
        "결제 API 응답 지연으로 결제 실패 급증. 최근 배포 롤백으로 복구, 영향 고객 보상 쿠폰 지급"
    ),
    decision_reason="원인 배포를 롤백하고 기준 이하 보상은 자동 지급",
    alternatives_considered=["PG사 전환", "주문 일시 중단"],
    source_refs=["exe_rollback_0001", "ntc_0001"],
    author="hr",
    area="payment",
)


def incident_card(record: RecordCase) -> dict:
    """인사가 기록을 카드로 바꿔 `memory_write_card`에 넣는 입력. 카드 문장은 hr이 만든다."""
    draft = CardDraft(
        kind="incident",
        context_id=record.context_id,
        title="결제 API 장애 — 배포 롤백으로 복구",
        body=f"{record.summary}\n결정 이유: {record.decision_reason}",
        tags=["payment", "rollback", "결제", "장애"],
        source_refs=record.source_refs,
        author=record.author,
        confidence="high",
        extra={"alternatives_considered": record.alternatives_considered},
    )
    return {"card": draft.model_dump(mode="json")}


NOISE = [
    (
        "fraud",
        "ctx_req_seller_0001",
        "신규 판매자 사기 의심 — 정산 보류",
        "판매자 서류 불일치, 정산 보류",
    ),
    ("notice", "ctx_ntc_0001", "추석 배송 지연 안내", "택배사 물량 증가로 배송 지연 공지"),
]


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_MOCK", "1")  # 목 임베더, 토큰은 mock:<이름>
    monkeypatch.delenv("MEMORY_PG_DSN", raising=False)
    monkeypatch.delenv("AX_SAFETY_URL", raising=False)
    handlers.reset_state()
    yield
    handlers.reset_state()


@pytest.fixture
def agent() -> TestClient:
    return TestClient(app)


def _call(agent: TestClient, tmp_path: Path, skill: str, data: dict, *argv: str) -> dict:
    """`ax-test-call --agent ... --skill ... --input ...`과 같은 요청을 보낸다."""
    input_file = tmp_path / f"{skill}.json"
    input_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    args = testclient.build_parser().parse_args(
        ["--agent", "http://memory:8002", "--skill", skill, "--input", str(input_file), *argv]
    )
    request = testclient.build_request(args)
    response = agent.post("/a2a", json=request.model_dump(mode="json"))
    assert response.status_code == 200
    return response.json()


def _write_as_hr(agent: TestClient, tmp_path: Path, data: dict, ctx: str, key: str) -> dict:
    return _call(
        agent, tmp_path, "card_write", data,
        "--as", "hr", "--context-id", ctx, "--idempotency-key", key,
    )  # fmt: skip


def test_agent_card_declares_grades(agent: TestClient) -> None:
    card = agent.get("/.well-known/agent-card.json").json()
    skills = {skill["id"]: skill for skill in card["skills"]}
    assert card["name"] == "memory"
    assert skills["card_write"]["grade"] == "A"  # memory_write_card
    assert skills["card_search"]["grade"] == "read"  # memory_search_card
    assert "hr" in skills["card_write"]["allowed_requesters"]
    assert "memory_write_card" in skills["card_write"]["description"]
    assert "memory_search_card" in skills["card_search"]["description"]


def test_incident_card_saved_then_found_for_second_incident(
    agent: TestClient, tmp_path: Path
) -> None:
    """사건 카드 저장 → 2차 장애 설명으로 검색하면 1차 사건 카드가 맨 위에 나온다."""
    saved = _write_as_hr(agent, tmp_path, incident_card(RECORD), INCIDENT_CTX, "rec-scn-a-1")
    assert saved["status"] == "completed"
    assert saved["meta"]["requester"] == "hr"
    card_id = saved["output"]["card_id"]
    assert card_id.startswith("card_")
    assert saved["output"]["created"] is True
    assert card_id in saved["evidence"]["ref_ids"]

    for i, (kind, ctx, title, body) in enumerate(NOISE):
        noise = {"card": {"kind": kind, "context_id": ctx, "title": title, "body": body,
                          "author": "hr", "confidence": "high"}}  # fmt: skip
        assert _write_as_hr(agent, tmp_path, noise, ctx, f"noise-{i}")["status"] == "completed"

    # 2차 장애: 통합 에이전트가 아니라 고객지원이 직접 비슷한 사건을 찾는다
    query = {"query": "결제 API 응답 지연 결제 실패 급증 배포", "kind": ["incident"], "top_k": 3}
    found = _call(
        agent,
        tmp_path,
        "card_search",
        query,
        "--as",
        "support",
        "--context-id",
        "ctx_inc_scn_a_0002",
    )
    assert found["status"] == "completed"
    hits = found["output"]["hits"]
    assert hits, "1차 사건 카드가 검색되어야 한다"
    top = hits[0]
    assert top["card"]["card_id"] == card_id
    assert top["card"]["context_id"] == INCIDENT_CTX
    assert top["card"]["source_refs"] == RECORD.source_refs
    assert top["low_confidence"] is False
    assert 0 < top["score"] <= 1
    assert all(hit["card"]["kind"] == "incident" for hit in hits)
    assert found["evidence"]["ref_ids"][0] == card_id  # 참조한 카드가 근거로 남는다

    # kind 필터 없이도 의미가 가까운 1차 사건 카드가 다른 카드(사기·공지)보다 위에 온다
    unfiltered = _call(
        agent, tmp_path, "card_search", {"query": query["query"], "top_k": 3},
        "--as", "support", "--context-id", "ctx_inc_scn_a_0002",
    )  # fmt: skip
    ranked = unfiltered["output"]["hits"]
    assert len(ranked) == 1 + len(NOISE)
    assert ranked[0]["card"]["card_id"] == card_id
    assert ranked[0]["score"] > ranked[1]["score"]

    got = _call(agent, tmp_path, "card_get", {"card_id": card_id}, "--as", "planning")
    assert got["status"] == "completed"
    assert got["output"]["card"]["title"] == "결제 API 장애 — 배포 롤백으로 복구"


def test_same_record_twice_does_not_duplicate(agent: TestClient, tmp_path: Path) -> None:
    first = _write_as_hr(agent, tmp_path, incident_card(RECORD), INCIDENT_CTX, "rec-scn-a-1")
    again = _write_as_hr(agent, tmp_path, incident_card(RECORD), INCIDENT_CTX, "rec-scn-a-2")
    assert again["status"] == "completed"
    assert again["output"] == {"card_id": first["output"]["card_id"], "created": False}


def test_write_needs_idempotency_key(agent: TestClient, tmp_path: Path) -> None:
    result = _call(agent, tmp_path, "card_write", incident_card(RECORD), "--as", "hr")
    assert result["status"] == "failed"
    assert result["outcome"] == "invalid_input"


def test_requester_outside_acl_cannot_write(agent: TestClient, tmp_path: Path) -> None:
    result = _call(
        agent, tmp_path, "card_write", incident_card(RECORD),
        "--as", "dashboard-api", "--idempotency-key", "rec-x",
    )  # fmt: skip
    assert result["status"] == "rejected"
    assert result["outcome"] == "FORBIDDEN_SKILL"
