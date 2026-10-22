"""공지 초안 생성과 게시 저장소. 프레임워크에 의존하지 않는다.

문안은 사실 필드로만 만드는 고정 템플릿이다(LLM은 아직 연결하지 않았다). 원인은 적지 않고,
약속 금액은 `promises[]`에서 코드가 옮겨 적는다. 외부 입력(`untrusted`)은 읽지 않는다.
"""

from __future__ import annotations

import hashlib
import itertools

from marketing_agent.schemas import DraftNoticeInput, NoticeDraft, NoticeView

_COMPONENT_LABEL = {"api": "서비스 접속", "db": "데이터 조회", "payment": "결제"}


def _template(data: DraftNoticeInput) -> str:
    label = _COMPONENT_LABEL.get(data.facts.component or "", "일부 서비스")
    if data.kind == "incident_opened":
        text = f"{label} 이용이 원활하지 않은 상황을 확인했습니다. 복구되는 대로 안내드리겠습니다."
    elif data.kind == "incident_resolved":
        text = f"{label} 이용이 정상으로 돌아왔습니다. 불편을 드려 죄송합니다."
    else:
        text = "새 상품이 등록되었습니다."
    for promise in data.promises:
        kind = "적립금" if promise.type == "points" else "쿠폰"
        text += f" ({promise.condition}: {kind} {promise.amount:,}원)"
    return text


def make_draft(context_id: str, data: DraftNoticeInput) -> NoticeDraft:
    """같은 입력이면 같은 초안(`draft_id`도 같다)을 만든다."""
    digest = hashlib.sha256(f"{context_id}:{data.model_dump_json()}".encode()).hexdigest()[:10]
    return NoticeDraft(
        draft_id=f"drf_{digest}",
        kind=data.kind,
        channel_code=data.channel_code,
        public_text=_template(data),
        facts=data.facts,
        promises=data.promises,
        valid_from=data.valid_from,
        valid_to=data.valid_to,
    )


class NoticeStore:
    """초안·게시 공지를 메모리에 둔다(목 모드). 같은 초안을 두 번 게시하면 같은 `notice_id`다."""

    def __init__(self) -> None:
        self._drafts: dict[str, NoticeDraft] = {}
        self._published: dict[str, tuple[str, NoticeDraft]] = {}  # notice_id → (context_id, draft)
        self._by_draft: dict[str, str] = {}
        self._counter = itertools.count(1)

    def save_draft(self, draft: NoticeDraft) -> None:
        self._drafts[draft.draft_id] = draft

    def get_draft(self, draft_id: str) -> NoticeDraft | None:
        return self._drafts.get(draft_id)

    def publish(self, context_id: str, draft: NoticeDraft) -> str:
        if draft.draft_id in self._by_draft:
            return self._by_draft[draft.draft_id]
        notice_id = f"ntc_{next(self._counter):04d}"
        self._published[notice_id] = (context_id, draft)
        self._by_draft[draft.draft_id] = notice_id
        return notice_id

    def view(self, notice_id: str) -> NoticeView:
        """게시된 공지만 보여 준다. 없으면 '확인할 수 없음'이다."""
        found = self._published.get(notice_id)
        if found is None:
            return NoticeView(found=False, unknown_reason="게시된 공지를 찾을 수 없음")
        _, draft = found
        return NoticeView(
            found=True,
            notice_id=notice_id,
            status="published",
            channel_code=draft.channel_code,
            public_text=draft.public_text,
            facts=draft.facts,
            promises=draft.promises,
            valid_from=draft.valid_from,
            valid_to=draft.valid_to,
        )
