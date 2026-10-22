import pytest
from pydantic import ValidationError

from ax_contracts.schemas import (
    AnswerHandoverQuestionInput,
    CheckPlanInput,
    CheckPlanOutput,
    ConfirmHandoverOutput,
    ConfirmOffboardingOutput,
    ConfirmReassignmentOutput,
    GetRosterOutput,
    HandoverAnswerRequest,
    HandoverQuestion,
    InProgressItem,
    ListInProgressInput,
    ListInProgressOutput,
    RecordCase,
    RecordCaseOutput,
    RecordKind,
    RequestAssignmentChangeInput,
    RequestAssignmentChangeOutput,
    StartHandoverInput,
    StartHandoverOutput,
    StartOnboardingOutput,
    VerifyHandoverInput,
    VerifyHandoverOutput,
)

T0 = "2026-10-22T05:00:00Z"
T1 = "2026-10-22T05:40:00Z"

QUESTION = {
    "question_id": "qst_1",
    "context_id": "ctx_hnd_1",
    "assignee_id": "emp_7",
    "question": "롤백 기준 문서는 어디에 있나요?",
    "gap_field": "rollback_criteria",
}


def test_decision_rejected_is_a_record_kind() -> None:
    assert RecordKind("decision_rejected") is RecordKind.DECISION_REJECTED
    case = RecordCase.model_validate(
        {
            "context_id": "ctx_req_9",
            "kind": "decision_rejected",
            "summary": "결제 서버 캐시 도입안을 기각했다.",
            "decision_reason": "결제 정합성 위험",
            "alternatives_considered": ["캐시 도입", "현행 유지"],
            "source_refs": ["req:88"],
            "author": "planning",
        }
    )
    assert case.kind is RecordKind.DECISION_REJECTED


def test_record_case_output_outcomes() -> None:
    created = RecordCaseOutput.model_validate(
        {"outcome": "card_created", "card_id": "card_1", "confidence": "high"}
    )
    assert created.card_id == "card_1"
    assert RecordCaseOutput(outcome="record_only").card_id is None
    with pytest.raises(ValidationError):
        RecordCaseOutput.model_validate({"outcome": "saved"})
    with pytest.raises(ValidationError):  # 확신도는 low·mid·high만 (퍼센트 금지)
        RecordCaseOutput.model_validate({"outcome": "candidate", "confidence": "0.4"})


def test_list_in_progress_and_verify_handover() -> None:
    assert ListInProgressInput.model_validate({"owner_id": "emp_7"}).area is None
    item = {
        "item_id": "dep_1",
        "kind": "deployment",
        "title": "shop-api v1.4.0 롤아웃",
        "status": "rolling_out",
        "owner_id": "emp_7",
    }
    assert InProgressItem.model_validate(item).kind == "deployment"
    listed = ListInProgressOutput.model_validate({"items": [item], "collected_at": T0})
    assert listed.items[0].area is None
    with pytest.raises(ValidationError):  # 조회 시각은 근거라서 필수
        ListInProgressOutput.model_validate({"items": []})
    with pytest.raises(ValidationError):
        InProgressItem.model_validate({**item, "kind": "notice"})  # 공지는 1차에서 뺐다
    ask = VerifyHandoverInput.model_validate(
        {"handover_id": "hnd_1", "owner_id": "emp_7", "handover_item_ids": ["dep_1"]}
    )
    assert ask.handover_item_ids == ["dep_1"]
    ok = VerifyHandoverOutput.model_validate(
        {"approved": True, "reason_code": "complete", "checked_at": T1}
    )
    assert ok.missing == []
    bad = VerifyHandoverOutput.model_validate(
        {"approved": False, "reason_code": "missing_items", "missing": [item], "checked_at": T1}
    )
    assert bad.missing[0].item_id == "dep_1"
    with pytest.raises(ValidationError):
        VerifyHandoverOutput.model_validate(
            {"approved": False, "reason_code": "other", "checked_at": T1}
        )


def test_handover_flow_models() -> None:
    start = StartHandoverInput.model_validate({"owner_id": "emp_7", "reason": "shift"})
    assert start.successor_id is None
    draft = StartHandoverOutput.model_validate(
        {
            "handover_id": "hnd_1",
            "status": "input_required",
            "items": [{"item_id": "i1", "kind": "ticket", "title": "결제 개선 티켓"}],
            "completeness": 60,
            "gap_count": 2,
            "questions": [QUESTION],
        }
    )
    assert draft.questions[0].gap_field == "rollback_criteria"
    assert HandoverQuestion.model_validate(QUESTION).assignee_id == "emp_7"
    assert AnswerHandoverQuestionInput(question_id="qst_1", answer="위키 결제 롤백 문서").answer
    body = HandoverAnswerRequest(question_id="qst_1", answer="답", idempotency_key="k1")
    assert body.idempotency_key == "k1"
    with pytest.raises(ValidationError):  # 완성도는 0~100
        StartHandoverOutput.model_validate(
            {"handover_id": "h", "status": "draft", "completeness": 120, "gap_count": 0}
        )
    held = ConfirmHandoverOutput.model_validate(
        {"handover_id": "hnd_1", "status": "held", "completeness": 55, "hold_id": "hold_1"}
    )
    assert held.hold_id == "hold_1"
    with pytest.raises(ValidationError):
        ConfirmHandoverOutput.model_validate(
            {"handover_id": "hnd_1", "status": "done", "completeness": 100}
        )


def test_assignment_change_models() -> None:
    req = RequestAssignmentChangeInput.model_validate(
        {"change_kind": "offboarding", "member_id": "emp_7", "area": "payment"}
    )
    assert req.successor_id is None
    out = RequestAssignmentChangeOutput.model_validate(
        {
            "change_id": "chg_1",
            "checklist": [{"item_id": "c1", "title": "권한 목록 전달"}],
            "successor_candidates": ["emp_9"],
            "handover_id": "hnd_1",
        }
    )
    assert out.checklist[0].done is False
    with pytest.raises(ValidationError):
        RequestAssignmentChangeInput.model_validate(
            {"change_kind": "promotion", "member_id": "emp_7", "area": "payment"}
        )
    rejected = ConfirmReassignmentOutput.model_validate(
        {"change_id": "chg_1", "status": "rejected", "gap_areas": ["payment"]}
    )
    assert rejected.gap_areas == ["payment"]
    assert ConfirmOffboardingOutput(change_id="chg_1", status="held", hold_id="h1").hold_id == "h1"


def test_onboarding_and_roster_models() -> None:
    onboarding = StartOnboardingOutput.model_validate(
        {
            "onboarding_id": "onb_1",
            "steps": [
                {"step_id": "s1", "order": 1, "title": "결제 영역 결정 카드", "card_id": "c1"}
            ],
            "progress": 0,
        }
    )
    assert onboarding.steps[0].done is False
    with pytest.raises(ValidationError):  # 읽기 순서는 1부터
        StartOnboardingOutput.model_validate(
            {
                "onboarding_id": "o",
                "steps": [{"step_id": "s", "order": 0, "title": "t"}],
                "progress": 0,
            }
        )
    roster = GetRosterOutput.model_validate(
        {
            "entries": [{"area": "payment", "role": "owner", "member_id": "emp_7"}],
            "gap_areas": ["search"],
            "collected_at": T0,
        }
    )
    assert roster.gap_areas == ["search"]


def test_check_plan_models() -> None:
    ask = CheckPlanInput.model_validate(
        {
            "draft": {
                "title": "결제 서버 캐시 도입",
                "summary": "응답 속도 개선",
                "area": "payment",
                "kind": "spec",
            }
        }
    )
    assert ask.draft.area == "payment"
    warn = CheckPlanOutput.model_validate(
        {
            "verdict": "warn",
            "findings": [
                {
                    "code": "REJECTED_BEFORE",
                    "level": "warn",
                    "message": "3주 전에 기각된 안입니다. 기각 이유: 결제 정합성 위험.",
                    "card_ids": ["card_97"],
                }
            ],
            "similar_cards": [
                {"card_id": "card_97", "summary": "결제 서버 캐시 도입 기각", "status": "active"}
            ],
            "computed": {"days_since_rejected": 21, "basis": "결정 카드의 decided_at"},
        }
    )
    assert warn.findings[0].card_ids == ["card_97"]
    empty = CheckPlanOutput.model_validate({"verdict": "ok", "no_related_cards": True})
    assert empty.findings == [] and empty.computed.days_since_rejected is None
    with pytest.raises(ValidationError):
        CheckPlanOutput.model_validate({"verdict": "maybe"})
    with pytest.raises(ValidationError):
        CheckPlanOutput.model_validate(
            {"verdict": "warn", "findings": [{"code": "BAD", "level": "warn", "message": "m"}]}
        )
    with pytest.raises(ValidationError):  # 숫자는 음수가 될 수 없다
        CheckPlanOutput.model_validate({"verdict": "ok", "computed": {"days_since_rejected": -1}})


def test_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        StartHandoverInput.model_validate({"owner_id": "emp_7", "salary": 1})  # 개인 정보 필드 없음
