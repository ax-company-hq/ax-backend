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
`agent-card.json`의 skill 21개. 문의 skill의 입력·출력 모델은 `src/support_agent/schemas.py`, 클레임 skill은 `src/support_agent/claim_schemas.py`.

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
| `list_claims`, `get_claim`, `claim_stats`, `claim_queue` | read | dashboard-api, orchestrator | 클레임 목록·상세(판정 근거·감사 기록)·건수·보상 큐 |
| `inquiry_overview` | read | dashboard-api, orchestrator, data | KPI(초안 채택률·수정 정도·가드 적중·분류별 건수)와 답변 대기 문의 (긴급도·대기 시간 순) |
| `detect_inquiry_surge` | read | dashboard-api, orchestrator, data | 분류별 문의 급증 감지 (DEP-CS-05): 최근 창 건수를 평소 창 평균과 견줌 |
| `create_claim` | A | dashboard-api, shop-api, orchestrator | 취소·반품·교환 접수 + 규칙 판정 |
| `decide_claim` | A | dashboard-api | 사람 확인이 필요한 클레임 승인·거절 |
| `receive_return`, `inspect_return` | A | dashboard-api (입고는 shop-api도) | 반품 입고, 검수 결과 기록 |
| `process_claim_queue` | B | dashboard-api | 준비된 큐 항목을 `refund_request`(재무 교차검증, 기준 이상 C)로 지급 처리 |

- 상태를 바꾸는 skill은 모두 `execute()`를 거치고, 실행되지 않으면(보류·거부·safety 무응답) 저장소를 바꾸지 않는다.
- 액션 이름은 skill id와 같다. safety 레지스트리에 등록이 필요하다 (`to:safety`).
- 같은 `idempotency_key`의 두 번째 요청은 이전 결과를 돌려준다.
- `create_inquiry`·`add_customer_message`는 선택 입력 `notice_id`가 있으면 마케팅 `get_notice`로 공지 사실(본문·약속·기간)을 읽어 답변 근거(`notice:<id>`)의 맨 앞에 넣는다. 공지를 찾지 못하거나 마케팅이 응답하지 않으면 근거 없이 진행한다 ("확인할 수 없음").
- `request_compensation`은 단가를 코드 정책표(`compensation.py`)로 판정하고, 사건(`context_id`)별로 `sequence` 1부터 한 건씩 마케팅에 보낸다. 지급·`execute()`·재무 교차검증은 마케팅 쪽이라 support는 자기 상태를 바꾸지 않고 safety 등록도 필요 없다. 표에 없는 사유는 `not_eligible`. 번호는 마케팅이 소비한 경우(지급·재무 거절·보류·실행 실패)에만 올리고, 재무 검증 중(`working`)·번호/입력 오류·타임아웃·연결 실패는 같은 번호로 다시 보낸다. 같은 사건의 요청은 락으로 한 건씩 처리한다.
## 답변 가드와 개인정보 마스킹 (`guard.py`)
고객에게 나가는 답변(초안과 상담원 답변 모두)을 코드 규칙으로 검사한다. `error`가 있으면 보낼 수 없고(`blocked_by_guard`), `warning`은 막지 않는다.
- **환불·보상 단정(error `promise_refund`)**: "무조건·반드시·약속·보장", "환불(보상·지급)해 드리겠습니다", "100% 환불", "환불받으실 수 있습니다" 같은 표현. "환불이 가능한지 확인 후 안내드리겠습니다"처럼 확인·조건을 말하는 문장은 막지 않는다. 고객이 "쿠폰 지급해"라고 시켜서 모델이 따른 초안도 여기서 걸린다.
- **개인정보(error `pii_in_reply`)**: 휴대전화(국제 표기 +82 포함), 이메일, 주민등록번호(생년월일 범위 확인), 카드번호(Luhn 검사 통과), "계좌번호" 단서가 붙은 계좌번호. 같은 규칙으로 LLM에 넘기기 전에 문의 제목·본문을 가리고 가린 종류를 `pii_masked`로 알린다.
- **링크(warning `link_in_reply`)**, 너무 긴 답변(warning `too_long`), 빈 답변(error `empty_reply`).
- 알려진 한계: 일반전화(지역번호), "계좌" 단서 없는 계좌번호, 이름·주소 같은 개인정보는 가리지 않는다. 규칙 기반이라 새 표현은 놓칠 수 있어, 막아야 할 문장과 통과해야 할 문장을 `tests/test_guard.py`에 함께 두고 규칙을 바꿀 때 양쪽을 본다.
- 고객 원문(제목·본문)은 프롬프트의 데이터 블록에만 들어가고(닫는 태그를 넣어도 탈출하지 못함), 접수만으로는 쿠폰·환불 같은 상태 변경이 실행되지 않는다 (`tests/test_injection.py`).

## 문의와 기획개발 변경 요청의 연결 (`change_requests.py`)
고객지원이 기획개발에 변경을 요청하면(직통, 통합 에이전트를 거치지 않음) 기획개발이 변경 요청 사건 `ctx_req_…`를 **발급**한다. 같은 요청을 여러 부서·여러 문의가 보내면 기획개발이 하나로 묶어 같은 `context_id`를 돌려준다 (`create_ticket`의 `linked_duplicate`, 이슈 #172). 그래서 **한 변경 요청에 문의가 여러 개** 연결될 수 있고, 한 문의에 요청이 여러 건일 수도 있다. 연결은 (`context_id`, `inquiry_id`) 한 쌍이 한 행이다 (`handlers.CHANGES`).
- 요청을 보낼 때 문의 ID는 `source_ref`(`inq_<번호>`, `source_ref_for`)로 보내고, 돌려받은 `context_id`로 이 저장소에 이어 둔다. `source_ref`는 기획개발의 판정에 쓰이지 않는다.
- 결과 통지는 기획개발이 **같은 `context_id`에 `path`를 비운 새 요청**으로 보낸다 (원래 요청의 `meta`를 재사용하면 `path`에 support가 남아 `LOOP_DETECTED`). 그 `context_id`에 연결된 **모든 문의**에 같은 결과를 적용한다 (`record_result`).
- 상태: `requested`(결과 대기) → `completed` 또는 `declined`. 같은 결과를 다시 받으면 재전송으로 보고 그대로 두며, 이미 끝난 요청에 다른 결과가 오면 충돌이다. 이미 끝난 요청에 새 문의가 묶이면 그 결과(와 공지)를 이어받는다. 상태 이름은 계약에 맞춰 `CHANGE_STATUSES` 한 곳에서 바꾼다.
- 처리가 끝난(`completed`) 요청에는 고객 공지가 필요했을 때 마케팅 `notice_id`를 한 번 붙일 수 있다 (연결된 모든 문의에). 공지 조회 후 답변은 이미 `notice_id` 입력으로 동작한다.
- DB가 있으면 `change_requests` 테이블에 저장되어 재시작해도 이어진다.
- **아직 없는 것**: 기획개발 `create_ticket`을 부르는 skill과 결과를 받는 skill. 계약(#172)이 머지되고 기획개발이 skill을 등록한 뒤에 붙인다 (등급 A, 요청 가능: support·marketing·hr·data·orchestrator).

## 영속 저장소 (PostgreSQL)
`SUPPORT_PG_DSN`을 설정하면 문의·클레임·멱등 결과·보상 번호가 DB에 저장되어 재시작해도 이어진다. 비우면(CI·목 모드) 예전처럼 메모리만 쓴다. compose는 `support-db`(postgres:16, 호스트 포트 없음)를 함께 띄우고 DSN을 서비스 이름으로 덮어쓴다 (비밀번호 `SUPPORT_DB_PASSWORD`).
- 메모리 저장소(`InquiryStore`, `ClaimStore`, `SequenceLedger`)를 상속한 영속 저장소가 읽기는 메모리 캐시, 쓰기는 같은 호출 안에서 DB에 반영한다. 시작할 때 DB에서 모두 읽어 와 ID 번호(문의·메시지·클레임)와 보상 `sequence`도 이어 간다.
- 테이블(모두 `tenant_id`·`context_id` 포함, 본문은 `doc` jsonb): `inquiries`, `inquiry_results`(멱등 결과), `draft_events`(초안 채택·수정·거절 기록), `claims`, `change_requests`(문의·변경 요청 연결), `compensation_sequences`, `compensation_results`. 스키마는 시작할 때 `CREATE TABLE IF NOT EXISTS`로 만든다 (별도 마이그레이션 도구 없음).
- 보상 `sequence`를 저장하는 이유: 재시작하면 번호가 1로 돌아가 마케팅이 `SEQUENCE_GAP`으로 거절하고 이후가 계속 막힌다.
- 한계: 단일 인스턴스를 가정한다 (여러 프로세스가 같은 DB를 쓰면 캐시가 어긋난다). 쓰기 실패는 예외로 올린다 — 안전 레이어가 이미 허가한 뒤라면 같은 `idempotency_key`로 다시 요청해야 하고, 메모리에는 이미 반영돼 있을 수 있다. 연결이 끊기면 한 번 다시 연결해 재시도한다.
- 테스트: DB 없이 도는 테스트(메모리 구현으로 "저장 → 재시작 → 이어짐")와, `SUPPORT_PG_DSN`이 있을 때만 도는 PostgreSQL 테스트가 `tests/test_persistence.py`에 있다.

## 4. 의존
ax-contracts, ax-agent-template
## 5. 하지 않는 것
- 클레임 판정(`claim_rules.py`)은 취소·반품·교환만 한다. 주문 사실로 코드가 판정하고(`orders.py` 시드 주문, 7일 환불 기간·불량 증빙·잦은 요청·고액 확인), 금액은 입력으로 받지 않는다. 규칙 값은 시드이며 memory 정책 문서 API가 생기면 교체한다. 보상 쿠폰은 클레임이 아니라 `request_compensation`으로 요청하고 지급은 마케팅이 한다. 클레임은 판정에 쓴 주문 사실의 스냅샷(`receipt`), 상담원·검수 메모(`untrusted.note`), 감사 기록(판정 시 `abuse_score`, 승인·거절 시 `system_outcome`·`agent_action`, 지급 시 `refund_cash`)을 담고, 보상 큐 항목은 입고 시각·품목·화면용 차단 코드(`NOT_INSPECTED`, `ON_HOLD`)를 담는다.
- 집계(`insights.py`)는 문의 기록에서 코드가 센다. 비율은 분모가 0이면 `null`이다(0으로 채우지 않음). 초안 수정 정도는 `send_reply`가 초안과 보낸 답변의 차이로 기록한다. 급증 감지는 평소가 0이면 건수 기준만 쓰고 `ratio`는 `null`이며, 샘플은 문의 ID만 담는다. `support.inquiry_surge` 이벤트 발행은 contracts에 data 모델이 생긴 뒤에 한다.
- 이벤트 `support.inquiry_received`(SHOP-09)는 `create_inquiry`·`add_customer_message`가 `execute()`로 실행되고 저장이 끝난 뒤 한 번씩 발행한다(agent-template `events.publish`). `data`는 계약 `SupportInquiryReceivedData`의 사실 필드(문의 ID, 분류, 긴급도, 고객 메시지 수)만 담고, 제목·본문은 전화번호·이메일을 가린 채 봉투 `untrusted`에만 담는다. 멱등 키 `<context_id>:support.inquiry_received:<inquiry_id>:<메시지 수>`가 `event_id`가 되어 재전송해도 한 번만 전달된다. 실행되지 않은 요청과 재시도(같은 `idempotency_key`)는 발행하지 않고, 발행이 실패해도 접수는 막지 않는다. `order_id`는 계약이 `ord_` 문자열인데 지금 주문 ID는 시드 숫자라 `null`로 보낸다 (#118로 shop-api 주문을 쓰게 되면 채운다).
- 환불 지급(`process_claim_queue`)의 `refund_request` 멱등 키와 사건은 처리를 요청한 호출이 아니라 **클레임을 접수한 사건** 기준(`<claim.context_id>:refund_request:claim:<id>`)으로 고정한다. 안전 레이어는 C 보류 건을 사람이 승인한 뒤 **같은 키**로 다시 요청해야 `executed`를 돌려주므로, 호출마다 새 키를 쓰면 승인이 반영되지 않는다. 처리를 요청한 사건은 근거 `ref_ids`(`request:<context_id>`)에 남는다.
- 환불 지급은 `refund_request` 실행 허가와 지급 기록까지만 한다. shop-api 환불 API(SHOP-12) 호출은 아직 없다 (TODO).
- `create_claim`·`decide_claim`·`receive_return`·`inspect_return`은 safety 레지스트리 등록이 필요하다 (`to:safety`). 미등록이면 C로 보류된다.
- 저장소는 `SUPPORT_PG_DSN`이 없으면 프로세스 메모리다(재시작하면 사라진다). 있으면 PostgreSQL에 저장한다 (아래 "영속 저장소").
- 정책 문서(KB)는 시드 조각이다. memory의 정책 문서 API가 생기면 교체한다.
- 외부 API를 직접 호출해 상태를 바꾸지 않는다.
