"""결제 지표 측정과 `shop.degraded`·`shop.recovered` 발행 판정. 프레임워크에 의존하지 않는다.

shop-api가 직접 측정한다(Zabbix는 보조 신호). 기준 초안은 **p95 1,000ms 초과 또는 5xx 2% 초과가
1분 지속**이고 기준값은 환경변수로 바꾼다(평소 p95 180ms·5xx 0.3%).
시간은 호출하는 쪽이 `now`(초)로 넘겨서 테스트에서 시계를 움직일 수 있다.
"""

from __future__ import annotations

import math
import os
from collections import deque
from dataclasses import dataclass

from ax_contracts.schemas import EventType, ShopDegradedData
from ax_shop_api import outbox
from ax_shop_api.db import Db

COMPONENT = "payment"


@dataclass(frozen=True)
class Thresholds:
    p95_latency_ms: float = 1000.0
    error_rate_5xx: float = 0.02
    window_s: float = 60.0
    sustain_s: float = 60.0
    min_samples: int = 5

    @classmethod
    def from_env(cls) -> Thresholds:
        def number(name: str, default: float) -> float:
            return float(os.getenv(f"SHOP_{name}") or default)

        return cls(
            p95_latency_ms=number("DEGRADED_P95_MS", cls.p95_latency_ms),
            error_rate_5xx=number("DEGRADED_5XX_RATE", cls.error_rate_5xx),
            window_s=number("MONITOR_WINDOW_S", cls.window_s),
            sustain_s=number("DEGRADED_SUSTAIN_S", cls.sustain_s),
            min_samples=int(number("MONITOR_MIN_SAMPLES", cls.min_samples)),
        )


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]  # nearest-rank


class Monitor:
    def __init__(self, db: Db, thresholds: Thresholds | None = None) -> None:
        self.db = db
        self.thresholds = thresholds or Thresholds.from_env()
        self._samples: deque[tuple[float, float, bool]] = deque()  # (시각, 지연 ms, 5xx 여부)
        self._breach_since: float | None = None
        self.degraded = False
        self._incident_key: str | None = None
        self._metric_code: str | None = None

    def record(self, now: float, latency_ms: float, server_error: bool) -> None:
        self._samples.append((now, latency_ms, server_error))

    def _breach(self, now: float) -> tuple[str, float, float] | None:
        """기준을 넘은 지표 `(metric_code, 값, 기준값)`. 표본이 모자라면 판단하지 않는다."""
        limit = self.thresholds
        while self._samples and now - self._samples[0][0] > limit.window_s:
            self._samples.popleft()
        if len(self._samples) < limit.min_samples:
            return None
        p95 = _p95([latency for _, latency, _ in self._samples])
        rate = sum(1 for *_, error in self._samples if error) / len(self._samples)
        if p95 > limit.p95_latency_ms:
            return "p95_latency_ms", p95, limit.p95_latency_ms
        if rate > limit.error_rate_5xx:
            return "error_rate_5xx", rate, limit.error_rate_5xx
        return None

    def _enqueue(
        self, event_type: EventType, metric: str, value: float | None, threshold: float | None
    ) -> None:
        data = ShopDegradedData(
            component=COMPONENT, metric_code=metric, value=value, threshold=threshold
        )
        with self.db.tx() as conn:
            if event_type == EventType.SHOP_DEGRADED:
                self._incident_key = self.db.next_id(conn, "degraded", "dgd_")
            outbox.enqueue(
                conn,
                event_type,
                data.model_dump(mode="json"),
                f"{event_type.value}:{self._incident_key}",
            )

    def evaluate(self, now: float) -> None:
        """기준 초과가 `sustain_s` 이어지면 `shop.degraded`, 정상 복귀하면 `shop.recovered`.

        지속 시간은 기준을 넘기 시작한 첫 표본(느리거나 5xx인 표본)의 시각부터 센다.
        """
        breach = self._breach(now)
        if breach is None:
            if len(self._samples) >= self.thresholds.min_samples:  # 표본이 충분한데 정상
                self._breach_since = None
                if self.degraded:
                    self.degraded = False
                    self._enqueue(EventType.SHOP_RECOVERED, self._metric_code or "", None, None)
            return
        if self._breach_since is None:
            slow = self.thresholds.p95_latency_ms
            bad = [at for at, latency, error in self._samples if latency > slow or error]
            self._breach_since = min(bad, default=now)
        if not self.degraded and now - self._breach_since >= self.thresholds.sustain_s:
            self.degraded = True
            self._metric_code = breach[0]
            self._enqueue(EventType.SHOP_DEGRADED, *breach)

    def reset(self) -> None:
        """장애를 해제할 때: 지난 표본을 버리고, 장애 상태였다면 `shop.recovered`를 발행한다.

        복구 직후에는 새 측정값이 없으므로 `value`·`threshold`는 비운다(확인할 수 없음).
        """
        self._samples.clear()
        self._breach_since = None
        if self.degraded:
            self.degraded = False
            self._enqueue(EventType.SHOP_RECOVERED, self._metric_code or "", None, None)
