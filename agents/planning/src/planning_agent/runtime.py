"""프로세스 안의 상태 저장소 하나 (목 모드). A2A 핸들러와 REST가 같은 서비스를 본다.

v0은 DB 없이 프로세스 메모리에만 둔다 (재시작하면 초기 데이터로 돌아간다).
"""

from __future__ import annotations

from planning_agent.domain.service import PlanningService

_service: PlanningService | None = None


def get_service() -> PlanningService:
    global _service
    if _service is None:
        _service = PlanningService()
    return _service


def set_service(service: PlanningService) -> PlanningService:
    """테스트에서 시계·관문을 바꾼 서비스를 끼울 때 쓴다."""
    global _service
    _service = service
    return service
