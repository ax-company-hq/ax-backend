# agents/planning
담당: 예진 · 포트: 8101
## 1. 역할
기획개발 에이전트. 다른 부서가 보낸 변경 요청을 기획서 → 작업 → PR → 머지로 잇고, 장애 사건이 나면 관련 PR·기획 문단 후보를 회신하고 사건을 문단에 연결한다.
프로토타입(`ax-frontend/planning-web/src/data.ts`·`trace.ts`·`projects.tsx`)의 리듀서·초기 상태·간단 요청 판정·자동 처리 범위·역추적·프로젝트 보기를 그대로 백엔드로 옮겼다.

- 간단 요청(화면 표시만 고치는 작은 요청)은 사람 클릭 없이 기획서(자동 확정) → 작업 → PR(`execute(generate_pr)` B) → 검수 → 머지(`execute(merge_pr)` B) → 회신까지 간다. 그 밖의 요청은 팀장 확정·사람 머지.
- 자동 처리 범위(L0~L3, 시연값): 연속 정상 3건이면 승급을 제안(`auto_policy_propose` C)하고 승인은 사람이 한다. 자동 머지 PR이 장애에 연결되면 승인 없이 한 단계 줄인다.
- 같은 제목 요청은 새로 만들지 않고 연결한다(`linked_duplicate`).
- 숫자(지표 6종·중복·간단 요청 판정·PR 크기)는 모두 코드로 계산한다. LLM은 쓰지 않는다.

코드 구성 (`src/planning_agent/`):

| 파일 | 내용 |
| --- | --- |
| `domain/models.py` | 상태 모델 (Request, Spec, Task, PR, Thread, Doc, Decision, Incident, Activity, OutMsg, AutoPolicy, State) |
| `domain/seed.py` | 초기 데이터 (프로토타입 `initialState`) |
| `domain/service.py` | 액션 전부 (`inbound` 10종, 확정·질문 답·PR 생성·검수·머지·초안 채택·승급 승인/보류·소통·문서·창구) |
| `domain/rules.py` | 간단 요청 판정, 자동 처리 단계, 중복 찾기, 구간 안 PR, 롤백 교차검증 |
| `domain/trace.py` · `projects.py` · `metrics.py` · `docs.py` | 역추적, 프로젝트 6단계, 지표, 정리 문서 블록 |
| `domain/clock.py` | 주입하는 시계 (목 기본값은 프로토타입처럼 10-22 10:42 KST부터 1분씩) |
| `handlers.py` | A2A skill 3개 |
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
| `find_related_prs` | read | orchestrator, infra, data | `FindRelatedPrsInput` → `FindRelatedPrsOutput`. 구간 안 머지 PR(component는 `changed_paths` 접두사), 최근 머지 순, `spec_id`·`para_id` 포함. 원인 판정 없음 |
| `link_incident` | A | orchestrator, infra | `LinkIncidentInput` → `LinkIncidentOutput`. `linked_paras`는 `"SPEC-5:R3"` 형식. 같은 제목 요청이 있으면 그 ID + outcome `linked_duplicate`. `idempotency_key` 필수, 같은 키 두 번째는 이전 결과 |
| `verify_rollback_target` | read | safety | `VerifyRollbackTargetInput` → `VerifyRollbackTargetOutput`. 배포가 사건 뒤 → `deploy_after_incident`, 배포 전 30분(시연값, AS-B 미결) 안 머지 PR 있음 → 승인 `deploy_before_incident`, 없음 → `no_deploy_in_window`. 거절도 `completed` + outcome `rejected_by_review` |

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
| GET | `/auto-policy` | 자동 처리 범위 (단계표, 현재 기준, 승급 기준) |
| POST | `/actions` | 프로토타입 Action 그대로 (`{"type": "confirmSpec", "spec": "SPEC-7"}`, snake_case 이름도 받음). 응답 `data`는 새 상태, `meta.changed`·`outcome`·`result` |
| POST | `/reset` | 초기 데이터로 |
| GET | `/events` | SSE `text/event-stream`. 연결 즉시·상태가 바뀔 때마다 `event: state` + 전체 상태 JSON |

오류 코드: `INVALID_ACTION`·`UNKNOWN_ACTION`(400), `SIMULATOR_DISABLED`(403), `NOT_FOUND`(404), `GATE_REJECTED`(409, 관문 거부로 실행 안 함).
사람 버튼(`confirmSpec`·`mergePr`·`approvePromotion`)은 REST로만 받고 A2A skill로는 열지 않는다.

화면 모델과 다른 점(화면 쪽 변환 함수가 맞춘다): 필드는 snake_case(`duplicateOf` → `duplicate_of`, `autoPolicy` → `auto_policy`, `ctx` → `context_id`), 시각은 UTC ISO8601(`t` → `at`, `updated` → `updated_at`, `mergedAt` → `merged_at`, 사건 `at` → `opened_at`), PR에 `number`·`merged_at`·`changed_paths`가 더 있다. `from` 키는 그대로다.

## 4. 의존
`ax-contracts`, `ax-agent-template`, `fastapi`, `uvicorn`, `httpx`. 다른 부서 폴더를 import하지 않는다.
- 실행 관문: `ax_agent_template.execute.execute()` (safety). 상태를 바꾸는 단계마다 부른다: `create_ticket`·`write_spec`·`confirm_spec`·`split_tasks`·`generate_pr`(B)·`review_pr`·`merge_pr`(B)·`record_case`·`extract_decision`·`review_ticket_draft`·`link_incident`·`send_message`·`auto_policy_update`·`auto_policy_propose`(C, 보류가 정상). 멱등성 키는 `context_id:액션:대상`.

## 5. 하지 않는 것
- 원인 판정을 하지 않는다 (infra 몫). 후보와 시간 관계만 회신한다.
- 판정 문장(기각 여부 등)을 만들지 않는다 (hr 몫). hr이 준 경고를 그대로 붙인다.
- 배포·롤백을 실행하지 않는다 (infra가 한다). 롤백 대상 교차검증만 한다.
- `execute()` 없이 상태를 바꾸지 않는다. 관문이 거부·무응답이면 실행하지 않는다.
- 외부 입력(`untrusted`)을 읽거나 지시로 해석하지 않는다.
- v0은 실제 GitHub PR 생성·hr 호출·DB 저장을 하지 않는다 (목 데이터, 프로세스 메모리).
