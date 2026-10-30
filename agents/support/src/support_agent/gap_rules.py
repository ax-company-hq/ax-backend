"""지식 공백 순위 계산. 프레임워크에 의존하지 않는 순수 코드다 (LLM 없음).

설계: docs/notes/support/2026-10-28-knowledge-gap-qa-design.md (4.2)

분류별로 "근거 없이 답한 비율", "초안을 거절·차단한 비율", "사람에게 넘어간 비율"을 센다.
모든 기준값(기간·최소 표본·임계·가중치)은 근거 데이터가 없는 시드다.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from support_agent import kb
from support_agent.gap_schemas import GapItem, GapType, KnowledgeGapReport, SuggestedAction
from support_agent.insights import SAMPLE_LIMIT, parse_time
from support_agent.schemas import Conversation
from support_agent.store import InquiryStore

UNKNOWN = "unknown"
NO_DOCUMENT_RATE = 0.5  # 근거 없음 비율이 이 이상이면 문서가 없다고 본다 (시드)
WEAK_DOCUMENT_RATE = 0.3  # 거절·차단 비율이 이 이상이면 문서를 점검한다 (시드)
# 처방을 받을 곳의 코드. 부서 이름이 아니라서 부서 구조가 바뀌어도 이 표만 바꾼다
OWNER_TARGETS: dict[str, str] = {}
DEFAULT_OWNER_TARGET = "kb_owner"
SEED_NOTE = (
    "근거 출처가 시드(%d조각)이다. 정책 문서 출처(MEM-03)가 연결되기 전의 순위는 참고용이다."
)

Key = tuple[str, str | None]  # (분류, 근거 문서)


@dataclass
class _Bucket:
    inquiries: set[int] = field(default_factory=set)
    analyzed: int = 0
    no_evidence: int = 0
    escalated: set[int] = field(default_factory=set)
    decisions: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def _last_category(conversation: Conversation) -> str:
    for message in reversed(conversation.messages):
        if message.role == "customer" and message.analysis and message.analysis.classification:
            return message.analysis.classification.category
    return UNKNOWN


def _item(key: Key, bucket: _Bucket, min_samples: int) -> GapItem:
    category, doc_id = key
    decided = sum(bucket.decisions.values())
    rejected = bucket.decisions["rejected"]
    blocked = bucket.decisions["blocked"]
    no_evidence_rate = _rate(bucket.no_evidence, bucket.analyzed)
    reject_rate = _rate(rejected + blocked, decided)
    escalated_rate = _rate(len(bucket.escalated), len(bucket.inquiries))
    rates = [r for r in (no_evidence_rate, reject_rate, escalated_rate) if r is not None]

    gap_type: GapType
    action: SuggestedAction
    if bucket.analyzed < min_samples:
        gap_type, action = "insufficient_samples", "none"
    elif no_evidence_rate is not None and no_evidence_rate >= NO_DOCUMENT_RATE:
        gap_type, action = "no_document", "add_document"
    elif reject_rate is not None and reject_rate >= WEAK_DOCUMENT_RATE:
        gap_type, action = "weak_document", "review_document"
    else:
        gap_type, action = "none", "none"
    return GapItem(
        category=category,
        doc_id=doc_id,
        inquiries=len(bucket.inquiries),
        analyzed_messages=bucket.analyzed,
        no_evidence=bucket.no_evidence,
        drafts_decided=decided,
        drafts_edited=bucket.decisions["edited"],
        drafts_rejected=rejected,
        drafts_blocked=blocked,
        escalated=len(bucket.escalated),
        reject_reasons=dict(bucket.reasons),
        no_evidence_rate=no_evidence_rate,
        reject_rate=reject_rate,
        escalated_rate=escalated_rate,
        gap_type=gap_type,
        gap_score=round(sum(rates) / len(rates), 3) if rates else None,
        sample_inquiry_ids=sorted(bucket.inquiries)[:SAMPLE_LIMIT],
        owner_target=OWNER_TARGETS.get(category, DEFAULT_OWNER_TARGET),
        suggested_action=action,
    )


def _ranked(items: list[GapItem]) -> list[GapItem]:
    """표본이 충분한 줄을 점수 순으로 줄 세우고, 표본 부족은 뒤에 둔다 (순위 없음)."""
    enough = [i for i in items if i.gap_type != "insufficient_samples"]
    enough.sort(
        key=lambda i: (-(i.gap_score or 0), -i.analyzed_messages, i.category, i.doc_id or "")
    )
    ranked = [i.model_copy(update={"rank": n}) for n, i in enumerate(enough, 1)]
    rest = [i for i in items if i.gap_type == "insufficient_samples"]
    rest.sort(key=lambda i: (-i.analyzed_messages, i.category, i.doc_id or ""))
    return ranked + rest


def build_report(
    store: InquiryStore,
    now: datetime | None = None,
    *,
    window_days: int = 7,
    min_samples: int = 5,
    category: str | None = None,
) -> KnowledgeGapReport:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=window_days)
    cats: dict[Key, _Bucket] = defaultdict(_Bucket)
    docs: dict[Key, _Bucket] = defaultdict(_Bucket)
    by_id = {c.id: c for c in store.all()}

    for conversation in by_id.values():
        for message in conversation.messages:
            if message.role != "customer" or not message.analysis:
                continue
            if not since < parse_time(message.created_at) <= now:
                continue
            cat = (
                message.analysis.classification.category
                if message.analysis.classification
                else UNKNOWN
            )
            sources = message.analysis.sources or []
            targets = [cats[(cat, None)]]
            if sources:
                targets.append(docs[(cat, sources[0].doc_id)])
            for bucket in targets:
                bucket.inquiries.add(conversation.id)
                bucket.analyzed += 1
                bucket.no_evidence += 0 if sources else 1
                if conversation.status == "escalated":
                    bucket.escalated.add(conversation.id)

    for event in store.draft_events:
        conversation = by_id.get(event["inquiry_id"])
        # 옛 기록에는 시각·분류가 없어 문의의 마지막 활동과 마지막 분류로 잇는다
        stamp = event.get("occurred_at") or (conversation.updated_at if conversation else None)
        if stamp is None or not since < parse_time(stamp) <= now:
            continue
        cat = event.get("category") or (_last_category(conversation) if conversation else UNKNOWN)
        targets = [cats[(cat, None)]]
        if event.get("doc_id"):
            targets.append(docs[(cat, event["doc_id"])])
        for bucket in targets:
            bucket.decisions[event["result"]] += 1
            if event.get("reason_code"):
                bucket.reasons[event["reason_code"]] += 1
            if conversation:
                bucket.inquiries.add(conversation.id)

    def build(buckets: dict[Key, _Bucket]) -> list[GapItem]:
        return _ranked(
            [
                _item(key, bucket, min_samples)
                for key, bucket in buckets.items()
                if category is None or key[0] == category
            ]
        )

    return KnowledgeGapReport(
        window_days=window_days,
        min_samples=min_samples,
        evaluated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        categories=build(cats),
        documents=build(docs),
        notes=[SEED_NOTE % len(kb.KB_CHUNKS)],
    )
