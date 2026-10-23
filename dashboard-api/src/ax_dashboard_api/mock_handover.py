"""인사 탭(인수인계) 목 모드 데이터와 동작. 형식은 모두 계약(`ax_contracts.schemas.hr`)을 쓴다.

hr 에이전트에 인수인계 skill(`start_handover`, `answer_handover_question`)이 아직 없어서
목 모드에서만 응답한다. 실제 연결 때는 질문의 `context_id`로 hr `answer_handover_question`을
A2A로 부르고, 대기 중인 질문 목록은 hr의 읽기 skill로 받는다 (README "인사 탭").

완성도·빈 자리 수는 코드가 계산한다 (LLM 값이 아니다): 완성도 = (항목 수 - 빈 자리) / 항목 수.
"""

from __future__ import annotations

from ax_contracts.schemas.hr import (
    AnswerHandoverQuestionOutput,
    HandoverItem,
    HandoverQuestion,
    StartHandoverOutput,
)
from ax_dashboard_api.envelope import ApiError


def _completeness(item_count: int, gap_count: int) -> int:
    if item_count == 0:
        return 100
    return int(100 * (item_count - gap_count) / item_count)


def seed() -> list[StartHandoverOutput]:
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
            assignee_id="mem_kim",
            question="결제 서비스 배포 후 확인이 끝나지 않은 항목이 있나요?",
            gap_field="itm_1.verification",
        ),
        HandoverQuestion(
            question_id="hq_0002",
            context_id="ctx_hnd_0001",
            assignee_id="mem_kim",
            question="보상 단가 변경 결정의 적용 시점은 언제인가요?",
            gap_field="itm_4.effective_at",
        ),
    ]
    first = StartHandoverOutput(
        handover_id="hov_0001",
        status="input_required",
        items=items,
        completeness=_completeness(len(items), len(questions)),
        gap_count=len(questions),
        questions=questions,
    )
    second = StartHandoverOutput(
        handover_id="hov_0002",
        status="ready",
        items=items[:2],
        completeness=100,
        gap_count=0,
        questions=[],
    )
    return [first, second]


class HandoverBoard:
    """메모리 상태. 테스트는 새 보드로 바꿔 끼운다."""

    def __init__(self, handovers: list[StartHandoverOutput] | None = None) -> None:
        self.handovers = handovers if handovers is not None else seed()
        self._answered: dict[str, str] = {}  # question_id -> answer
        self._by_key: dict[str, tuple[str, AnswerHandoverQuestionOutput]] = {}

    def list_handovers(self) -> list[StartHandoverOutput]:
        return self.handovers

    def get(self, handover_id: str) -> StartHandoverOutput:
        found = next((h for h in self.handovers if h.handover_id == handover_id), None)
        if found is None:
            raise ApiError(404, "NOT_FOUND", f"인수인계 {handover_id}를 찾을 수 없다")
        return found

    def pending_questions(self) -> list[HandoverQuestion]:
        return [q for h in self.handovers for q in h.questions]

    def answer(
        self, question_id: str, answer: str, idempotency_key: str
    ) -> AnswerHandoverQuestionOutput:
        previous = self._by_key.get(idempotency_key)
        if previous is not None:
            if previous[0] != question_id:  # 같은 키를 다른 질문에 쓰면 충돌이다
                raise ApiError(409, "IDEMPOTENCY_CONFLICT", "같은 키가 다른 질문에 쓰였다")
            return previous[1]  # 같은 답변을 두 번 보내도 한 번만 반영한다
        if question_id in self._answered:
            raise ApiError(409, "INVALID_STATE", f"질문 {question_id}에는 이미 답변했다")
        handover = next(
            (h for h in self.handovers if any(q.question_id == question_id for q in h.questions)),
            None,
        )
        if handover is None:
            raise ApiError(404, "NOT_FOUND", f"질문 {question_id}를 찾을 수 없다")

        handover.questions = [q for q in handover.questions if q.question_id != question_id]
        handover.gap_count = len(handover.questions)
        handover.completeness = _completeness(len(handover.items), handover.gap_count)
        if handover.gap_count == 0:
            handover.status = "ready"
        self._answered[question_id] = answer
        output = AnswerHandoverQuestionOutput(
            handover_id=handover.handover_id,
            status=handover.status,
            completeness=handover.completeness,
            remaining_questions=list(handover.questions),
        )
        self._by_key[idempotency_key] = (question_id, output)
        return output


BOARD = HandoverBoard()
