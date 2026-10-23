# dashboard-api
담당: 병환 · 포트: 8020
## 1. 역할
상담원 화면(ax-frontend `dashboard-web`)이 쓰는 API 서버. LLM 없이 코드로만 돈다.
- 문의·클레임·보상 큐는 support 에이전트(A2A skill), 주문·후기는 shop-api, KB는 memory, 집계는 공통 로그 저장소에서 받아 화면 형태로 돌려준다.
- 문의(`/conversations*`)는 목 모드가 아니면 support 에이전트 skill을 A2A로 부른다 (3단계, 아래 "문의 API 연결"). 나머지는 아직 목 데이터다.
- 현재(1~2단계)는 목 데이터로 읽기·쓰기 API를 연다. 쓰기는 목 모드에서 메모리 데이터만 바꾼다. 실제 연결 단계에서 승인·지급 계열은 support의 `execute()`를 거치도록 바꾼다.
## 2. 실행 방법
`docker compose -f infra/compose.yml up dashboard-api` / 목 모드: `DASHBOARD_MOCK=1`
목 모드가 아니면 문의·클레임·보상 큐·KPI 경로는 support를, 인수인계·후보 카드 경로는 hr을 부르고, 후기·집계(`/metrics`)·고객·KB·주문 경로는 `503 UPSTREAM_NOT_CONNECTED`를 돌려준다.
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

요청마다 새 `ctx_ui_<hex>` 사건 ID(아래 "사건 ID")를 붙이고, 상태를 바꾸는 skill에는 `idempotency_key`(context_id + skill + 대상)를 붙인다. 오류 매핑: `not_found`→404, `not_executed_*`(보류·거부)→409 `NOT_EXECUTED`, 타임아웃→504 `UPSTREAM_TIMEOUT`, 거부→403 `UPSTREAM_REJECTED`, 그 밖의 실패→502 `UPSTREAM_ERROR`.
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
- support 클레임의 `order_id`·`order_item_id`·`buyer_id`는 문자열이다. 화면 모델은 주문·품목 ID를 숫자 또는 문자열로 받고, `user_id`는 `buyer_id`가 `usr_<숫자>`일 때만 그 숫자로 채운다(아니면 비움).
- 비어 있는 값: `description`(고객 원문은 저장하지 않는다), `tracking`, `compensation`(보상 쿠폰은 `request_compensation`), `hold_reason`.
- 지급 연동이 막힌 항목(재무 검증 중·보류·거부)은 `process` 결과에서 `held`와 `ON_HOLD` 이유로 보인다. 같은 큐를 다시 처리해도 지급 요청(`refund_request`)의 키는 support가 클레임을 접수한 사건 기준으로 고정하므로(`<claim.context_id>:refund_request:claim:<id>`), 검증 중인 건이나 승인된 보류 건을 다시 처리하면 같은 판정이 이어지고 중복 지급되지 않는다. dashboard-api 호출마다 새 `ctx_ui_`를 붙여도 상관없다.
- 감사 기록의 `agent_override`(상담원 승인) 라벨은 화면 표기에 맞춘 이름이다. `claim_stats.agent_overrides`는 자동 판정을 뒤집는 경우만 세는데 지금은 자동 판정을 바꿀 수 없어 항상 0이다.

### 인사 탭: 인수인계·후보 카드
형식은 모두 계약 `ax_contracts.schemas.hr`를 그대로 쓴다. 목 모드(`DASHBOARD_MOCK=1`)는 메모리 데이터로, 아니면 hr 에이전트 skill을 A2A로 부른다(`hr_client.py`, 요청자 `dashboard-api`, `deadline` 30초). hr 화면 계약 문서는 `docs/notes/hr/dashboard-api-contract.md`다.
| 경로 (`/api/dashboard` 아래) | hr skill | 응답 `data` |
| --- | --- | --- |
| GET `/handovers?status&area` | `list_handovers` | `HandoverView[]` (인계자·후임·영역·사유·완성도·항목·`open_questions`) |
| GET `/handovers/{handover_id}` | `list_handovers`에서 찾음 (단건 skill 없음) | `HandoverView`, 없으면 404 |
| GET `/handover-questions?assignee_id&handover_id` | `list_open_questions` | 답을 기다리는 `HandoverQuestion[]` (각 질문에 `context_id`) |
| POST `/handover-questions/{question_id}/answer` | `answer_handover_question` | 본문 `HandoverAnswerRequest`(`question_id`, `answer`, `idempotency_key`) → `AnswerHandoverQuestionOutput` |
| GET `/candidate-cards?area` | `list_candidate_cards` | 후보 `Card[]` (`candidate: true`, `confidence` low·mid·high) |
| POST `/candidate-cards/{card_id}/decision` | `decide_candidate_card` | 본문 `{decision: approve|reject, note?}` → `{card_id, status: active|retired}` |
| GET `/onboardings?member_id&area` | `list_onboardings` | `OnboardingView[]` (단계·진행률) |
| POST `/onboardings/{onboarding_id}/steps/{step_id}` | `update_onboarding_step` | 본문 `{done: true|false}`(기본 true) → `StartOnboardingOutput` (진행률은 다시 센 값) |
| GET `/assignment-changes?status&area&member_id` | `list_changes` | `ChangeView[]` (교대·발령·퇴직·입사 요청, 확인 목록) |
| GET `/roster?area` | `get_roster` | `GetRosterOutput` 한 개 (`entries`, `gap_areas`, `collected_at`). 개인 정보 없음 |
| GET `/hr-activity/{context_id}` | `get_audit_timeline` | `AuditEntry[]` (hr이 한 일, 시간순) |
| POST `/plan-checks` | `check_plan` | 본문 `{draft: {title, summary, area, kind}}` → `CheckPlanOutput` (읽기, 저장 안 함) |
- 답변은 질문이 속한 사건의 `context_id`(질문 목록에서 받은 값)를 그대로 `meta.context_id`로 보낸다. 새로 만들지 않는다. 그래서 hr 감사 기록이 그 사건으로 모인다. 이미 답해서 대기 목록에서 빠진 질문(같은 답의 재전송)은 사건을 알 수 없어 새 `ctx_ui_`로 보내고, hr이 질문 ID로 찾아 한 번만 반영한다. 멱등 키는 본문의 `idempotency_key`다.
- 같은 답을 다시 보내면 한 번만 반영되고 성공한다. **다른 답**을 다시 보내면 409 `CONFLICT`다. 없는 질문·카드·인수인계는 404 `NOT_FOUND`, 본문 오류(빈 답변·빈 키·경로와 다른 `question_id`·모르는 결정 값)는 422다.
- 후보 카드: 같은 결정을 다시 하면 성공하고 반대 결정이면 409 `CONFLICT`다. 실행은 hr이 `decide_candidate_card`(A)로 한다. 대시보드는 hr skill만 부르고 직접 `execute()`하지 않는다. 승인 권한 규칙은 아직 없다(누구나 호출 가능, 정하면 여기서 막는다).
- hr 결과를 오류로 바꾸는 표(`hr_client.unwrap`): `rejected/INVALID_INPUT` 422, 그 밖의 `rejected` 403 `UPSTREAM_REJECTED`, `UNKNOWN_*` 404 `NOT_FOUND`, `CONFLICT`·`NOT_CANDIDATE`·`HANDOVER_CLOSED` 등 409 `CONFLICT`, `gate_*` 409 `NOT_EXECUTED`, `failed` 502 `UPSTREAM_ERROR`, `canceled` 504 `UPSTREAM_TIMEOUT`. `input-required`(사람 입력 대기)는 오류가 아니라 출력 그대로다.
- 목과 실제는 같은 모양이다. 예전 목의 `StartHandoverOutput`(`gap_count`, `questions`)은 hr 화면 계약의 `HandoverView`(`open_questions`)로 바뀌었다. 화면 타입을 다시 만들어야 한다.
- 완성도·진행률 같은 숫자는 hr이 계산한 값이다 (목은 코드가 계산: 완성도 = (항목 수 - 빈 자리) / 항목 수).
- 답변 문장은 사람이 쓴 내부 입력이라 계약대로 `input`으로 보낸다. 화면은 텍스트로만 표시해야 한다.
- 온보딩 단계 완료는 이미 그 상태면 그대로 성공하고(`duplicate`), 없는 온보딩·단계는 404다. 변경(`assignment-changes`)의 시작·확정·퇴직 확정은 통합 에이전트만 부르고 화면은 조회만 한다 (hr Agent Card가 dashboard-api를 허용하지 않음). 계획 비교의 `draft`는 화면에서 사람이 쓴 요약이라 `input`으로 보낸다 (`untrusted` 비움).
- 목 모드 데이터는 `mock_handover.py`(인수인계·후보 카드)와 `mock_hr.py`(온보딩·변경·명부·감사·계획 비교)에 있다. 목의 진행률은 hr과 같은 규칙(완료한 단계 / 전체 단계)으로 코드가 센다.

### 소통 탭: 부서 간 A2A 대화
| 경로 (`/api/dashboard` 아래) | 응답 `data` |
| --- | --- |
| GET `/communications?dept&context_id` | 한 줄(`CommunicationLine`)의 배열, 시간 오름차순. `meta.total`, `meta.truncated` |
- 읽기는 공통 로그 읽기 조회(`CommonLogQuery`/`CommonLogResult`)만 쓴다. 쓰는 쪽은 agent-template이 A2A 요청·응답을 자동 기록한다(#249). 읽는 곳은 `AX_COMMON_LOG_DB`(compose의 공유 볼륨, #271) SQLite 파일이고, 그 값이 없으면 목 모드가 아닐 때 503 `UPSTREAM_NOT_CONNECTED`다.
- 한 줄에는 코드가 만든 필드만 담는다: `from_agent` → `to_agent`, `skill`, `outcome`(`completed`·`rejected`·`failed`·`held`·`needs_human`·`in_progress`), `occurred_at`, `context_id`, `execution_id`. `payload`와 고객 원문은 넣지 않는다. `execution_id`로 안전 레이어 결정과 이어 "보류·거부"를 한 줄에 보여 줄 수 있다.
- `dept`는 그 부서가 보냈거나 받은 줄만, `context_id`는 같은 사건끼리 묶어 본다. `dept`는 에이전트 이름(`support`·`marketing`·`hr`·`data`·`finance`·`orchestrator`·`safety`·`planning`)이고, DejaFix 어댑터는 팀 기준 이름 `dejafix`(또는 `infra-dejafix`)와 에이전트 이름 `infra` 중 무엇으로 보내도 같은 줄을 돌려준다. 응답의 `from_agent`·`to_agent`는 에이전트 이름 그대로라 DejaFix는 `infra`로 나온다. 방향은 요청(`request_received`)이면 요청자 → 받은 쪽, 결과·결정이면 기록한 쪽 → 요청자다. 이벤트 발행·기억 참조는 부서 간 대화가 아니라서 뺀다. 이관 큐로 넘긴 건(`escalated`)은 `needs_human`으로 보인다.

### AI 대화방: 총괄 사건·안전 관문 기록 (읽기 전용 중계)
| 경로 (`/api/dashboard` 아래) | 원본 | 응답 `data` |
| --- | --- | --- |
| GET `/incidents` | 총괄 `GET /api/incidents` | 사건 배열 (단계·이벤트·`pending_holds`) |
| GET `/incidents/{context_id}` | 총괄 `GET /api/incidents/{id}` | 사건 하나, 없으면 404 |
| GET `/executions?context_id` | 안전 관문 `GET /api/safety/executions` | 실행 기록 배열 |
- 브라우저가 총괄·안전 관문을 직접 부르지 않게 하는 경로다 (이슈 #255, ax-frontend #18). 읽기 전용이라 상위로는 GET만 나가고, 사람 결정은 여기서 만들지 않는다 (안전 관문 보류 큐 결정 경로 사용).
- 총괄 응답은 그대로 중계하되 `untrusted` 키는 중첩된 곳까지 지운다. 안전 관문 기록은 화면에 필요한 필드만 남긴다: `execution_id`, `created_at`, `request`(`action`·`requester`·`context_id`), `decision`(`grade`·`status`·`computed_amount`·`threshold_version`·`verifier`·`hold_reason`·`review_id`·`result_ref`). 요청의 `params`·`evidence`·멱등 키·`report`·고객 정보는 넘기지 않는다. 금액은 안전 레이어가 계산한 `computed_amount`뿐이다.
- 주소는 `AX_ORCHESTRATOR_URL`·`AX_SAFETY_URL`(기본 `http://<이름>:<포트>`). 연결 실패는 502 `UPSTREAM_ERROR`, 시간 초과는 504 `UPSTREAM_TIMEOUT`, 원본 형식이 다르면 502다.
- 목 모드는 샘플 사건 1개(보류 대기 포함)와 실행 기록 2개를 돌려준다. `/communications`와 같은 `{data, meta}` 봉투다.

### 사건 ID (`context_id`)
모든 요청에는 사건 단위 `context_id`가 필요하다. 화면(상담원) 호출은 요청마다 새 `ctx_ui_<hex>`를 붙인다 (`support_client.UI_CONTEXT_PREFIX`, 한 곳).
- 계약에 정해진 접두사는 흉내 내지 않는다: `ctx_inc_`(통합 에이전트 발급), `ctx_req_`(기획개발 발급 — 같은 변경 요청을 여러 부서가 보내면 하나로 묶는 번호표), `ctx_ntc_`, `ctx_cmp_`(마케팅 발급). 예전에는 화면 호출에 `ctx_req_`를 붙였는데, 기획개발이 발급하는 변경 요청 번호와 같은 모양이라 헷갈릴 수 있어 바꿨다.
- **임시 값이다.** 화면 호출용 접두사는 계약 `ContextPrefix`와 `docs/agent-common.md`에 아직 없다 (요청 이슈 참고). 정해지면 상수만 바꾼다.
- 이미 사건이 있는 대상(인수인계 질문, 클레임 지급 등)은 그 사건의 `context_id`를 그대로 쓴다.

## 4. 의존
ax-contracts, ax-agent-template
## 5. 하지 않는 것
- 다른 서비스 DB를 직접 읽지 않는다. 판매 사이트 환불·쿠폰 API를 직접 호출하지 않는다.
- LLM을 직접 호출하지 않는다 (답변 초안·가드·요약은 support 에이전트).
