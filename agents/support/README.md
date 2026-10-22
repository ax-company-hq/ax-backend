# agents/support
담당: 병환 · 포트: 8102
## 1. 역할
고객 문의를 접수하고, 고객 메시지마다 분류·정책 근거 검색·답변 초안 3가지(empathetic, concise, step_by_step)·가드 검사를 만들어 상담원 화면(dashboard-api 경유)에 제공한다.
- 분류와 문장은 LLM, 근거 점수·가드 검사·집계 숫자는 코드가 계산한다.
- 고객 원문은 `untrusted`로만 받고 PII(전화번호·이메일)를 가린 뒤 데이터 블록으로만 LLM에 넣는다.
- LLM 실패(미설정·스키마 검증 실패)는 고정 템플릿으로 대체하고 `fallback: true`로 알린다.
## 2. 실행 방법
`docker compose -f infra/compose.yml up support` / 목 모드: `SUPPORT_MOCK=1`
모델은 `.env`의 `SUPPORT_LLM_MODEL` 또는 `LLM_MODEL`로 정한다 (코드에 모델 이름을 쓰지 않는다).
## 3. 입출력 계약
`agent-card.json`의 skill 10개. 입력·출력 모델은 `src/support_agent/schemas.py`.

| skill | 등급 | 요청 가능 | 내용 |
| --- | --- | --- | --- |
| `list_inquiries` | read | dashboard-api, orchestrator | 문의 목록 (상태 필터) |
| `get_inquiry` | read | dashboard-api, orchestrator | 메시지·분석 포함 상세 |
| `check_reply` | read | dashboard-api, orchestrator | 답변 가드 검사 (`untrusted.text`) |
| `summarize_inquiry` | read | dashboard-api, orchestrator | 대화 요약 (저장 안 함) |
| `create_inquiry` | A | dashboard-api, shop-api, orchestrator | 문의 접수 + 첫 메시지 분석 |
| `add_customer_message` | A | dashboard-api, shop-api, orchestrator | 고객 메시지 추가 + 분석 |
| `send_reply` | A | dashboard-api, orchestrator | 답변 발송 (가드 통과 필수) |
| `reject_draft` | A | dashboard-api | 거절한 초안 기록 |
| `set_inquiry_status` | A | dashboard-api, orchestrator | 상태 변경 |
| `request_compensation` | A | dashboard-api, orchestrator | 보상 단가 판정 + 마케팅 `issue_coupon(compensation)` 직렬 요청 (DEP-CS-02) |

- 상태를 바꾸는 skill은 모두 `execute()`를 거치고, 실행되지 않으면(보류·거부·safety 무응답) 저장소를 바꾸지 않는다.
- 액션 이름은 skill id와 같다. safety 레지스트리에 등록이 필요하다 (`to:safety`).
- 같은 `idempotency_key`의 두 번째 요청은 이전 결과를 돌려준다.
- `request_compensation`은 단가를 코드 정책표(`compensation.py`)로 판정하고, 사건(`context_id`)별로 `sequence` 1부터 한 건씩 마케팅에 보낸다. 지급·`execute()`·재무 교차검증은 마케팅 쪽이라 support는 자기 상태를 바꾸지 않고 safety 등록도 필요 없다. 표에 없는 사유는 `not_eligible`. 번호는 마케팅이 소비한 경우(지급·재무 거절·보류·실행 실패)에만 올리고, 재무 검증 중(`working`)·번호/입력 오류·타임아웃·연결 실패는 같은 번호로 다시 보낸다. 같은 사건의 요청은 락으로 한 건씩 처리한다.
## 4. 의존
ax-contracts, ax-agent-template
## 5. 하지 않는 것
- 클레임 판정·현금 환불은 아직 없다 (5단계). 보상 쿠폰 지급은 마케팅이 한다.
- 저장소는 프로세스 메모리다. DB 연결 전까지 재시작하면 사라진다.
- 정책 문서(KB)는 시드 조각이다. memory의 정책 문서 API가 생기면 교체한다.
- 외부 API를 직접 호출해 상태를 바꾸지 않는다.
