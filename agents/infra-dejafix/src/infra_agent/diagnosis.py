"""원인 진단 규칙 (코드). LLM을 쓰지 않는다.

- 진단 구간(기본 60분) 안에 그 구성요소를 바꾼 배포를 원인 후보로 본다. 없으면 `undetermined`.
- 확신도: 후보가 하나이고 배포 뒤 15분 안에 진단하면 `high`, 그보다 오래됐으면 `mid`.
  후보가 여럿이면 가장 최근 것이 `mid`, 나머지는 `low`.
- 목 모드는 시간과 상관없이 가장 최근 배포 1건을 `high` 후보로 돌려준다(SCN-A 체크리스트).
- 복구 예상 시각은 만들지 않는다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from ax_contracts.schemas.infra import DiagnoseIncidentOutput, SuspectDeployment
from infra_agent.deployments import Deployment, DeploymentHistory

HIGH_CONFIDENCE_WINDOW = timedelta(minutes=15)


def lookback() -> timedelta:
    return timedelta(minutes=int(os.getenv("INFRA_DIAGNOSE_LOOKBACK_MIN") or 60))


@dataclass(frozen=True)
class Diagnosis:
    output: DiagnoseIncidentOutput
    candidates: tuple[Deployment, ...]
    diagnosed_at: datetime
    window_from: datetime | None  # 목 모드는 구간 없음

    def confidence_of(self, service: str, version: str) -> str | None:
        for suspect in self.output.suspects:
            if suspect.service == service and suspect.version == version:
                return suspect.confidence
        return None


def _suspect(deployment: Deployment, confidence: str) -> SuspectDeployment:
    return SuspectDeployment(
        service=deployment.service,
        version=deployment.version,
        deployed_at=deployment.deployed_at,
        change_summary=deployment.change_summary,
        confidence=confidence,  # type: ignore[arg-type]
    )


def diagnose(
    history: DeploymentHistory, component: str | None, now: datetime, *, mock: bool
) -> Diagnosis:
    scope = [component] if component else []
    rows = history.for_component(component)
    if mock:
        picked = rows[:1]
        suspects = [_suspect(d, "high") for d in picked]
        window_from = None
    else:
        window_from = now - lookback()
        picked = [d for d in rows if window_from <= d.deployed_at <= now]
        if len(picked) == 1:
            fresh = now - picked[0].deployed_at <= HIGH_CONFIDENCE_WINDOW
            suspects = [_suspect(picked[0], "high" if fresh else "mid")]
        else:
            suspects = [_suspect(d, "mid" if i == 0 else "low") for i, d in enumerate(picked)]
    output = DiagnoseIncidentOutput(
        status="suspected" if suspects else "undetermined",
        suspects=suspects,
        impact_scope=scope,
    )
    return Diagnosis(
        output=output, candidates=tuple(picked), diagnosed_at=now, window_from=window_from
    )
