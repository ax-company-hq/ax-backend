"""실행 중 바꾼 숫자 기준을 파일에 남겨 재시작 뒤에도 복원한다.

- 경로: `SAFETY_THRESHOLD_OVERRIDES_PATH`.
  없으면 `data/safety/threshold_overrides.json`(작업 폴더 기준)
- 사람이 승인해 적용할 때마다 저장한다. 저장하지 못하면 적용하지 않는다(감사 로그와 실제 기준이
  어긋나지 않게)
- 시작할 때 읽는다. 저장할 때의 원래 값(`baseline`, TOML + 환경변수)과 지금 원래 값이 다르면
  사람이 기준표를 고친 것이므로 버리고 TOML 값을 쓴다. 파일이 깨졌어도 TOML 값을 쓴다
- 기록 형식: `version`, `baseline`(저장 때 원래 값), `values`(원래 값과 다른 기준만),
  `change_id`(마지막으로 적용한 기준 바꾸기), `saved_at`
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

OVERRIDES_ENV = "SAFETY_THRESHOLD_OVERRIDES_PATH"
DEFAULT_PATH = Path("data") / "safety" / "threshold_overrides.json"


class SavedOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    baseline: dict[str, int]
    values: dict[str, int]
    change_id: str | None = None
    saved_at: datetime


class OverrideFile:
    """바뀐 기준을 JSON 파일 하나에 저장한다. 쓰기는 임시 파일 → 바꿔치기라 반쯤 쓴 파일이 없다."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @classmethod
    def from_env(cls) -> OverrideFile:
        raw = os.getenv(OVERRIDES_ENV, "").strip()
        return cls(Path(raw) if raw else DEFAULT_PATH)

    def load(self) -> SavedOverrides | None:
        """없으면 None. 깨졌으면 ValueError(pydantic ValidationError 포함)."""
        if not self.path.exists():
            return None
        return SavedOverrides.model_validate_json(self.path.read_text("utf-8"))

    def save(self, saved: SavedOverrides) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(saved.model_dump_json(indent=2), "utf-8")
        os.replace(tmp, self.path)
