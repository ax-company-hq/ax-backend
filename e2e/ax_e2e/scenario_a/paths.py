"""레포 위치와 부서 Agent Card 찾기. 부서 목록·skill 담당은 카드 파일에서 읽는다 (하드코딩 금지)."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path


def repo_root() -> Path:
    override = os.getenv("AX_E2E_ROOT")
    if override:
        return Path(override)
    # e2e/ax_e2e/scenario_a/paths.py → 레포 루트
    return Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def cards() -> dict[str, dict]:
    """`agents/*/agent-card.json` → {카드 name: 카드}. 폴더 이름이 달라도 카드 name을 쓴다."""
    out: dict[str, dict] = {}
    for path in sorted((repo_root() / "agents").glob("*/agent-card.json")):
        card = json.loads(path.read_text(encoding="utf-8"))
        card["_folder"] = path.parent.name
        out[card["name"]] = card
    return out


def card_skills(dept: str) -> dict[str, dict]:
    return {s["id"]: s for s in cards().get(dept, {}).get("skills", [])}


def dept_of_skill(skill: str) -> str | None:
    """skill을 낸 부서 (오케스트레이터 레지스트리와 같은 규칙: 이름순 첫 부서)."""
    for dept in sorted(cards()):
        if skill in card_skills(dept):
            return dept
    return None


def app_module(dept: str) -> str | None:
    """부서 폴더의 `src/<패키지>` 이름 → `<패키지>.main`."""
    folder = cards().get(dept, {}).get("_folder")
    if not folder:
        return None
    src = repo_root() / "agents" / folder / "src"
    pkgs = sorted(p.name for p in src.iterdir() if p.is_dir() and (p / "__init__.py").exists())
    return f"{pkgs[0]}.main" if pkgs else None
