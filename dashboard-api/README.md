# dashboard-api
담당: 병환 · 포트: 8020
## 1. 역할
상담원 화면(ax-frontend `dashboard-web`)이 쓰는 API 서버. LLM 없이 코드로만 돈다.
- 문의·클레임·보상 큐는 support 에이전트(A2A skill), 주문·후기는 shop-api, KB는 memory, 집계는 공통 로그 저장소에서 받아 화면 형태로 돌려준다.
- 문의(`/conversations*`)는 목 모드가 아니면 support 에이전트 skill을 A2A로 부른다 (3단계, 아래 "문의 API 연결"). 나머지는 아직 목 데이터다.
- 현재(1~2단계)는 목 데이터로 읽기·쓰기 API를 연다. 쓰기는 목 모드에서 메모리 데이터만 바꾼다. 실제 연결 단계에서 승인·지급 계열은 support의 `execute()`를 거치도록 바꾼다.
## 2. 실행 방법
`docker compose -f infra/compose.yml up dashboard-api` / 목 모드: `DASHBOARD_MOCK=1`
목 모드가 아니면 문의·클레임·보상 큐·KPI 경로는 support를 부르고, 후기·집계(`/metrics`)·고객·KB·주문·인수인계 경로는 `503 UPSTREAM_NOT_CONNECTED`를 돌려준다.
에이전트 토큰은 `DASHBOARD_API_TOKEN`, support 주소는 `AX_SUPPORT_URL`(기본 `http://support:8102`).
## 3. 입출력 계약
응답은 성공 `{data, meta}`, 실패 `{error: {code, message}}`. 모델은 `schemas.py`이고 OpenAPI는 `contracts/openapi/dashboard-api.json`으로 CI가 내보낸다.

### 엔드포인트 (GET)
| 경로 (`/api/dashboard` 아래) | 옛 경로 (`/api/admin` 등) |
| --- | --- |
| `/health` | `/api/health` |
| `/users` | `/api/users` |
| `/metrics?days` | `/admin/dashboard?days` |
| `/kpis` | `/admin/kpi` |
| `/kb-chunks` | `/admin/kb` |
| `/conversations`, `/conversations/{id}` | `/admin/conversations`, `/admin/conversations/{id}` |
| `/orders?user_id` | `/admin/orders?user_id` |
| `/claims?status`, `/claims/stats`, `/claims/{id}` | `/admin/claims...` |
| `/queue-entries` | `/admin/queue` |
| `/reviews` | `/admin/reviews` |

### 엔드포인트 (POST, 목 모드)
| 경로 (`/api/dashboard` 아래) | 동작 |
| --- | --- |
| `/conversations` | 새 문의 만들기 (201) |
| `/conversations/{id}/customer-messages`, `/agent-messages` | 메시지 추가 (201). 상담원 답변은 가드(환불 단정 문구)에 걸리면 422 `REPLY_BLOCKED` |
| `/conversations/{id}/check`, `/reject-draft`, `/summary`, `/status` | 답변 검사, 초안 거절, 요약, 상태 변경 |
| `/claims/{id}/approve`, `/reject`, `/receive`, `/inspection` | 클레임 처리. 상태가 맞지 않으면 409 `INVALID_STATE` |
| `/queue-entries/process` | 준비된 보상 큐 항목 지급 처리 |
| `/reviews/{id}/status` | 후기 노출·숨김 |

CORS 허용 출처는 `DASHBOARD_CORS_ORIGINS`(쉼표 구분, 기본 `http://localhost:3001,http://127.0.0.1:3001`)로 정한다.

### 문의 API 연결 (목 모드가 아닐 때)
| 경로 | support skill |
| --- | --- |
| GET `/conversations`, `/conversations/{id}` | `list_inquiries`, `get_inquiry` |
| POST `/conversations` | `create_inquiry` (제목·본문은 `untrusted`) |
| POST `/conversations/{id}/customer-messages` | `add_customer_message` |
| POST `/conversations/{id}/check` | `check_reply` |
| POST `/conversations/{id}/agent-messages` | `send_reply` (가드 차단이면 422 `REPLY_BLOCKED`) |
| POST `/conversations/{id}/reject-draft`, `/summary`, `/status` | `reject_draft`, `summarize_inquiry`, `set_inquiry_status` |

요청마다 새 `ctx_req_<hex>` 사건 ID를 붙이고, 상태를 바꾸는 skill에는 `idempotency_key`(context_id + skill + 대상)를 붙인다. 오류 매핑: `not_found`→404, `not_executed_*`(보류·거부)→409 `NOT_EXECUTED`, 타임아웃→504 `UPSTREAM_TIMEOUT`, 거부→403 `UPSTREAM_REJECTED`, 그 밖의 실패→502 `UPSTREAM_ERROR`.
`POST /conversations`는 선택 필드 `customer_name`을 support에 넘긴다 (없으면 "고객 <user_id>"). 고객 목록(`/users`)은 shop-api 연결 전까지 목 전용이다.

### 클레임·보상 큐·KPI 연결 (목 모드가 아닐 때)
| 경로 | support skill |
| --- | --- |
| GET `/claims?status`, `/claims/stats`, `/claims/{id}` | `list_claims`, `claim_stats`, `get_claim` |
| GET `/queue-entries` | `claim_queue` |
| GET `/kpis` | `inquiry_overview` (`kpis`만 쓴다) |
| POST `/claims/{id}/approve`, `/reject` | `decide_claim` (메모는 `untrusted.note`) |
| POST `/claims/{id}/receive`, `/inspection` | `receive_return`, `inspect_return` |
| POST `/queue-entries/process` | `process_claim_queue` (`refund_request`로 지급, 재무 교차검증) |

support 출력은 `claim_mapping.py`에서 화면 모델로 바꾼다 (값을 지어내지 않고, support에 없는 값은 비우거나 0).
- 상태: support의 `approved`(승인됐고 지급 대기)는 화면에 없어서 `return_received`("입고 완료·처리 대기")로 보낸다. 상태 필터는 변환 뒤에 건다.
- 검수 값: 화면 `ok`·`mismatch`·`pending` ↔ support `pass`·`fail`·`partial`·없음 (`mismatch` 요청은 `fail`로 보낸다, 모르는 값은 422).
- 규칙·공제 코드와 감사 액션 이름은 화면 라벨 표(`RULE_KO`, `AUDIT_KO`)의 키에 맞춘다 (대응이 없는 코드는 그대로 둔다).
- 거절된 클레임은 환불 예정액·적립금을 0으로 보낸다.
- 비어 있는 값: `description`(고객 원문은 저장하지 않는다), `tracking`, `compensation`(보상 쿠폰은 `request_compensation`), `hold_reason`.
- 지급 연동이 막힌 항목(재무 검증 중·보류·거부)은 `process` 결과에서 `held`와 `ON_HOLD` 이유로 보인다. 같은 큐를 다시 처리해도 지급 요청(`refund_request`)의 키는 support가 클레임을 접수한 사건 기준으로 고정하므로(`<claim.context_id>:refund_request:claim:<id>`), 검증 중인 건이나 승인된 보류 건을 다시 처리하면 같은 판정이 이어지고 중복 지급되지 않는다. dashboard-api 호출마다 새 `ctx_req_`를 붙여도 상관없다.
- 감사 기록의 `agent_override`(상담원 승인) 라벨은 화면 표기에 맞춘 이름이다. `claim_stats.agent_overrides`는 자동 판정을 뒤집는 경우만 세는데 지금은 자동 판정을 바꿀 수 없어 항상 0이다.

### 인사 탭: 인수인계 질문 (목 모드 전용)
형식은 모두 계약 `ax_contracts.schemas.hr`를 그대로 쓴다. hr 에이전트에 인수인계 skill이 아직 없어서 목 모드(`DASHBOARD_MOCK=1`)에서만 응답하고, 아니면 `503 UPSTREAM_NOT_CONNECTED`다.
| 경로 (`/api/dashboard` 아래) | 응답 |
| --- | --- |
| GET `/handovers`, `/handovers/{handover_id}` | `StartHandoverOutput` (상태, 항목, 완성도, 빈 자리 수, 대기 질문) |
| GET `/handover-questions` | 답을 기다리는 `HandoverQuestion` 전체 (각 질문에 `context_id`) |
| POST `/handover-questions/{question_id}/answer` | 본문 `HandoverAnswerRequest`(`question_id`, `answer`, `idempotency_key`) → `AnswerHandoverQuestionOutput` (남은 질문, 새 완성도, 상태) |
- 완성도·빈 자리 수는 코드가 계산한다 (완성도 = (항목 수 - 빈 자리) / 항목 수). 마지막 질문에 답하면 `ready`가 된다.
- 같은 `idempotency_key`로 같은 질문에 다시 보내면 이전 결과를 돌려주고 한 번만 반영한다 (다른 질문에 쓰면 409 `IDEMPOTENCY_CONFLICT`). 이미 답한 질문은 409 `INVALID_STATE`, 없는 질문은 404, 본문 오류(빈 답변·빈 키·경로와 다른 `question_id`)는 422다.
- 실제 연결 계획: 답변은 새 요청으로 hr `answer_handover_question`(A2A, `deadline` 30초 그대로)에 보내고, 요청 `context_id`는 새 `ctx_req_`가 아니라 **질문의 `context_id`**를 그대로 쓴다 (`support_client.call()`에 `context_id` 인자 추가 필요). 대기 중인 질문 목록은 hr의 읽기 skill로 받는다.
- hr 쪽에 필요한 것: `start_handover`·`answer_handover_question` 구현과 Agent Card 등록(요청 가능: dashboard-api), 대기 질문·인수인계 목록 읽기 skill (계약에 없음).
- 답변 문장은 사람이 쓴 내부 입력이라 계약대로 `input`으로 보낸다. 화면은 텍스트로만 표시해야 한다.

## 4. 의존
ax-contracts, ax-agent-template
## 5. 하지 않는 것
- 다른 서비스 DB를 직접 읽지 않는다. 판매 사이트 환불·쿠폰 API를 직접 호출하지 않는다.
- LLM을 직접 호출하지 않는다 (답변 초안·가드·요약은 support 에이전트).
