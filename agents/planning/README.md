# agents/planning
담당: 예진 · 포트: 8101
## 1. 역할
기획개발 에이전트. 다른 부서가 보낸 변경 요청을 기획서 → 작업 → PR → 머지로 잇고, 장애 사건이 나면 관련 PR·기획 문단 후보를 회신하고 사건을 문단에 연결한다.
인사(hr)에는 인수인계용 진행 중 항목을 알려 주고(`list_in_progress`), 인계 목록 누락을 교차검증하고(`verify_handover`), 기각한 안을 `record_case`(kind `decision_rejected`)로 보낸다.
프로토타입(`ax-frontend/planning-web/src/data.ts`·`trace.ts`·`projects.tsx`)의 리듀서·초기 상태·간단 요청 판정·자동 처리 범위·역추적·프로젝트 보기를 그대로 백엔드로 옮겼다.

- 간단 요청(화면 표시만 고치는 작은 요청)은 사람 클릭 없이 기획서(자동 확정) → 작업 → PR(`execute(generate_pr)` B) → 검수 → 머지(`execute(merge_pr)` B) → 회신까지 간다.
- 기획서·PR은 사람 사전 승인 대신 규칙 교차검증 + 사후 감사로 처리한다 (10/23 결정, `domain/autocheck.py`).
  - 기획서: 질문 0·경고 0·근거 1 이상·되돌릴 수 없음 0이면 자동 확정(`execute(confirm_spec)`) → 작업 분할. 아니면 확정하지 않고 `auto_review.state` = `답 대기`(질문 남음) 또는 `교차검증 대기`. 질문에 답하면(`answerQuestion`)·`checkSpec`으로 다시 본다.
  - PR: 마지막 검수 통과 + `merge_pr` 규칙에 안 걸리면 자동 머지(`execute(merge_pr)` B). 걸리면 `교차검증 대기`, 되돌릴 수 없는 영역이면 `사람 확인`. safety가 보류·거절해도 `교차검증 대기`.
  - 자동 확정·머지는 활동(`audit.auto_confirmed`·`execute(merge_pr)`)과 `record_case`(`decision_reason`에 규칙 값)로 남겨 사후 감사한다. 대기 건 판단은 기존 사람 버튼(`confirmSpec`·`mergePr`)으로 한다.
  - 실제 교차검증자 연결·학습(한 번 학습, 백테스트, 시험 3건)은 safety 협의(#166 3번) 뒤. 지금은 대기열 상태와 `GET /review-queue`만 있다.
- 자동 처리 범위(L0~L3, 시연값): 연속 정상 3건이면 승급을 제안(`auto_policy_propose` C)하고 승인은 사람이 한다. 자동 머지 PR이 장애에 연결되면 승인 없이 한 단계 줄인다.
- 같은 제목 요청은 새로 만들지 않고 연결한다(`linked_duplicate`).
- 숫자(지표 6종·중복·간단 요청 판정·PR 크기)는 모두 코드로 계산한다. LLM은 쓰지 않는다.

코드 구성 (`src/planning_agent/`):

| 파일 | 내용 |
| --- | --- |
| `domain/models.py` | 상태 모델 (Request, Spec, Task, PR, Thread, Doc, Decision, Incident, Activity, OutMsg, AutoPolicy, State) |
| `domain/seed.py` | 초기 데이터 (프로토타입 `initialState`) |
| `domain/service.py` | 액션 전부 (`inbound` 10종, 확정·질문 답·PR 생성·검수·머지·초안 채택·승급 승인/보류·소통·문서·창구) |
| `domain/autocheck.py` | 기획서 자동 확정·PR 자동 머지 판정, #171 규칙 키 params 계산, 되돌릴 수 없는 영역 경로 접두사 |
| `domain/rules.py` | 간단 요청 판정, 자동 처리 단계, 중복 찾기, 구간 안 PR, 롤백 교차검증 |
| `domain/handover.py` | 인수인계용 진행 중 항목 계산 (담당자·영역 대응, 인계 목록 비교) |
| `domain/trace.py` · `projects.py` · `metrics.py` · `docs.py` | 역추적, 프로젝트 6단계, 지표, 정리 문서 블록 |
| `domain/clock.py` | 주입하는 시계 (목 기본값은 프로토타입처럼 10-22 10:42 KST부터 1분씩) |
| `handlers.py` | A2A skill 5개 (시나리오 A 3개 + hr 연동 2개) |
| `api.py` | 화면용 REST·SSE |
| `runtime.py` | 프로세스 메모리 상태 저장소 (A2A와 REST가 같이 본다) |

## 2. 실행 방법
- 서버: `docker compose -f infra/compose.yml up planning`, 또는 레포 루트에서 `uv run uvicorn planning_agent.main:app --port 8101`
- 목 모드: `PLANNING_MOCK=1` (또는 템플릿 공통 `AGENT_TEMPLATE_MOCK=1`). CI는 항상 1.
  - 상태는 프로세스 메모리에만 있다. 재시작하거나 `POST /api/planning/reset`이면 초기 데이터로 돌아간다.
  - 실행 관문 `execute()`는 템플릿 목(모두 `executed`)을 쓴다. 목 모드가 아니면 템플릿의 safety 호출이 아직 TODO라서 관문이 거부하고, 상태를 바꾸는 액션은 모두 실행되지 않는다(fail-closed).
  - 시뮬레이터 액션(`inbound`, `deptChat`)은 목 모드에서만 받는다.
  - 외부 네트워크(GitHub·hr·safety)를 부르지 않는다. hr `record_case`·GitHub PR은 활동 로그에만 남긴다.
- 단독 테스트: `uv run pytest agents/planning`
- 요청 보내 보기: `uv run ax-test-call --agent http://localhost:8101 --skill find_related_prs --input input.json --as infra`

## 3. 입출력 계약
### A2A (`POST /a2a`, Agent Card `agent-card.json` → `/.well-known/agent-card.json`)
입출력은 `contracts/src/ax_contracts/schemas/planning.py` 그대로, 요청·결과 공통 필드는 `message.py`.

| skill | 등급 | 요청 가능 | 입력 → 출력 |
| --- | --- | --- | --- |
| `create_ticket` | A | support, marketing, hr, data, orchestrator | `CreateTicketInput` → `CreateTicketOutput` (#174). 같은 제목 요청이 있으면 새 요청을 원래 요청에 묶고 outcome `linked_duplicate` + `duplicate_of`(원래 REQ), `context_id`는 원래 요청의 `ctx_req_`. 없으면 outcome `created` + 새 `REQ-n`·`ctx_req_`(planning 발급). `source_ref`(예: `inq_42`)·`evidence_ids`는 저장만 하고 판정에 안 쓴다. `untrusted`(고객 원문)는 읽지 않는다. 간단 요청이면 자동 처리 흐름을 탄다. 결과 통지는 같은 `context_id`에 `path`를 비운 새 요청으로 보낸다(요청 부서 수신 skill 전까지는 활동 기록만). `idempotency_key` 필수 |
| `find_related_prs` | read | orchestrator, infra, data | `FindRelatedPrsInput` → `FindRelatedPrsOutput`. 구간 안 머지 PR(component는 `changed_paths` 접두사), 최근 머지 순, `spec_id`·`para_id` 포함. 원인 판정 없음 |
| `link_incident` | A | orchestrator, infra | `LinkIncidentInput` → `LinkIncidentOutput`. `linked_paras`는 `"SPEC-5:R3"` 형식. 같은 제목 요청이 있으면 그 ID + outcome `linked_duplicate`. `idempotency_key` 필수, 같은 키 두 번째는 이전 결과 |
| `verify_rollback_target` | read | safety | `VerifyRollbackTargetInput` → `VerifyRollbackTargetOutput`. 배포가 사건 뒤 → `deploy_after_incident`, 배포 전 30분(시연값, AS-B 미결) 안 머지 PR 있음 → 승인 `deploy_before_incident`, 없음 → `no_deploy_in_window`. 거절도 `completed` + outcome `rejected_by_review` |
| `list_in_progress` | read | hr, orchestrator | `ListInProgressInput` → `ListInProgressOutput`. 담당자(`owner_id` = 명부 `member_id`)·영역(`area`)의 `deployment`·`ticket`·`pr` 항목. 다른 담당자·영역 항목은 넣지 않는다. 우선순위 recommended |
| `verify_handover` | read | safety | `VerifyHandoverInput` 또는 safety 교차검증 형식(`execution_id`·`action`=`confirm_handover`·`requester`·`params{handover_id·owner_id·area·item_ids·completeness_percent…}`, #187) → `VerifyHandoverOutput`. hr `confirm_handover`(B)의 교차검증. 담당자·영역을 모르면 승인하지 않고 `failed` + `invalid_input`(safety는 보류). 빠진 진행 중 항목 0건 → 승인 `complete`, 있으면 거절 `missing_items` + `missing`. 이미 끝난 항목은 `stale_item_ids`(판정에 안 씀). 거절도 `completed` + outcome `rejected_by_review` |

hr 연동 항목 규칙 (`domain/handover.py`, 시연값):

| kind | 대상 | item_id | status |
| --- | --- | --- | --- |
| `ticket` | 끝나지 않은 작업 | `tkt_<작업 ID>` (`tkt_T-11`) | 작업 상태 그대로 (`진행 중`·`PR 검수`·`머지 대기`·`C 보류`) |
| `pr` | 머지하지 않은 PR | `pr_<번호>` (`pr_31`) | `open` |
| `deployment` | dev에 머지됐지만 확인이 끝나지 않은 PR | `dep_pr_<번호>` (`dep_pr_28`) | `incident_follow_up_open`(연결된 사건의 재발 방지 요청이 열림) · `merged_unconfirmed`(걸린 요청이 아직 완료 아님) |

- 담당자: 작업 `owner` 라벨 → `member_id` (`백엔드` → `emp_01`, `프론트` → `emp_02`). 대응이 없는 작업(에이전트가 혼자 끝낸 자동 처리)은 사람 인계 대상이 아니라 넣지 않는다.
- 영역: 값은 명부 `area`(`payment`·`search`·`catalog`·`support-desk`·`infra-ops`·`inventory`)만 쓴다(#179). PR 바뀐 경로 디렉터리(`shop-api/payment`·`shop-api/cart`·`shop-web/checkout` → `payment`, `shop-web/support` → `support-desk`, `infra` → `infra-ops` 등, 경로 단위 비교) → 없으면 기획서 문단·제목 낱말(`결제`·`장바구니` → `payment` 등). 둘 다 안 맞으면 `area` 없음. 입력 `area`가 명부 값이 아니면 지어내지 않고 빈 목록 + `collected_at`.
- `verify_handover`는 `tkt_`·`pr_`·`dep_pr_`로 시작하는 ID만 비교한다. 결정 카드(`card_`)·문서 ID는 보지 않는다. planning에 없는 planning 형식 ID는 `evidence.data_summary`에 "확인할 수 없음"으로만 적는다.

`evidence.ref_ids`는 `pr:28`, `para:SPEC-5:R3`, `req:REQ-102` 형식이다. 근거가 없으면 `data_summary`가 "확인할 수 없음"으로 시작한다.
입력이 계약과 다르면 `failed` + outcome `invalid_input`, 관문이 거부하면 `failed` + outcome `gate_rejected`.

### 화면 REST (`/api/planning`, 성공 `{data, meta}` / 실패 `{error: {code, message}}`)
| 메서드 | 경로 | 내용 |
| --- | --- | --- |
| GET | `/state` | 전체 상태 (화면 store 한 번에 채우기) |
| GET | `/requests?state=` · `/specs` · `/tasks` · `/pull-requests` · `/threads` · `/decisions` · `/incident-links` · `/docs` | 목록 (`meta.count`) |
| GET | `/metrics` | 지표 6종 + 계산 원자료 |
| GET | `/projects` · `/projects/{id}` | 프로젝트 목록(진행 중 먼저)·상세(6단계, 모은 항목) |
| GET | `/trace/{pr}` | 역추적 (`28`, `PR-28`, `PR%20%2328` 모두 받음) |
| GET | `/review-queue?state=` | 자동 확정·머지를 멈춘 건 (`kind` spec/pr, `state` 답 대기·교차검증 대기·사람 확인, 걸린 `rules`, 계산한 `params`, safety `gate_status`·`review_id`, 판단 버튼 `decide_with`) |
| GET | `/auto-policy` | 자동 처리 범위 (단계표, 현재 기준, 승급 기준) |
| POST | `/actions` | 프로토타입 Action 그대로 (`{"type": "confirmSpec", "spec": "SPEC-7"}`, snake_case 이름도 받음). 응답 `data`는 새 상태, `meta.changed`·`outcome`·`result` |
| POST | `/requests/{id}/reject` | 개선 초안 기각. 본문 `{decision_reason, alternatives_considered}` 둘 다 필수. `/actions` + `rejectDraft`와 같은 길. 요청 상태 `기각`, hr `record_case` kind `decision_rejected` |
| POST | `/reset` | 초기 데이터로 |
| GET | `/events` | SSE `text/event-stream`. 연결 즉시·상태가 바뀔 때마다 `event: state` + 전체 상태 JSON |

기각 액션: `rejectDraft`(`request`, `decision_reason`, `alternatives_considered`) · `rejectDecision`(`spec`, `text`, `decision_reason`, `alternatives_considered`). 이유나 대안이 비면 hr에 보내지 않고 400 `MISSING_DECISION_REASON`.

execute params (safety #171 `thresholds.toml` 규칙 키, 모두 도메인 상태에서 코드로 계산. 요청 본문의 숫자는 받지 않는다):

| 액션 | params | 계산 |
| --- | --- | --- |
| `confirm_spec` | `open_questions`·`warnings`·`basis_count`·`irreversible_count` (+ `auto`, 경고가 있으면 `warn_ignored`) | 남은 질문 수, 인사 경고가 붙은 문단 수, 근거 수, 바꿀 경로가 되돌릴 수 없는 영역에 닿는 요구사항 문단 수 |
| `merge_pr` | `changed_lines`·`changed_files`·`changed_paths`·`review_mismatch_count`·`linked_incident_count`·`irreversible_count` (+ `auto`) | PR 크기·경로, 검수 불일치 횟수, 이 PR 문단 또는 같은 파일을 바꾼 PR 문단 중 장애 표시 수, 되돌릴 수 없는 영역 경로 수 |

되돌릴 수 없는 영역(제안 목록, 확정은 safety·사람): `shop-api/payment/migrations/`(결제 데이터 삭제·변경), `shop-api/accounts/pii/`(개인정보 필드), `shop-api/settlement/payout/`(실제 송금·정산 연동). 허용 경로는 safety와 같은 `shop-api/`·`shop-web/`·`docs/`(#200). 머지 위험 경로 `shop-web/checkout/`·`shop-web/payment/`·`shop-web/account/`는 `merge_pr`의 `irreversible_count`에만 더해 항상 사람 확인(C)으로 보낸다(기획서 확정에는 안 셈). 문단에 `paths`가 있으면 그 경로로, 없으면 역추적 규칙으로 정한다.

경고 무시 확정 (FR-SPEC-006): 인사 경고(`warn`)가 남은 문단을 둔 채 `confirmSpec`을 하면 "warn 무시 진행"을 세 곳에 남긴다. `execute(confirm_spec)` params `warn_ignored`, `record_case`(kind `spec_confirmed`) params의 `record_case.summary`·`source_refs`와 `warn_ignored`, 활동 로그(point `audit.warn_ignored`, hr로 가는 `record_case` 활동).

오류 코드: `INVALID_ACTION`·`UNKNOWN_ACTION`·`MISSING_DECISION_REASON`(400), `SIMULATOR_DISABLED`(403), `NOT_FOUND`(404), `GATE_REJECTED`(409, 관문 거부로 실행 안 함).
사람 버튼(`confirmSpec`·`mergePr`·`approvePromotion`)은 REST로만 받고 A2A skill로는 열지 않는다.

화면 모델과 다른 점(화면 쪽 변환 함수가 맞춘다): 필드는 snake_case(`duplicateOf` → `duplicate_of`, `autoPolicy` → `auto_policy`, `ctx` → `context_id`), 시각은 UTC ISO8601(`t` → `at`, `updated` → `updated_at`, `mergedAt` → `merged_at`, 사건 `at` → `opened_at`), PR에 `number`·`merged_at`·`changed_paths`가 더 있다. `from` 키는 그대로다.

## 4. 의존
`ax-contracts`, `ax-agent-template`, `fastapi`, `uvicorn`, `httpx`. 다른 부서 폴더를 import하지 않는다.
- hr `record_case` 입력은 계약 `RecordCase`(`record.py`)로 만들어 `execute(record_case)` params의 `record_case`에 넣는다 (`decision_rejected`·`spec_confirmed`).
- 실행 관문: `ax_agent_template.execute.execute()` (safety). 상태를 바꾸는 단계마다 부른다: `create_ticket`·`write_spec`·`confirm_spec`·`split_tasks`·`generate_pr`(B)·`review_pr`·`merge_pr`(B)·`record_case`·`extract_decision`·`review_ticket_draft`·`link_incident`·`send_message`·`auto_policy_update`·`auto_policy_propose`(C, 보류가 정상). 멱등성 키는 `context_id:액션:대상`.

## 5. 하지 않는 것
- 원인 판정을 하지 않는다 (infra 몫). 후보와 시간 관계만 회신한다.
- 판정 문장(기각 여부 등)을 만들지 않는다 (hr 몫). hr이 준 경고를 그대로 붙인다.
- 배포·롤백을 실행하지 않는다 (infra가 한다). 롤백 대상 교차검증만 한다.
- `execute()` 없이 상태를 바꾸지 않는다. 관문이 거부·무응답이면 실행하지 않는다.
- 외부 입력(`untrusted`)을 읽거나 지시로 해석하지 않는다.
- v0은 실제 GitHub PR 생성·hr 호출·DB 저장을 하지 않는다 (목 데이터, 프로세스 메모리).
