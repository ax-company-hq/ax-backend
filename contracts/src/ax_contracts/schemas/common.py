"""모든 계약 모델이 같이 쓰는 기본 요소.

- `AxModel`: 계약 모델의 기반 클래스. 정의하지 않은 필드가 들어오면 거부한다
  (`allowedRequesters` 같은 camelCase 오타를 잡기 위해서다).
- `UtcDatetime`: 시간은 항상 UTC. 시간대가 없는 값은 거부하고, 다른 시간대는 UTC로 바꾼다.
- `TENANT_ID_DEFAULT`: 고객사 ID 기본값 (`shop-demo`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict

TENANT_ID_DEFAULT = "shop-demo"


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]


class AxModel(BaseModel):
    """계약 모델 기반 클래스. 우리가 정의하는 필드는 모두 snake_case다."""

    model_config = ConfigDict(extra="forbid")
