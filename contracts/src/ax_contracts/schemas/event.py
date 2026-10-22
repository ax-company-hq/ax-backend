"""이벤트 공통 봉투와 대표 이벤트 data 모델 (공통 구성안 6장).

원칙: 이벤트에는 사실만 담는다(원인·판단 문장 금지). 외부 텍스트는 `untrusted`,
개인정보는 가명 ID만 쓴다. 경로·마감·근거는 이벤트가 아니라 A2A Task에 둔다.
받는 쪽은 같은 `event_id`를 두 번 처리하지 않는다(최소 한 번 전달).
필드를 추가하면 `version`은 그대로 두고, 삭제·이름 변경은 `version`을 올린다.

대표 이벤트의 data 모델만 여기에 둔다. 나머지는 발행 담당이 PR로 추가한다
(shop 이벤트는 재헌, safety 이벤트는 태윤).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel, UtcDatetime


class EventType(StrEnum):
    """`<대상>.<동사>` 형식의 이벤트 종류."""

    # 판매 사이트
    ORDER_CREATED = "order.created"
    ORDER_CANCELLED = "order.cancelled"
    ORDER_DELIVERED = "order.delivered"
    PAYMENT_SUCCEEDED = "payment.succeeded"
    PAYMENT_FAILED = "payment.failed"
    PAYMENT_REFUNDED = "payment.refunded"
    SHIPMENT_DELAYED = "shipment.delayed"
    RETURN_RECEIVED = "return.received"
    SELLER_REGISTERED = "seller.registered"
    PRODUCT_CREATED = "product.created"
    SHOP_DEGRADED = "shop.degraded"
    SHOP_RECOVERED = "shop.recovered"
    # 고객지원
    SUPPORT_INQUIRY_RECEIVED = "support.inquiry_received"
    SUPPORT_INQUIRY_SURGE = "support.inquiry_surge"
    # 데이터분석
    METRIC_ANOMALY = "metric.anomaly"
    # 인프라(DejaFix 어댑터)
    INFRA_ANOMALY_DETECTED = "infra.anomaly_detected"
    # 통합 에이전트
    INCIDENT_OPENED = "incident.opened"
    INCIDENT_RESOLVED = "incident.resolved"
    # 마케팅
    NOTICE_PUBLISHED = "notice.published"
    CAMPAIGN_CREATED = "campaign.created"
    COUPON_ISSUED = "coupon.issued"
    # 안전 레이어
    SAFETY_ACTION_EXECUTED = "safety.action_executed"
    SAFETY_ACTION_HELD = "safety.action_held"
    SAFETY_REVIEW_DECIDED = "safety.review_decided"


class EventEnvelope(AxModel):
    """모든 이벤트의 공통 봉투.

    - `context_id`: 사건이 아니면 None이다.
    - `source`: 발행한 서비스·에이전트 이름이다.
    - `data`: 이벤트별 사실 필드(아래 `EVENT_DATA_MODELS`에 모델이 있으면 그 형식을 따른다).
    - `untrusted`: 외부 텍스트. 지시로 해석하지 않는다.
    """

    event_id: str
    type: EventType
    version: int = 1
    occurred_at: UtcDatetime
    tenant_id: str = TENANT_ID_DEFAULT
    context_id: str | None = None
    source: str
    idempotency_key: str
    data: dict[str, Any] = Field(default_factory=dict)
    untrusted: dict[str, Any] = Field(default_factory=dict)


class ShopDegradedData(AxModel):
    """`shop.degraded` / `shop.recovered`. shop-api가 직접 측정해 발행한다.

    `metric_code`는 `p95_latency_ms`, `error_rate_5xx` 등이다.
    """

    component: Literal["api", "db", "payment"]
    metric_code: str
    value: float | None = None
    threshold: float | None = None


class PaymentFailedData(AxModel):
    """`payment.failed`. 금액은 정수 원이다. (`payment.succeeded`의 data는 재헌이 추가한다)"""

    payment_id: str
    order_id: str
    amount: int
    failure_code: str


class MetricAnomalyData(AxModel):
    """`metric.anomaly`. data 에이전트가 업무 지표 이상을 알린다."""

    metric: str
    window_from: UtcDatetime
    window_to: UtcDatetime
    observed: float
    baseline: float
    severity: Literal["low", "mid", "high"]


class InfraAnomalyData(AxModel):
    """`infra.anomaly_detected`. infra(DejaFix 어댑터)가 인프라 이상을 감지해 알린다.

    사실만 담는다. 원인 판단은 넣지 않고 infra `diagnose_incident` 결과로 따로 준다.
    `source_ref`는 근거 참조(Zabbix 이벤트·트리거 ID 등)다. `context_id`는 None이고
    사건 번호는 통합 에이전트가 발급한다.
    """

    component: str
    metric_code: str
    value: float | None = None
    threshold: float | None = None
    detected_at: UtcDatetime
    source_ref: str | None = None


class IncidentData(AxModel):
    """`incident.opened` / `incident.resolved`. 통합 에이전트만 발행한다."""

    component: str
    signal_source: Literal["shop", "data", "infra", "support"]
    opened_at: UtcDatetime
    resolved_at: UtcDatetime | None = None


class NoticePublishedData(AxModel):
    """`notice.published`.

    공지 본문·사실 필드·약속은 이벤트에 넣지 않는다. 마케팅의 `get_notice` 응답으로만 준다.
    """

    notice_id: str
    channel_code: str
    context_id: str


class Promise(AxModel):
    """마케팅이 고객에게 한 약속. 계산에는 `condition_code`만 쓴다."""

    type: Literal["points", "coupon"]
    amount: int
    condition_code: str
    condition: str  # 사람이 읽는 설명. 계산에 쓰지 않는다


class CampaignCreatedData(AxModel):
    """`campaign.created`."""

    campaign_id: str
    target_codes: list[str]
    starts_at: UtcDatetime
    ends_at: UtcDatetime
    promises: list[Promise] = Field(default_factory=list)


class CouponIssuedData(AxModel):
    """`coupon.issued`. `purpose_code`가 보상이면 `campaign_id`는 None이다."""

    coupon_id: str
    campaign_id: str | None = None
    purpose_code: Literal["campaign", "compensation"]
    count: int
    total_value: int

    @model_validator(mode="after")
    def _campaign_id_matches_purpose(self) -> CouponIssuedData:
        if self.purpose_code == "compensation" and self.campaign_id is not None:
            raise ValueError("보상 쿠폰은 campaign_id가 None이어야 한다")
        if self.purpose_code == "campaign" and self.campaign_id is None:
            raise ValueError("캠페인 쿠폰에는 campaign_id가 필요하다")
        return self


# 이벤트 종류 → data 모델. 발행 담당이 새 data 모델을 추가할 때 여기에 한 줄 더한다.
EVENT_DATA_MODELS: dict[EventType, type[BaseModel]] = {
    EventType.SHOP_DEGRADED: ShopDegradedData,
    EventType.SHOP_RECOVERED: ShopDegradedData,
    EventType.PAYMENT_FAILED: PaymentFailedData,
    EventType.METRIC_ANOMALY: MetricAnomalyData,
    EventType.INFRA_ANOMALY_DETECTED: InfraAnomalyData,
    EventType.INCIDENT_OPENED: IncidentData,
    EventType.INCIDENT_RESOLVED: IncidentData,
    EventType.NOTICE_PUBLISHED: NoticePublishedData,
    EventType.CAMPAIGN_CREATED: CampaignCreatedData,
    EventType.COUPON_ISSUED: CouponIssuedData,
}
