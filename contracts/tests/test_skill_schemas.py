import pytest
from pydantic import ValidationError

from ax_contracts.schemas import (
    DiagnoseIncidentInput,
    DiagnoseIncidentOutput,
    FindRelatedPrsInput,
    FindRelatedPrsOutput,
    LinkIncidentInput,
    LinkIncidentOutput,
    RelatedPr,
    RollbackDeploymentParams,
    SuspectDeployment,
    VerifyRecoveryInput,
    VerifyRecoveryOutput,
    VerifyRollbackTargetInput,
    VerifyRollbackTargetOutput,
)

T0 = "2026-10-22T05:00:00Z"
T1 = "2026-10-22T05:40:00Z"

PR = {
    "pr_number": 42,
    "title": "결제 타임아웃 조정",
    "merged_at": T0,
    "spec_id": "spec_pay",
    "para_id": "R1",
    "changed_paths": ["shop-api/payment.py"],
}


def test_find_related_prs_and_link_incident() -> None:
    query = FindRelatedPrsInput.model_validate({"window_from": T0, "window_to": T1})
    assert query.component is None
    related = FindRelatedPrsOutput.model_validate({"candidates": [PR]})
    assert related.candidates[0].para_id == "R1"
    assert FindRelatedPrsOutput().candidates == []  # 후보가 없어도 된다
    minimal = RelatedPr.model_validate({"pr_number": 1, "title": "t", "merged_at": T0})
    assert (minimal.spec_id, minimal.para_id, minimal.changed_paths) == (None, None, [])
    link = LinkIncidentInput.model_validate(
        {"incident_context_id": "ctx_inc_1", "pr_numbers": [42], "follow_up_title": "재발 방지"}
    )
    assert link.pr_numbers == [42]
    out = LinkIncidentOutput.model_validate({"linked_paras": ["R1"], "follow_up_request_id": "r1"})
    assert out.follow_up_request_id == "r1"
    assert LinkIncidentOutput().follow_up_request_id is None


def test_verify_rollback_target() -> None:
    ask = VerifyRollbackTargetInput.model_validate(
        {
            "service": "shop-api",
            "target_version": "v1.4.0",
            "deployed_at": T0,
            "incident_opened_at": T1,
        }
    )
    assert ask.service == "shop-api"
    answer = VerifyRollbackTargetOutput.model_validate(
        {"approved": True, "reason_code": "deploy_before_incident", "related_prs": [PR]}
    )
    assert answer.related_prs[0].pr_number == 42
    with pytest.raises(ValidationError):
        VerifyRollbackTargetOutput.model_validate({"approved": True, "reason_code": "because"})


def test_infra_diagnose_and_rollback() -> None:
    assert DiagnoseIncidentInput().component is None
    suspect = {
        "service": "shop-api",
        "version": "v1.4.0",
        "deployed_at": T0,
        "change_summary": "결제 타임아웃 설정 변경",
        "confidence": "mid",
    }
    assert SuspectDeployment.model_validate(suspect).confidence == "mid"
    diagnosis = DiagnoseIncidentOutput.model_validate(
        {"status": "suspected", "suspects": [suspect], "impact_scope": ["payment"]}
    )
    assert diagnosis.impact_scope == ["payment"]
    assert DiagnoseIncidentOutput.model_validate({"status": "undetermined"}).suspects == []
    with pytest.raises(ValidationError):  # 확신도는 low·mid·high만 (퍼센트 금지)
        SuspectDeployment.model_validate({**suspect, "confidence": "0.9"})
    with pytest.raises(ValidationError):
        DiagnoseIncidentOutput.model_validate({"status": "confirmed"})
    params = RollbackDeploymentParams.model_validate(
        {"service": "shop-api", "from_version": "v1.4.0", "to_version": "v1.3.2"}
    )
    assert params.release_fault_injection is True  # 데모 기본값


def test_verify_recovery_and_strictness() -> None:
    assert VerifyRecoveryInput.model_validate({"component": "payment", "since": T0}).since
    result = VerifyRecoveryOutput.model_validate(
        {"recovered": True, "infra_metrics_ok": True, "checked_at": T1}
    )
    assert result.business_metrics_ok is None  # 못 받으면 None
    with pytest.raises(ValidationError):  # 모르는 필드는 거부
        VerifyRecoveryOutput.model_validate(
            {"recovered": True, "infra_metrics_ok": True, "checked_at": T1, "eta": "10m"}
        )
    with pytest.raises(ValidationError):  # 시간대 없는 시각은 거부
        VerifyRecoveryInput.model_validate({"component": "payment", "since": "2026-10-22T05:00:00"})
