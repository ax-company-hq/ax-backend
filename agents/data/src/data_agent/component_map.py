"""코드 경로 → 컴포넌트(지표) 대응 (이슈 #206).

원본은 `component_map.toml` 하나다. 숫자가 아니라 이름 대응만 다루므로 LLM을 쓰지 않는다.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

MAP_PATH = Path(__file__).with_name("component_map.toml")


@dataclass(frozen=True)
class ComponentMatch:
    component: str
    metrics: tuple[str, ...]
    prefix: str


def load_entries(path: Path = MAP_PATH) -> list[ComponentMatch]:
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return [
        ComponentMatch(e["component"], tuple(e["metrics"]), prefix)
        for e in raw["entry"]
        for prefix in e["prefixes"]
    ]


def lookup(code_path: str, entries: list[ComponentMatch] | None = None) -> ComponentMatch | None:
    """가장 긴 접두사가 맞는 항목을 돌려준다. 없으면 None ("확인할 수 없음" → escalate)."""
    normalized = code_path.lstrip("./")
    best: ComponentMatch | None = None
    for entry in entries if entries is not None else load_entries():
        if normalized.startswith(entry.prefix) and (
            best is None or len(entry.prefix) > len(best.prefix)
        ):
            best = entry
    return best
