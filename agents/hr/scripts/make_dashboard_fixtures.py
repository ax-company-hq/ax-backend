"""대시보드 인사 화면 개발용 응답 샘플(MSW 픽스처)을 만든다.

실제 hr 서비스를 가짜 시계(`ManualClock`)로 돌려 얻은 출력이라 계약 모델과 정확히 같은 모양이다.
같은 입력이면 같은 파일이 나온다(결정적).

실행: uv run python agents/hr/scripts/make_dashboard_fixtures.py [출력 폴더]
기본 출력: docs/notes/hr/dashboard-fixtures/
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

os.environ.setdefault("AGENT_TEMPLATE_MOCK", "1")
os.environ.setdefault("HR_MOCK", "1")

from fastapi.testclient import TestClient  # noqa: E402

from hr_agent.config import HrSettings, ManualClock  # noqa: E402
from hr_agent.main import app  # noqa: E402
from hr_agent.services import build_services, set_services  # noqa: E402

REPO = Path(__file__).resolve().parents[3]


def envelope(data: Any, total: int | None = None) -> dict[str, Any]:
    """dashboard-api 응답 형식: 성공은 `{data, meta}` (`ax_dashboard_api.envelope.ok`와 같다)."""
    meta: dict[str, Any] = {"tenant_id": "shop-demo"}
    if total is not None:
        meta["total"] = total
    return {"data": data, "meta": meta}


def main(out_dir: Path) -> None:
    svc = build_services(HrSettings(mock=True), clock=ManualClock())
    set_services(svc)
    client = TestClient(app)

    def call(
        skill: str, inp: dict[str, Any], key: str, ctx: str, who: str = "orchestrator"
    ) -> dict:
        raw = {
            "meta": {
                "context_id": ctx,
                "token": f"mock:{who}",
                "path": [],
                "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
                "idempotency_key": key,
            },
            "skill": skill,
            "input": inp,
        }
        return client.post("/a2a", json=raw).json()

    # ---- 이야기: 결제 영역 인계 중, 후보 카드 하나, 온보딩 진행 중, 퇴직 요청 하나 ---------------
    call(
        "record_case",
        {
            "context_id": "ctx_dec_0001",
            "kind": "spec_confirmed",
            "summary": "결제 재시도를 최대 2회로 확정했다",
            "decision_reason": "중복 결제 위험을 줄이기 위해",
            "alternatives_considered": ["재시도 없음"],
            "source_refs": ["pr:412"],
            "area": "payment",
            "author": "planning",
        },
        "r1",
        "ctx_dec_0001",
        "planning",
    )
    call(
        "record_case",
        {
            "context_id": "ctx_dec_0002",
            "kind": "decision_rejected",
            "summary": "결제 서버 캐시 도입안을 기각했다",
            "decision_reason": "결제 정합성 위험",
            "alternatives_considered": ["현행 유지"],
            "source_refs": ["req:88"],
            "area": "payment",
            "author": "planning",
        },
        "r2",
        "ctx_dec_0002",
        "planning",
    )
    call(
        "record_case",
        {
            "context_id": "ctx_dec_0003",
            "kind": "decision",
            "summary": "야간 알림 기준을 낮추기로 했다",
            "area": "search",
            "author": "support",
        },
        "r3",
        "ctx_dec_0003",
        "support",
    )
    started = call(
        "start_handover",
        {"owner_id": "emp_01", "successor_id": "emp_02", "reason": "transfer"},
        "h1",
        "ctx_hnd_0001",
    )
    call(
        "answer_handover_question",
        {
            "question_id": started["output"]["questions"][0]["question_id"],
            "answer": "오류율 5% 초과 시 직전 버전으로 되돌린다",
        },
        "a1",
        "ctx_hnd_0001",
        "dashboard-api",
    )
    svc.clock.advance(minutes=5)

    def read(skill: str, inp: dict[str, Any] | None = None) -> dict[str, Any]:
        return call(skill, inp or {}, "read", "ctx_req_dash0001", "dashboard-api")["output"]

    handovers = read("list_handovers")
    questions = read("list_open_questions")
    roster = read("get_roster")
    candidates = read("list_candidate_cards")
    audit = read("get_audit_timeline", {"context_id": "ctx_hnd_0001"})
    plan = read(
        "check_plan",
        {
            "draft": {
                "title": "결제 서버 캐시 도입",
                "summary": "결제 응답 속도를 위해 서버 캐시를 도입한다",
                "area": "payment",
                "kind": "spec",
            }
        },
    )
    approve = call(
        "decide_candidate_card",
        {"card_id": candidates["cards"][0]["card_id"], "decision": "approve", "note": "확인함"},
        "d1",
        "ctx_req_dash0002",
        "dashboard-api",
    )["output"]
    answer = call(
        "answer_handover_question",
        {
            "question_id": questions["questions"][0]["question_id"],
            "answer": "야간 배치와 겹치면 타임아웃이 늘 수 있다",
        },
        "a2",
        "ctx_hnd_0001",
        "dashboard-api",
    )["output"]

    files = {
        "GET_handovers": envelope(handovers["handovers"], len(handovers["handovers"])),
        "GET_handover-questions": envelope(questions["questions"], len(questions["questions"])),
        "POST_handover-questions_answer": envelope(answer),
        "GET_roster": envelope(roster),
        "GET_candidate-cards": envelope(candidates["cards"], len(candidates["cards"])),
        "POST_candidate-cards_decision": envelope(approve),
        "GET_hr-activity": envelope(audit["entries"], len(audit["entries"])),
        "POST_plan-checks": envelope(plan),
    }
    errors = {
        "422_INVALID_INPUT": {
            "error": {"code": "INVALID_INPUT", "message": "입력 형식이 맞지 않는다"}
        },
        "404_NOT_FOUND": {"error": {"code": "NOT_FOUND", "message": "찾을 수 없다"}},
        "409_CONFLICT": {
            "error": {"code": "CONFLICT", "message": "같은 사건에서 이미 다른 후임으로 요청했다"}
        },
        "409_NOT_EXECUTED": {
            "error": {
                "code": "NOT_EXECUTED",
                "message": "실행되지 않았다 (held). 승인 대기 또는 거부 상태",
            }
        },
        "502_UPSTREAM_ERROR": {
            "error": {
                "code": "UPSTREAM_ERROR",
                "message": "hr이 다른 서비스 문제로 처리하지 못했다",
            }
        },
        "504_UPSTREAM_TIMEOUT": {
            "error": {"code": "UPSTREAM_TIMEOUT", "message": "hr이 제때 응답하지 않았다"}
        },
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in {**files, **{f"ERROR_{k}": v for k, v in errors.items()}}.items():
        (out_dir / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(f"{len(files) + len(errors)} files -> {out_dir}")
    set_services(None)
    svc.store.close()


if __name__ == "__main__":
    target = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else REPO / "docs" / "notes" / "hr" / "dashboard-fixtures"
    )
    main(target)
