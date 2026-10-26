"""impact_analysis 출력의 `affected_customer_ids`가 계약 그대로 나가는지 확인한다.

계약(`ImpactAnalysisOutput`): 가명 ID 목록, 개수 = `affected_customer_count`.
구매자 ID 형식은 shop-api 시드(`buy_{n:04d}`), contracts `IdPrefix.BUYER`(#301, `buy_` + 4자리),
고객지원 `request_incident_compensation`의 `^buy_\\d{4}$`와 같다. 형식이 틀리면 고객지원이 보상
요청 전체를 거부하므로 A2A 응답까지 확인한다.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ax_agent_template import testclient
from ax_agent_template.llm import MockLLMClient
from ax_contracts.schemas.data import ImpactAnalysisOutput
from data_agent import handlers, seed
from data_agent.main import build_app

BUYER_ID = re.compile(r"^buy_\d{4}$")
DEMO_OPENED_AT = datetime(2026, 10, 26, 3, 12, 34, tzinfo=UTC)

WINDOWS = {
    "fixed": (seed.INCIDENT_FROM, seed.INCIDENT_TO),
    "demo": (DEMO_OPENED_AT - timedelta(minutes=15), DEMO_OPENED_AT + timedelta(minutes=15)),
}


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_MOCK", "1")
    monkeypatch.setattr(handlers, "get_llm", lambda: MockLLMClient())


def test_seed_buyer_ids_use_shop_format() -> None:
    assert all(BUYER_ID.fullmatch(o.buyer_id) for o in seed.SEED_FAILED_ORDERS)


def _call_impact(tmp_path: Path, window: tuple[datetime, datetime]) -> dict:
    input_file = tmp_path / "input.json"
    input_file.write_text(
        json.dumps(
            {
                "window_from": window[0].isoformat(),
                "window_to": window[1].isoformat(),
                "scope": "payment",
            }
        ),
        encoding="utf-8",
    )
    args = testclient.build_parser().parse_args(
        ["--agent", "http://data:8106", "--skill", "impact_analysis", "--input", str(input_file)]
    )
    request = testclient.build_request(args)
    response = TestClient(build_app()).post("/a2a", json=request.model_dump(mode="json"))
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("name", sorted(WINDOWS))
def test_affected_customer_ids_follow_contract(tmp_path: Path, name: str) -> None:
    result = _call_impact(tmp_path, WINDOWS[name])
    assert result["status"] == "completed"
    output = result["output"]
    ids = output["affected_customer_ids"]

    assert ids == ["buy_0001", "buy_0002", "buy_0003", "buy_0004"]
    assert all(BUYER_ID.fullmatch(i) for i in ids)
    assert ids == sorted(set(ids))  # 정렬, 중복 없음
    assert len(ids) == output["affected_customer_count"] == seed.EXPECTED_AFFECTED_CUSTOMER_COUNT

    parsed = ImpactAnalysisOutput.model_validate(output)  # 계약 스키마로 다시 읽힌다
    assert parsed.model_dump(mode="json") == output
