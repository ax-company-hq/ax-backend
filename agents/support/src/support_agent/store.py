"""문의 저장소 (메모리).

TODO: DB로 옮긴다. 테이블은 복수형 snake_case이고 `tenant_id`·`context_id`를 갖는다 (`inquiries`,
`inquiry_messages`). 지금은 프로세스 메모리라서 재시작하면 사라진다.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import Any

from support_agent.schemas import (
    Analysis,
    Conversation,
    ConversationListItem,
    InquiryStatus,
    Message,
    Urgency,
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _preview(text: str) -> str:
    return text if len(text) <= 60 else text[:59] + "…"


class InquiryStore:
    def __init__(self) -> None:
        self._conversations: dict[int, Conversation] = {}
        self._inquiry_ids = itertools.count(1)
        self._message_ids = itertools.count(1)
        # 같은 idempotency_key의 두 번째 요청은 이전 결과를 돌려준다
        self.results: dict[str, dict[str, Any]] = {}
        # 초안 채택·수정·거절 기록 (KPI용)
        self.draft_events: list[dict[str, Any]] = []

    def list(self, status: InquiryStatus | None = None) -> list[ConversationListItem]:
        items = [
            ConversationListItem.model_validate(
                c.model_dump(include=set(ConversationListItem.model_fields))
            )
            for c in self._conversations.values()
            if status is None or c.status == status
        ]
        return sorted(items, key=lambda c: (c.updated_at, c.id), reverse=True)

    def all(self) -> list[Conversation]:
        """집계용 전체 문의 (읽기 전용으로 쓴다)."""
        return list(self._conversations.values())

    def get(self, inquiry_id: int) -> Conversation | None:
        return self._conversations.get(inquiry_id)

    def create(
        self,
        *,
        context_id: str,
        tenant_id: str,
        user_id: int,
        customer_name: str,
        subject: str,
        order_id: str | None,
        order_no: str | None,
        first_message: str,
        analysis: Analysis,
    ) -> Conversation:
        stamp = now_iso()
        conversation = Conversation(
            id=next(self._inquiry_ids),
            tenant_id=tenant_id,
            context_id=context_id,
            user_id=user_id,
            customer_name=customer_name,
            subject=subject,
            order_id=order_id,
            order_no=order_no,
            status="open",
            updated_at=stamp,
            preview=_preview(first_message),
            awaiting_reply=True,
        )
        self._conversations[conversation.id] = conversation
        self._apply_analysis(conversation, analysis)
        self._append(conversation, "customer", first_message, analysis, stamp)
        return conversation

    def add_customer_message(self, inquiry_id: int, text: str, analysis: Analysis) -> Conversation:
        conversation = self._conversations[inquiry_id]
        self._apply_analysis(conversation, analysis)
        conversation.status = "open"
        self._append(conversation, "customer", text, analysis, now_iso())
        return conversation

    def add_agent_message(self, inquiry_id: int, text: str) -> Conversation:
        conversation = self._conversations[inquiry_id]
        self._append(conversation, "agent", text, None, now_iso())
        return conversation

    def set_status(self, inquiry_id: int, status: InquiryStatus) -> Conversation:
        conversation = self._conversations[inquiry_id]
        conversation.status = status
        conversation.updated_at = now_iso()
        return conversation

    def _append(
        self,
        conversation: Conversation,
        role: str,
        text: str,
        analysis: Analysis | None,
        stamp: str,
    ) -> None:
        conversation.messages.append(
            Message(
                id=next(self._message_ids),
                role=role,  # type: ignore[arg-type]
                text=text,
                analysis=analysis,
                created_at=stamp,
            )
        )
        conversation.updated_at = stamp
        conversation.preview = _preview(text)
        conversation.awaiting_reply = role == "customer"

    @staticmethod
    def _apply_analysis(conversation: Conversation, analysis: Analysis) -> None:
        if analysis.classification:
            conversation.category = analysis.classification.category
            conversation.sentiment = analysis.classification.sentiment
            urgency: Urgency = analysis.classification.urgency
            conversation.urgency = urgency
