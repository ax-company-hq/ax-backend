# 대시보드 "인사" 화면 API 계약 (초안)

대상: 병환(dashboard-api), 화면 담당(ax-frontend `dashboard-web`), 준하(hr).
상태: **초안**. `dashboard-api`와 `contracts/`에는 아무것도 넣지 않았다. 이 문서와 `dashboard-fixtures/`가 그대로 합의안이 되도록 썼다.

## 1. 원칙
- 지금 `support` 문의 API가 쓰는 방식을 그대로 따른다: 화면 → `dashboard-api`(`/api/dashboard/...`) → **hr 에이전트 skill을 A2A로 호출**(`support_client.py`와 같은 구조, 토큰은 `dashboard-api`).
- 응답은 성공 `{data, meta}`, 실패 `{error: {code, message}}`. 목록은 `data`가 배열이고 `meta.total`이 건수다.
- 모델은 새로 만들지 않고 hr 계약 모델을 그대로 돌려준다(`contracts/src/ax_contracts/schemas/hr.py`). 화면 타입은 `contracts/openapi/dashboard-api.json`에서 만든다.
- 상태를 바꾸는 요청은 `idempotency_key`를 **대상을 넣어** 만든다: `ctx_req_<hex>:<skill>:<대상>` (`support_client.call(..., target=...)`과 같다). 같은 대상에 같은 요청을 다시 보내면 hr이 한 번만 처리한다.
- 모든 숫자(완성도·진행률·경과 일수)는 hr이 계산한 값을 그대로 보여 준다. 화면이 다시 계산하지 않는다. 값이 없으면 "확인할 수 없음"으로 보여 준다.
- C 등급 보류 큐(퇴직 확정·미달 인수인계)는 **safety의 보류 큐**다. hr은 보류 건을 따로 보관하지 않는다(6장).

## 2. 엔드포인트 한눈에 (`/api/dashboard` 아래)

| 화면 | 메서드·경로 | hr skill | `data` | 샘플 파일 |
|---|---|---|---|---|
| 인수인계 현황 | `GET /hr/handovers?status&area` | `list_handovers` | `HandoverView[]` | `GET_hr_handovers.json` |
| 인계자 질문 | `GET /hr/questions?assignee_id&handover_id` | `list_open_questions` | `HandoverQuestion[]` | `GET_hr_questions.json` |
| 질문에 답하기 | `POST /hr/questions/{question_id}/answer` | `answer_handover_question` | `AnswerHandoverQuestionOutput` | `POST_hr_questions_answer.json` |
| 온보딩 현황 | `GET /hr/onboardings?member_id&area` | `list_onboardings` | `OnboardingView[]` | `GET_hr_onboardings.json` |
| 단계 완료 표시 | `POST /hr/onboardings/{onboarding_id}/steps/{step_id}` | `update_onboarding_step` | `StartOnboardingOutput` | `POST_hr_onboardings_step.json` |
| 변경(발령·퇴직·입사) | `GET /hr/changes?status&area&member_id` | `list_changes` | `ChangeView[]` | `GET_hr_changes.json` |
| 명부·공백 | `GET /hr/roster?area` | `get_roster` | `GetRosterOutput` | `GET_hr_roster.json` |
| 후보 카드 큐 | `GET /hr/candidate-cards?area` | `list_candidate_cards` | `Card[]` (`status=candidate`) | `GET_hr_candidate-cards.json` |
| 후보 카드 결정 | `POST /hr/candidate-cards/{card_id}/decision` | `decide_candidate_card` | `DecideCandidateCardOutput` | `POST_hr_candidate-cards_decision.json` |
| 감사 기록 | `GET /hr/audit/{context_id}` | `get_audit_timeline` | `AuditEntry[]` | `GET_hr_audit.json` |
| 계획 비교(선택) | `POST /hr/plan-check` | `check_plan` | `CheckPlanOutput` | `POST_hr_plan-check.json` |

화면에서 **하지 않는** 것: 인수인계 시작·확정, 변경 요청·재배치·퇴직 확정은 통합 에이전트(orchestrator)가 부른다. hr의 Agent Card도 이 skill들은 `dashboard-api`를 허용하지 않는다.

## 3. 요청 본문

| 경로 | 본문 | 비고 |
|---|---|---|
| `POST /hr/questions/{id}/answer` | `{"answer": "…", "idempotency_key": "…"}` | 계약 `HandoverAnswerRequest`. `answer`는 비어 있으면 안 된다. 같은 답을 다시 보내면 한 번만 반영되고, **다른 답**을 다시 보내면 409 `CONFLICT` |
| `POST /hr/onboardings/{id}/steps/{step_id}` | `{"done": true}` | `false`면 완료 취소. 이미 그 상태면 그대로 성공(`outcome=duplicate`) |
| `POST /hr/candidate-cards/{id}/decision` | `{"decision": "approve"\|"reject", "note": "…"}` | 이미 반영된 같은 결정은 성공, 반대 결정은 409 `NOT_CANDIDATE` |
| `POST /hr/plan-check` | `{"draft": {"title","summary","area","kind"}}` | `kind`는 `spec`·`ticket`·`deploy_plan` |

경로의 `{...}`는 hr skill 입력의 대응 필드로 옮긴다(`question_id`, `onboarding_id`+`step_id`, `card_id`).

## 4. 응답 필드 요약 (자세한 모양은 샘플 파일)

- `HandoverView`: `handover_id`, `context_id`, `owner_id`, `successor_id`, `area`, `reason`(`shift`·`transfer`·`offboarding`·`other`), `status`(`draft`·`input_required`·`ready`·`confirmed`·`held`), `completeness`(0~100), `items[]`(`kind`·`title`·`source_ref`), `open_questions[]`, `created_at`, `updated_at`.
- `HandoverQuestion`: `question_id`, `context_id`, `assignee_id`, `question`, `gap_field`. 다시 묻는 질문은 `question` 앞에 `(다시 묻습니다 N번째)`가 붙는다.
- `OnboardingView`: `onboarding_id`, `member_id`, `area`, `steps[]`(`order`·`title`·`card_id`·`done`), `progress`(0~100).
- `ChangeView`: `change_id`, `change_kind`(`shift`·`transfer`·`offboarding`·`join`), `member_id`, `area`, `successor_id`, `status`(`requested`·`confirmed`·`offboarded`·`cancelled`·`expired`), `handover_id`, `onboarding_id`, `checklist[]`.
- `GetRosterOutput`: `entries[]`(`area`·`role`·`member_id`·`shift_start`·`shift_end`), `gap_areas[]`(담당이 없거나 교대 시각에 비는 영역), `collected_at`. 개인 정보는 없다.
- `Card`: 계약 `Card`. 후보는 `status="candidate"`, `confidence`는 `low`·`mid`·`high`(퍼센트 아님), 영역·결정·이유는 `extra`에 있다.
- 모든 조회 응답에는 조회 시각이 있다(`collected_at`). 화면에 "N분 전 기준"으로 보여 줄 수 있다.

## 5. 오류 매핑 (`support_client.unwrap`과 같은 방식)

| hr 결과 | HTTP | `error.code` |
|---|---|---|
| `rejected` / `INVALID_INPUT` | 422 | `INVALID_INPUT` |
| `rejected` / `FORBIDDEN_SKILL`·`UNAUTHORIZED` 등 | 403 | `UPSTREAM_REJECTED` |
| `completed` + `outcome` ∈ `UNKNOWN_CARD`·`UNKNOWN_STEP`·`UNKNOWN_QUESTION`·`UNKNOWN_HANDOVER`·`UNKNOWN_CHANGE`·`UNKNOWN_MEMBER`·`UNKNOWN_AREA` | 404 | `NOT_FOUND` |
| `completed` + `outcome` ∈ `CONFLICT`·`NOT_CANDIDATE`·`BAD_STATE`·`LOCKED`·`LOCK_LOST`·`EXPIRED`·`CANCELLED`·`HANDOVER_CLOSED` | 409 | `CONFLICT` (메시지는 hr의 `output.message`) |
| `gate_held`·`gate_rejected` 등 `gate_*` | 409 | `NOT_EXECUTED` |
| `failed` / `UPSTREAM_UNAVAILABLE`·`GATE_UNAVAILABLE` | 502 | `UPSTREAM_ERROR` |
| `canceled` / `TIMEOUT` | 504 | `UPSTREAM_TIMEOUT` |

`input-required`(`human_input`)는 오류가 아니다. 사람이 승인할 일이 있다는 뜻이고 `data`로 그대로 돌려준다(보류 큐 참고).

## 6. 보류 큐(safety)와의 경계
- 퇴직 확정과 완성도 미달 인수인계는 hr이 `input-required`/`held`로 돌려주고, 승인·거절은 **safety 보류 큐**에서 사람이 한다(`safety` `GET /api/safety/reviews`, `POST .../decision`).
- 대시보드 보류 큐 화면은 safety 목록을 보여 주고, 건마다 사람이 읽을 `summary`(예: `퇴직 확정 요청: emp_06 (support-desk)`)가 필요하다. hr은 `params.summary`로 보낸다(safety가 노출해 달라고 요청 중).
- 사람이 승인하면 통합 에이전트가 **같은 요청을 다시** hr에 보낸다(같은 `idempotency_key`). 그러면 hr이 적용한다. 화면은 이 재요청을 하지 않는다.

## 7. 화면 ↔ hr 데이터가 쓰는 출처 한눈에
| 화면 | 필요한 것 | 출처 |
|---|---|---|
| 인사 탭 홈 | 열린 질문 수, 진행 중 인수인계, 후보 카드 수, 보류 건수 | hr 조회 3개 + safety |
| 인수인계 상세 | 완성도, 자동 수집된 항목, 빈 칸 질문, 답하기 | `list_handovers`, `answer_handover_question` |
| 변경(발령·퇴직) | 요청 상태, 확인 목록, 후임 | `list_changes` |
| 온보딩 | 읽을 카드, 진행률, 단계 완료 | `list_onboardings`, `update_onboarding_step` |
| 명부 | 영역별 담당·교대, 공백 | `get_roster` |
| 후보 카드 큐 | 확신 낮은 카드 승인·폐기 | `list_candidate_cards`, `decide_candidate_card` |
| 사건 상세의 "hr이 한 일" | 시간순 기록 | `get_audit_timeline` |

## 8. 열린 결정 (병환·준하·화면 담당)
1. 경로 이름과 `/hr/...` 묶음을 이대로 쓸지(다른 부서 탭과 맞추려면 `/departments/hr/...`).
2. 목록 페이징: hr 조회는 `limit`(기본 50, 최대 200)만 받는다. 화면이 더 필요하면 알려 달라.
3. 갱신 주기: 지금 설계는 화면이 15초 폴링(루트 규칙의 "실시간 갱신은 폴링")한다. hr은 이벤트를 발행하지 않는다.
4. 인계자는 누구인가: 지금 질문의 `assignee_id`는 `emp_xx` ID뿐이다. 화면이 이름을 보여 주려면 사람 정보 출처가 필요하다(hr은 개인 정보를 두지 않는다).
5. 후보 카드 승인은 누구나 가능한지(권한 규칙)는 dashboard-api가 정한다. hr은 요청자 토큰만 본다.
6. 인사 탭을 `[필수]` 범위로 볼지(요구사항 `DEP-HR`은 `record_case`만 필수).
