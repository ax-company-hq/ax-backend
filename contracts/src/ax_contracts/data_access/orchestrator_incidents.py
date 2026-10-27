"""총괄(orchestrator) 사건 목록 조회 (finance 에이전트가 읽기 전용으로 사용).

제공: orchestrator `GET /api/incidents` (8000) · 사용: finance
합의: [협업] 이슈 #417 (제안 A, 2026-10-27). 후속 요청 #418.

재무는 서버 장애 대응 흐름의 단계가 아니다 (요구사항 DEP-FIN-01, 공통 구성안 결정 22).
사건 **중**에는 총괄을 부르지 않고, 평상시 사후 감사(`audit_report`)·사건 조회에서만 읽는다.
재무가 쓰는 필드는 `context_id`, `component`, `status`, `opened_at`, `closed_at` 다섯 개뿐이다.

`GET /api/incidents`는 지금 질의 매개변수를 받지 않고 사건 전부를
`{"data": [...], "meta": {...}}`로 돌려준다. 그래서 `OrchestratorIncidentsQuery`의 값은
**읽는 쪽이 받은 뒤에 거르는 조건**이다. 응답 행에는 재무가 쓰지 않는 키(`steps`, `events`,
`summary`, `metrics` 등)가 더 들어 있고 `AxModel`은 모르는 키를 거부하므로, 읽는 쪽이 위 다섯
키만 골라낸 뒤 검증한다.

`status`는 문자열로 둔다(총괄이 값을 늘릴 수 있다). 지금 쓰는 값: `open`, `resolved`, `degraded`.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel, UtcDatetime


class OrchestratorIncidentsQuery(AxModel):
    """입력(읽는 쪽이 거르는 조건). `from`은 파이썬 예약어라 별칭으로 받는다."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: UtcDatetime | None = Field(default=None, alias="from")  # 사건 시작 시각 이후만
    to: UtcDatetime | None = None  # 사건 시작 시각 이전만
    statuses: list[str] | None = None  # 비우면 상태를 가리지 않는다
    limit: int = Field(default=1000, ge=1)
    tenant_id: str = TENANT_ID_DEFAULT


class OrchestratorIncident(AxModel):
    """사건 한 건. 사건 번호는 `ctx_inc_`로 시작한다."""

    context_id: str
    component: str | None = None  # 장애 구성요소. 모르면 None
    status: str
    opened_at: UtcDatetime
    closed_at: UtcDatetime | None = None  # 아직 열려 있으면 None
    tenant_id: str = TENANT_ID_DEFAULT


class OrchestratorIncidentsResult(AxModel):
    """출력. 조회 자체의 근거(쿼리·시각)는 `Evidence`로 붙인다."""

    incidents: list[OrchestratorIncident] = Field(default_factory=list)
    collected_at: UtcDatetime | None = None  # 조회 시각
    truncated: bool = False  # limit에 걸려 잘렸으면 True
