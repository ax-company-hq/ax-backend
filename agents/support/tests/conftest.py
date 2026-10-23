"""support 테스트 공용 설정.

에이전트 템플릿의 내부 상태(에이전트 이름, 목 이벤트 버스, 목 실행 카운터)를 매 테스트 시작·끝에
비운다. `create_agent_app`이 이름을 전역에 두어서 다른 서비스 테스트의 상태가 전체 실행
(`uv run pytest`)에서만 새어 들어오던 문제는 템플릿이 고쳤고(#152), 우회 코드(#131)는 지웠다.
"""

import json
from typing import Any

import pytest

from ax_agent_template.testing import reset_state  # noqa: F401  autouse
from support_agent.persistence import BY_NAME, TABLES


class MemoryRepo:
    """`PgRepo`와 같은 인터페이스. JSON으로 직렬화해 저장해서 DB와 같은 제약(JSON만 가능)을 둔다."""

    def __init__(self) -> None:
        self.tables: dict[str, dict[tuple, str]] = {t.name: {} for t in TABLES}
        self.fail = False

    def load(self, table: str) -> list[dict[str, Any]]:
        rows = self.tables[table]
        return [json.loads(rows[key]) for key in sorted(rows)]

    def upsert(self, table: str, values: dict[str, Any], doc: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("db down")
        spec = BY_NAME[table]
        assert set(values) == {*spec.pk, *spec.columns}  # 컬럼을 빠뜨리지 않는다
        self.tables[table][tuple(values[c] for c in spec.pk)] = json.dumps(doc, ensure_ascii=False)


@pytest.fixture
def memory_repo() -> type[MemoryRepo]:
    """영속 저장소 인터페이스의 메모리 구현. `memory_repo()`로 새 저장소(=새 DB)를 만든다."""
    return MemoryRepo
