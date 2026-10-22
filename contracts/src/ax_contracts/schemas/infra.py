"""infra(DejaFix 어댑터) 에이전트 skill 입출력 (시나리오 A 필수 3개). 이슈 #10 요청(예진).

- `diagnose_incident`: orchestrator·marketing·support → infra, 등급 read.
- `rollback_deployment`: 상태를 바꾸므로 `execute(action="rollback_deployment")`의 params다.
  등급 B, 검증자 planning.
- `verify_recovery`: orchestrator → infra, 등급 read.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ax_contracts.schemas.common import AxModel, UtcDatetime


class SuspectDeployment(AxModel):
    service: str
    version: str
    deployed_at: UtcDatetime
    change_summary: str
    confidence: Literal["low", "mid", "high"]


class DiagnoseIncidentInput(AxModel):
    component: str | None = None


class DiagnoseIncidentOutput(AxModel):
    """원인이 정해지지 않으면 `undetermined`이다. 복구 예상 시각은 주지 않는다."""

    status: Literal["suspected", "undetermined"]
    suspects: list[SuspectDeployment] = Field(default_factory=list)
    impact_scope: list[str] = Field(default_factory=list)


class RollbackDeploymentParams(AxModel):
    service: str
    from_version: str
    to_version: str
    release_fault_injection: bool = True  # 데모: 롤백 = shop-api 장애 주입 해제 호출


class VerifyRecoveryInput(AxModel):
    component: str
    since: UtcDatetime


class VerifyRecoveryOutput(AxModel):
    """둘 다 정상이면 orchestrator가 `incident.resolved`를 발행한다."""

    recovered: bool
    infra_metrics_ok: bool
    business_metrics_ok: bool | None = None  # data anomaly_detection 결과, 못 받으면 None
    checked_at: UtcDatetime
