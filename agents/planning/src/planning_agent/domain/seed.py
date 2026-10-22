"""초기 데이터 (프로토타입 `data.ts`의 `initialState`를 그대로 옮긴 것). tenant_id는 shop-demo.

시각은 프로토타입 표시값(KST)을 `kst()`로 UTC로 바꿔 저장한다.
"""

from __future__ import annotations

from planning_agent.domain.clock import kst
from planning_agent.domain.models import (
    PR,
    Activity,
    AutoLog,
    AutoPolicy,
    Decision,
    Doc,
    DocBlock,
    Incident,
    IncidentCandidate,
    Message,
    Para,
    ParaWarn,
    PrGate,
    Request,
    RollbackCheck,
    Spec,
    State,
    Task,
    Thread,
)


def _act(t: str, ctx: str, frm: str, to: str, point: str, kind: str, text: str, grade=None):
    return Activity(
        at=kst(t), context_id=ctx, from_=frm, to=to, point=point, kind=kind, grade=grade, text=text
    )


def _msg(who: str, dept: str, text: str, t: str | None = None) -> Message:
    return Message(who=who, dept=dept, text=text, at=kst(t) if t else None)


def initial_state() -> State:
    """매번 새 객체를 만든다 (호출한 쪽이 고쳐도 원본이 바뀌지 않게)."""
    return State(
        seq=0,
        outbox=[],
        requests=[
            Request(
                id="REQ-9",
                context_id="ctx_req_0009",
                from_="data",
                via="create_ticket",
                title="느린 카드사에서 결제가 실패한다",
                evidence="결제 실패 코드 집계 (데이터분석)",
                state="완료",
                spec="SPEC-5",
            ),
            Request(
                id="REQ-12",
                context_id="ctx_req_0012",
                from_="support",
                via="create_ticket",
                title="결제 실패 뒤 장바구니가 비워진다",
                evidence="반복 문의 42건 (고객지원 문의 분류)",
                state="기획 중",
                spec="SPEC-7",
            ),
            Request(
                id="REQ-13",
                context_id="ctx_req_0013",
                from_="hr",
                via="ticket_draft_propose (③)",
                title="결제 실패 안내 문구 전체 정비",
                evidence="같은 인수인계 질문 3주 연속 반복 (인사 결정 카드 card-0142)",
                state="접수",
            ),
            Request(
                id="REQ-14",
                context_id="ctx_req_0014",
                from_="data",
                via="개선 초안 (원인 추적 루프 ③)",
                title="결제 실패율이 같은 구간에서 반복 상승",
                evidence="metric.anomaly 3회, 14:00~14:10 구간 (데이터분석)",
                state="접수",
            ),
            Request(
                id="REQ-101",
                context_id="ctx_inc_0001",
                from_="infra",
                via="link_incident (재발 방지)",
                title="결제 경로 설정 변경은 카나리 배포 필수",
                evidence="사건 ctx_inc_0001, 롤백으로 복구 (DejaFix)",
                state="접수",
            ),
            Request(
                id="REQ-15",
                context_id="ctx_req_0015",
                from_="marketing",
                via="create_ticket",
                title="장바구니 보존 문구를 공지에도 반영",
                evidence="REQ-12와 같은 대상",
                state="중복 연결",
                duplicate_of="REQ-12",
            ),
        ],
        specs=[
            Spec(
                id="SPEC-7",
                title="결제 실패 후 장바구니 유지",
                request="REQ-12",
                status="초안",
                problem=(
                    "결제가 실패하면 장바구니가 비워져 고객이 다시 담아야 한다 (관련 문의 42건)"
                ),
                goal="결제 실패 뒤에도 장바구니를 유지해 관련 문의를 줄인다",
                paras=[
                    Para(
                        id="R1",
                        kind="req",
                        text="결제 실패 시 장바구니 상태 보존 (품절 상품은 품절 표시)",
                    ),
                    Para(
                        id="R2",
                        kind="req",
                        text="실패 안내 문구에 재시도 버튼",
                        warn=ParaWarn(
                            card="DC-31",
                            text='"재시도 버튼만 추가"는 9/30 기각된 안. R1과 함께일 때만 진행',
                        ),
                    ),
                    Para(
                        id="C1", kind="done", text="결제 실패를 일으킨 뒤 장바구니가 그대로 남는다"
                    ),
                    Para(
                        id="C2",
                        kind="done",
                        text="실패 안내의 재시도 버튼을 누르면 결제 화면으로 간다",
                    ),
                ],
                question="재고가 없어진 상품도 장바구니에 남겨 둘까요?",
                basis=[
                    "SPEC-3 장바구니 기획서",
                    "결제 실패 문의 통계 (데이터분석)",
                    "DC-31 기각 결정 카드 (인사)",
                ],
                human_sentences=0,
            ),
            Spec(
                id="SPEC-5",
                title="결제 타임아웃 설정 조정",
                request="REQ-9",
                status="확정",
                problem="결제 대기 시간이 짧아 느린 카드사에서 실패가 난다",
                goal="결제 성공률 유지",
                paras=[
                    Para(
                        id="R3",
                        kind="req",
                        text="결제 타임아웃 설정값 변경",
                        incident="ctx_inc_0001",
                    ),
                    Para(id="C5", kind="done", text="느린 카드사 모의 응답에서도 결제가 완료된다"),
                ],
                basis=["결제 실패 코드 집계 (데이터분석)"],
                human_sentences=2,
            ),
        ],
        tasks=[
            Task(
                id="T-9",
                spec="SPEC-5",
                para="R3",
                title="결제 타임아웃 설정 변경",
                owner="백엔드",
                state="완료",
            ),
        ],
        prs=[
            PR(
                id="PR #28",
                number=28,
                task="T-9",
                title="R3: 결제 타임아웃 설정 변경",
                branch="gen/t-9-payment-timeout",
                reviews=["통과"],
                gate=PrGate(result="통과", lines=14, files=1, paths="shop-api/payment/"),
                merged=True,
                merged_at=kst("10-21 14:02"),
                changed_paths=["shop-api/payment/config.py"],
            ),
        ],
        threads=[
            Thread(
                id="TH-7",
                on="SPEC-7",
                title="SPEC-7 · 결제 실패 후 장바구니 유지",
                messages=[
                    _msg(
                        "기획개발 에이전트",
                        "planning",
                        "빈 항목 질문: 재고가 없어진 상품도 장바구니에 남겨 둘까요?",
                    )
                ],
            ),
            Thread(
                id="TH-5",
                on="SPEC-5",
                title="SPEC-5 · 결제 타임아웃 설정 조정",
                messages=[
                    _msg(
                        "장재현 (데이터분석)",
                        "data",
                        "설정 변경 뒤 14:04부터 결제 실패율이 4배로 올랐습니다. "
                        "원인 판정은 인프라 쪽에서 해 주세요.",
                    )
                ],
            ),
            Thread(
                id="CH-team",
                on="",
                kind="team",
                title="기획개발팀",
                messages=[
                    _msg(
                        "기획개발 에이전트",
                        "planning",
                        "REQ-12 기획서 SPEC-7 초안을 올렸습니다. "
                        "확정 전에 R2 경고(DC-31)를 확인해 주세요.",
                        "10-22 09:15",
                    ),
                    _msg(
                        "노예진",
                        "planning",
                        "확인했어요. 품절 상품 처리 질문은 고객지원 답 받고 확정할게요.",
                        "10-22 09:18",
                    ),
                ],
            ),
            Thread(
                id="CH-support",
                on="",
                kind="dept",
                peer="support",
                title="고객지원",
                messages=[
                    _msg(
                        "고객지원 에이전트",
                        "support",
                        '"결제 실패 뒤 장바구니 비워짐" 문의가 '
                        "오늘 6건 더 들어왔습니다 (누적 48건).",
                        "10-22 09:40",
                    )
                ],
            ),
            Thread(id="CH-marketing", on="", kind="dept", peer="marketing", title="마케팅"),
            Thread(
                id="CH-hr",
                on="",
                kind="dept",
                peer="hr",
                title="인사",
                messages=[
                    _msg(
                        "인사 에이전트",
                        "hr",
                        '"재시도 버튼만 추가"는 9/30 기각된 안입니다 (DC-31). '
                        "장바구니 보존과 함께일 때만 진행을 권합니다.",
                        "10-22 09:13",
                    )
                ],
            ),
            Thread(id="CH-data", on="", kind="dept", peer="data", title="데이터분석"),
            Thread(id="CH-infra", on="", kind="dept", peer="infra", title="인프라(DejaFix)"),
        ],
        docs=[
            Doc(
                id="DOC-1",
                title="10/22 기획개발 정리",
                updated_at=kst("10-22 09:30"),
                blocks=[
                    DocBlock(id="b1", kind="h2", text="오늘 할 일"),
                    DocBlock(
                        id="b2",
                        kind="todo",
                        text="SPEC-7 품절 상품 처리 질문 답 받기 (고객지원)",
                        done=False,
                    ),
                    DocBlock(id="b3", kind="todo", text="SPEC-7 확정 후 PR 생성", done=False),
                    DocBlock(
                        id="b4", kind="todo", text="REQ-101 카나리 배포 규칙 검토", done=False
                    ),
                    DocBlock(id="b5", kind="h2", text="메모"),
                    DocBlock(
                        id="b6",
                        kind="p",
                        text="REQ-15(마케팅)는 REQ-12와 같은 요청이라 연결만 해 둠. "
                        "공지 문구는 REQ-12 완료 뒤 마케팅에 회신.",
                    ),
                ],
            ),
        ],
        decisions=[
            Decision(
                id="D-0",
                spec="SPEC-5",
                text="결제 타임아웃은 카나리 배포 후 전체 적용",
                reason="장애 재발 방지",
                alternatives="바로 전체 적용",
                from_="사후관리",
                sent_to_hr=True,
            ),
        ],
        incidents=[
            Incident(
                context_id="ctx_inc_0001",
                title="결제 API 응답 지연 (p95 2,400ms)",
                opened_at=kst("10-21 14:04"),
                candidates=[
                    IncidentCandidate(
                        pr="PR #28", spec="SPEC-5", para="R3", merged_at=kst("10-21 14:02")
                    )
                ],
                rollback_check=RollbackCheck(
                    target="shop-api v2.4.1",
                    result="승인",
                    why="장애 2분 전 배포, 그 사이 PR #28 머지",
                ),
                linked=True,
                follow_up="REQ-101",
            ),
        ],
        activity=[
            _act(
                "10-21 14:04",
                "ctx_inc_0001",
                "orchestrator",
                "planning",
                "incident.opened",
                "이벤트",
                "결제 API 장애 사건 개시",
            ),
            _act(
                "10-21 14:05",
                "ctx_inc_0001",
                "infra",
                "planning",
                "find_related_prs",
                "A2A",
                "장애 직전 머지 PR 후보 1건 회신 (PR #28, SPEC-5 R3)",
                "read",
            ),
            _act(
                "10-21 14:06",
                "ctx_inc_0001",
                "data",
                "planning",
                "find_related_prs",
                "A2A",
                "이상 구간 14:00~14:10 직전 배포 질의 → PR #28",
                "read",
            ),
            _act(
                "10-21 14:07",
                "ctx_inc_0001",
                "safety",
                "planning",
                "verify_rollback_target",
                "A2A",
                "rollback_deployment(B) 교차검증 → 승인",
                "read",
            ),
            _act(
                "10-21 14:09",
                "ctx_inc_0001",
                "planning",
                "planning",
                "link_incident",
                "A2A",
                "SPEC-5 R3에 장애 표시, 재발 방지 요청 REQ-101 등록",
                "A",
            ),
            _act(
                "10-21 14:10",
                "ctx_inc_0001",
                "infra",
                "hr",
                "record_case",
                "A2A",
                "kind rollback_executed (인프라 → 인사)",
                "A",
            ),
            _act(
                "10-22 09:12",
                "ctx_req_0012",
                "support",
                "planning",
                "create_ticket",
                "A2A",
                "반복 문의 42건으로 변경 요청 REQ-12 등록",
                "A",
            ),
            _act(
                "10-22 09:13",
                "ctx_req_0012",
                "planning",
                "hr",
                "check_plan",
                "A2A",
                '② "재시도 버튼만 추가" 기각 이력 질의 → warn (DC-31)',
                "read",
            ),
            _act(
                "10-22 09:14",
                "ctx_req_0012",
                "planning",
                "planning",
                "write_spec",
                "A2A",
                "SPEC-7 초안 작성, 경고를 R2에 첨부, 질문 1개",
                "A",
            ),
            _act(
                "10-22 09:20",
                "ctx_req_0013",
                "hr",
                "planning",
                "ticket_draft_propose",
                "A2A",
                "③ 개선 초안 수신 → 요청함 REQ-13",
                "A",
            ),
            _act(
                "10-22 09:31",
                "ctx_req_0014",
                "data",
                "planning",
                "개선 초안 (루프 ③)",
                "A2A",
                "반복 지표 이상 초안 수신 → 요청함 REQ-14",
                "A",
            ),
            _act(
                "10-22 09:40",
                "ctx_req_0015",
                "marketing",
                "planning",
                "create_ticket",
                "A2A",
                "REQ-12와 같은 요청 → 새로 만들지 않고 연결",
                "A",
            ),
        ],
        drafts_in=2,
        drafts_accepted=0,
        warnings=1,
        first_pr_minutes=[38],
        auto_policy=AutoPolicy(
            level=1,
            paths="shop-web/",
            streak=0,
            history=[],
            log=[
                AutoLog(
                    at=kst("10-22 09:00"), text="L1 문구·표시 소규모(30줄·2파일)로 시작", by="사람"
                )
            ],
        ),
    )
