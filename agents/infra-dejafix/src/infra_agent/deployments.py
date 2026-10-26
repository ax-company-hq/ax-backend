"""배포 이력: 원인 진단과 롤백 대상(직전 정상 버전)을 정하는 원본.

실제 배포 시스템이 없어 JSON 파일 하나가 배포 기록이다. 기본은 패키지 안의 `data/deployments.json`
(planning 초기 데이터와 같은 버전·시각)이고 `INFRA_DEPLOYMENTS_FILE`로 바꿀 수 있다.
롤백한 배포는 "되돌림"으로 표시해 직전 정상 버전을 고를 때 건너뛴다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Deployment:
    deploy_id: str
    service: str
    version: str
    deployed_at: datetime
    components: tuple[str, ...]
    change_summary: str
    irreversible: tuple[str, ...] = ()


def _parse(row: dict[str, Any]) -> Deployment:
    return Deployment(
        deploy_id=row["deploy_id"],
        service=row["service"],
        version=row["version"],
        deployed_at=datetime.fromisoformat(row["deployed_at"].replace("Z", "+00:00")),
        components=tuple(row.get("components", [])),
        change_summary=row.get("change_summary", ""),
        irreversible=tuple(row.get("irreversible", [])),
    )


def load_rows() -> list[dict[str, Any]]:
    override = os.getenv("INFRA_DEPLOYMENTS_FILE")
    if override:
        text = Path(override).read_text(encoding="utf-8")
    else:
        text = (
            resources.files("infra_agent")
            .joinpath("data/deployments.json")
            .read_text(encoding="utf-8")
        )
    return list(json.loads(text).get("deployments", []))


@dataclass
class DeploymentHistory:
    deployments: list[Deployment]
    rolled_back: set[str] = field(default_factory=set)  # deploy_id

    @classmethod
    def load(cls) -> DeploymentHistory:
        return cls([_parse(row) for row in load_rows()])

    def for_component(self, component: str | None) -> list[Deployment]:
        """그 구성요소를 바꾼 배포, 최근 것이 앞."""
        rows = [d for d in self.deployments if component is None or component in d.components]
        return sorted(rows, key=lambda d: d.deployed_at, reverse=True)

    def find(self, service: str, version: str) -> Deployment | None:
        return next(
            (d for d in self.deployments if d.service == service and d.version == version), None
        )

    def previous_good(self, target: Deployment) -> Deployment | None:
        """같은 서비스에서 대상 배포 바로 앞의, 되돌리지 않은 배포."""
        earlier = [
            d
            for d in self.deployments
            if d.service == target.service
            and d.deployed_at < target.deployed_at
            and d.deploy_id not in self.rolled_back
        ]
        return max(earlier, key=lambda d: d.deployed_at, default=None)

    def mark_rolled_back(self, target: Deployment) -> None:
        self.rolled_back.add(target.deploy_id)
