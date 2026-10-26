"""문의 집계와 급증 감지. 프레임워크에 의존하지 않는 순수 코드다 (LLM 없음).

- 집계: 건수·비율·평균은 문의 저장소 기록에서 코드가 센다.
- 급증(DEP-CS-05): 최근 `window` 동안의 분류별 고객 메시지 수를, 그 앞 `baseline` 기간의 같은 길이
  창당 평균과 견준다. `min_count` 이상이고 평소의 `ratio`배 이상이면 급증이다. 평소가 0이면
  (비교할 수 없다) 건수 기준만으로 판정하고 배수는 None으로 둔다.
- 기준선 방식: 기본 `mean`(창당 평균)은 지난 어느 한 시간에 몰린 문의 하나에도 평소가 부풀어
  급증을 놓칠 수 있다. `median`은 기준 기간을 창으로 나눠 창별 건수의 중앙값을 평소로 쓴다
  (건수가 0인 창도 센다). 기본값은 기존 동작을 지키려고 `mean`이다.
"""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import UTC, datetime, timedelta

from support_agent.insight_schemas import (
    BaselineMethod,
    InquiryKpis,
    InquiryOverview,
    KeywordCount,
    SurgeReport,
    TopicSurge,
    WaitingInquiry,
)
from support_agent.schemas import Conversation, Message
from support_agent.store import InquiryStore

URGENCY_ORDER = {"high": 0, "medium": 1, "low": 2, None: 3}
SAMPLE_LIMIT = 3


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _customer_messages(conversations: list[Conversation]) -> list[tuple[Conversation, Message]]:
    return [(c, m) for c in conversations for m in c.messages if m.role == "customer"]


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def kpis(store: InquiryStore) -> InquiryKpis:
    conversations = store.all()
    customer = _customer_messages(conversations)
    analyses = [m.analysis for _, m in customer if m.analysis]

    drafts = [d for a in analyses for d in (a.drafts or [])]
    findings = Counter(f.code for d in drafts for f in d.validation.findings)
    events = store.draft_events
    accepted = sum(1 for e in events if e["result"] == "accepted")
    edited = sum(1 for e in events if e["result"] == "edited")
    rejected = sum(1 for e in events if e["result"] == "rejected")
    ratios = [e["edit_ratio"] for e in events if e.get("edit_ratio") is not None]
    timings = [
        a.timings_ms["classify"] + a.timings_ms["draft"]
        for a in analyses
        if a.timings_ms and {"classify", "draft"} <= set(a.timings_ms)
    ]
    by_language: Counter[str] = Counter()
    for conversation in conversations:
        classified = [
            m.analysis.classification
            for m in conversation.messages
            if m.role == "customer" and m.analysis and m.analysis.classification
        ]
        if classified:
            by_language[classified[-1].language] += 1
    keyword_counts: Counter[str] = Counter()
    for analysis in analyses:
        if analysis.classification:  # 한 메시지에서 같은 키워드는 한 번만 센다 (대소문자 무시)
            keyword_counts.update({w.casefold() for w in analysis.classification.keywords})
    top_keywords = [
        KeywordCount(keyword=keyword, count=count)
        for keyword, count in sorted(keyword_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    ]
    return InquiryKpis(
        conversations=len(conversations),
        by_status=dict(Counter(c.status for c in conversations)),
        agent_replies=sum(1 for c in conversations for m in c.messages if m.role == "agent"),
        draft_adoption_rate=_rate(accepted + edited, accepted + edited + rejected),
        accepted_as_is=accepted,
        edited=edited,
        rejected=rejected,
        avg_edit_ratio=round(sum(ratios) / len(ratios), 3) if ratios else None,
        avg_first_draft_ms=int(sum(timings) / len(timings)) if timings else None,
        drafts_generated=len(drafts),
        drafts_blocked_by_guard=sum(1 for d in drafts if not d.validation.passed),
        guard_findings=dict(findings),
        by_category=dict(Counter(c.category for c in conversations if c.category)),
        by_urgency=dict(Counter(c.urgency for c in conversations if c.urgency)),
        by_sentiment=dict(Counter(c.sentiment for c in conversations if c.sentiment)),
        by_language=dict(by_language),
        top_keywords=top_keywords,
    )


def overview(store: InquiryStore, now: datetime | None = None) -> InquiryOverview:
    now = now or datetime.now(UTC)
    waiting = [
        WaitingInquiry(
            id=c.id,
            customer_name=c.customer_name,
            subject=c.subject,
            category=c.category,
            urgency=c.urgency,
            sentiment=c.sentiment,
            preview=c.preview,
            waiting_minutes=max(int((now - parse_time(c.updated_at)).total_seconds() // 60), 0),
        )
        for c in store.all()
        if c.awaiting_reply and c.status == "open"
    ]
    waiting.sort(key=lambda w: (URGENCY_ORDER.get(w.urgency, 3), -w.waiting_minutes, w.id))
    return InquiryOverview(kpis=kpis(store), waiting=waiting)


def detect_surge(
    store: InquiryStore,
    now: datetime,
    *,
    window_minutes: int = 60,
    baseline_hours: int = 24,
    min_count: int = 5,
    ratio: float = 3.0,
    baseline_method: BaselineMethod = "mean",
) -> SurgeReport:
    window = timedelta(minutes=window_minutes)
    baseline = timedelta(hours=baseline_hours)
    recent_from = now - window
    baseline_from = recent_from - baseline
    windows_in_baseline = baseline / window

    median = baseline_method == "median"
    whole_windows = max(int(windows_in_baseline), 1)
    recent: dict[str, list[int]] = {}
    earlier: Counter[str] = Counter()
    per_slot: dict[str, Counter[int]] = {}  # 중앙값용: 주제별 (몇 번째 이전 창 → 건수)
    for conversation, message in _customer_messages(store.all()):
        if not (message.analysis and message.analysis.classification):
            continue
        topic = message.analysis.classification.category
        at = parse_time(message.created_at)
        if recent_from < at <= now:
            recent.setdefault(topic, []).append(conversation.id)
        elif baseline_from < at <= recent_from:
            earlier[topic] += 1
            slot = int((recent_from - at) / window)
            if slot < whole_windows:
                per_slot.setdefault(topic, Counter())[slot] += 1

    surges: list[TopicSurge] = []
    for topic, ids in recent.items():
        if median:
            slots = per_slot.get(topic, Counter())
            per_window = float(statistics.median(slots[i] for i in range(whole_windows)))
        else:
            per_window = earlier[topic] / windows_in_baseline
        times = round(len(ids) / per_window, 2) if per_window else None
        if len(ids) >= min_count and (times is None or times >= ratio):
            surges.append(
                TopicSurge(
                    topic_code=topic,
                    count=len(ids),
                    baseline_per_window=round(per_window, 3),
                    ratio=times,
                    sample_inquiry_ids=sorted(set(ids))[:SAMPLE_LIMIT],
                )
            )
    surges.sort(key=lambda s: (-s.count, s.topic_code))
    return SurgeReport(
        window_minutes=window_minutes,
        baseline_hours=baseline_hours,
        baseline_method=baseline_method,
        evaluated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        total_in_window=sum(len(v) for v in recent.values()),
        surges=surges,
    )
