"""인사 탭(인수인계·후보 카드) 목 모드 데이터와 동작. 형식은 계약(`ax_contracts.schemas.hr`)을 쓴다.

목 모드가 아니면 `hr_client`로 hr 에이전트 skill을 부른다(`routes.py`). 목은 그 응답과 같은 모양과
같은 규칙을 따른다: 같은 답을 다시 보내면 한 번만 반영하고 다른 답이면 `CONFLICT`, 후보 카드는 같은
결정이면 그대로 성공하고 반대 결정이면 `CONFLICT`.

완성도·빈 자리 수는 코드가 계산한다 (LLM 값이 아니다): 완성도 = (항목 수 - 빈 자리) / 항목 수.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from ax_contracts.schemas.hr import (
    AnswerHandoverQuestionOutput,
    DecideCandidateCardOutput,
    HandoverItem,
    HandoverQuestion,
    HandoverView,
)
from ax_contracts.schemas.record import Card
from ax_dashboard_api.envelope import ApiError

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


def _completeness(item_count: int, gap_count: int) -> int:
    if item_count == 0:
        return 100
    return int(100 * (item_count - gap_count) / item_count)


def seed() -> list[HandoverView]:
    items = [
        HandoverItem(
            item_id="itm_1", kind="deployment", title="결제 서비스 배포", source_ref="pr_41"
        ),
        HandoverItem(
            item_id="itm_2", kind="ticket", title="환불 정책 문구 수정", source_ref="tkt_7"
        ),
        HandoverItem(item_id="itm_3", kind="pr", title="쿠폰 재시도 수정", source_ref="pr_52"),
        HandoverItem(
            item_id="itm_4", kind="decision", title="보상 단가 변경 결정", source_ref="card_9"
        ),
    ]
    questions = [
        HandoverQuestion(
            question_id="hq_0001",
            context_id="ctx_hnd_0001",
            assignee_id="emp_01",
            question="결제 서비스 배포 후 확인이 끝나지 않은 항목이 있나요?",
            gap_field="itm_1.verification",
        ),
        HandoverQuestion(
            question_id="hq_0002",
            context_id="ctx_hnd_0001",
            assignee_id="emp_01",
            question="보상 단가 변경 결정의 적용 시점은 언제인가요?",
            gap_field="itm_4.effective_at",
        ),
    ]
    first = HandoverView(
        handover_id="hov_0001",
        context_id="ctx_hnd_0001",
        owner_id="emp_01",
        successor_id="emp_02",
        area="payment",
        reason="transfer",
        status="input_required",
        completeness=_completeness(len(items), len(questions)),
        items=items,
        open_questions=questions,
        created_at=NOW,
        updated_at=NOW,
    )
    second = HandoverView(
        handover_id="hov_0002",
        context_id="ctx_asg_0001",
        owner_id="emp_03",
        successor_id="emp_04",
        area="search",
        reason="shift",
        status="ready",
        completeness=100,
        items=items[:2],
        created_at=NOW,
        updated_at=NOW,
    )
    return [first, second]


def seed_cards() -> list[Card]:
    return [
        Card(
            card_id="card_d2c8a53d7ee0",
            kind="decision",
            context_id="ctx_dec_0003",
            title="[결정] 야간 알림 기준을 낮추기로 했다",
            body="결정: 야간 알림 기준을 낮추기로 했다\n이유: 확인할 수 없음",
            tags=["decision", "area:search", "candidate"],
            author="support",
            confidence="low",
            created_at=NOW,
            candidate=True,
            extra={"area": "search", "outcome": "adopted"},
        ),
        Card(
            card_id="card_77aa10c2be31",
            kind="decision",
            context_id="ctx_dec_0004",
            title="[결정] 환불 문구에 배송비 안내를 넣기로 했다",
            body="결정: 환불 문구에 배송비 안내를 넣기로 했다\n이유: 확인할 수 없음",
            tags=["decision", "area:payment", "candidate"],
            author="support",
            confidence="low",
            created_at=NOW,
            candidate=True,
            extra={"area": "payment", "outcome": "adopted"},
        ),
    ]


class HandoverBoard:
    """메모리 상태. 테스트는 새 보드로 바꿔 끼운다."""

    def __init__(
        self, handovers: list[HandoverView] | None = None, cards: list[Card] | None = None
    ) -> None:
        self.handovers = handovers if handovers is not None else seed()
        self.cards = cards if cards is not None else seed_cards()
        self._answers: dict[str, str] = {}  # question_id -> answer
        self._owners: dict[str, str] = {}  # question_id -> handover_id
        self._keys: dict[str, str] = {}  # idempotency_key -> question_id
        self._decisions: dict[str, Literal["approve", "reject"]] = {}

    # ---- 인수인계
    def list_handovers(
        self, status: str | None = None, area: str | None = None
    ) -> list[HandoverView]:
        return [
            h
            for h in self.handovers
            if (status is None or h.status == status) and (area is None or h.area == area)
        ]

    def get(self, handover_id: str) -> HandoverView:
        found = next((h for h in self.handovers if h.handover_id == handover_id), None)
        if found is None:
            raise ApiError(404, "NOT_FOUND", f"인수인계 {handover_id}를 찾을 수 없다")
        return found

    def pending_questions(
        self, assignee_id: str | None = None, handover_id: str | None = None
    ) -> list[HandoverQuestion]:
        return [
            q
            for h in self.handovers
            if handover_id is None or h.handover_id == handover_id
            for q in h.open_questions
            if assignee_id is None or q.assignee_id == assignee_id
        ]

    def answer(
        self, question_id: str, answer: str, idempotency_key: str
    ) -> AnswerHandoverQuestionOutput:
        # 목은 hr보다 엄격하다: 같은 키를 다른 질문에 쓰면 클라이언트 실수라 막는다
        if self._keys.setdefault(idempotency_key, question_id) != question_id:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "같은 키가 다른 질문에 쓰였다")
        previous = self._answers.get(question_id)
        if previous is not None:  # 같은 답은 한 번만 반영하고 현재 상태를 돌려준다
            if previous != answer:
                raise ApiError(409, "CONFLICT", "이미 다른 답변이 반영된 질문이다")
            return self._output(self.get(self._owners[question_id]))
        handover = next(
            (
                h
                for h in self.handovers
                if any(q.question_id == question_id for q in h.open_questions)
            ),
            None,
        )
        if handover is None:
            raise ApiError(404, "NOT_FOUND", f"질문 {question_id}를 찾을 수 없다")
        handover.open_questions = [
            q for q in handover.open_questions if q.question_id != question_id
        ]
        handover.completeness = _completeness(len(handover.items), len(handover.open_questions))
        if not handover.open_questions:
            handover.status = "ready"
        handover.updated_at = NOW
        self._answers[question_id] = answer
        self._owners[question_id] = handover.handover_id
        return self._output(handover)

    @staticmethod
    def _output(handover: HandoverView) -> AnswerHandoverQuestionOutput:
        return AnswerHandoverQuestionOutput(
            handover_id=handover.handover_id,
            status=handover.status,
            completeness=handover.completeness,
            remaining_questions=list(handover.open_questions),
        )

    # ---- 후보 카드
    def candidate_cards(self, area: str | None = None) -> list[Card]:
        return [
            c
            for c in self.cards
            if c.card_id not in self._decisions and (area is None or c.extra.get("area") == area)
        ]

    def decide(
        self, card_id: str, decision: Literal["approve", "reject"]
    ) -> DecideCandidateCardOutput:
        if not any(c.card_id == card_id for c in self.cards):
            raise ApiError(404, "NOT_FOUND", f"카드 {card_id}를 찾을 수 없다")
        previous = self._decisions.get(card_id)
        if previous is not None and previous != decision:
            raise ApiError(409, "CONFLICT", "이미 반대 결정이 반영된 카드다")
        self._decisions[card_id] = decision
        return DecideCandidateCardOutput(
            card_id=card_id, status="active" if decision == "approve" else "retired"
        )


BOARD = HandoverBoard()
