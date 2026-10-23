# hr 운영 가이드

대상: hr을 실행·점검하는 팀원. 호출하는 쪽 안내는 `calling-hr-guide.md`, 연동 가정은 `real-integration-assumptions.md`.

## 1. 한눈에
- 포트 8104, `POST /a2a`(skill 18개: 기능 10개 + 대시보드 화면용 조회·사람 입력 8개), `GET /.well-known/agent-card.json`. 데모 화면은 8114(개발 전용).
- 상태를 바꾸는 일은 모두 안전 레이어 관문(`execute()`)을 거친다: A 바로 실행, B 검증자 확인 뒤, C 사람 승인 뒤.
- 숫자와 판정(완성도·공백·충돌·경과 일수)은 코드가 정한다. LLM은 질문·교훈 문장만 다듬고, 실패하면 고정 문장(`fallback: true`)이다.
- 저장은 hr 전용 SQLite. 명부·교대·인수인계·온보딩·변경·잠금·감사 로그는 hr 테이블, 카드는 memory(지금은 목).

## 2. 실행
| 목적 | 명령 |
|---|---|
| 서버 | `uv run uvicorn hr_agent.main:app --port 8104` (compose: `docker compose -f infra/compose.yml up hr`) |
| 목 모드(CI·데모) | `HR_MOCK=1` 또는 `AGENT_TEMPLATE_MOCK=1` |
| 단독 테스트 | `uv run pytest agents/hr` (약 25초, 퍼징 포함) |
| 안전 회귀만 | `uv run pytest agents/hr/tests/test_safety_regression.py` |
| 퍼징 늘리기 | `FUZZ_SEED=7 FUZZ_ROUNDS=5000 uv run pytest agents/hr/tests/test_fuzz.py` |
| 데모 | `uv run --package ax-agent-hr python -m hr_agent.demo` → http://localhost:8114 |
| 직접 호출 | `uv run ax-test-call --agent http://localhost:8104 --skill get_roster --input input.json` |

## 3. 환경변수
| 변수 | 기본 | 뜻 |
|---|---|---|
| `HR_MOCK` | 목 | 없으면 실제 안전 레이어(템플릿 `execute()`)를 쓰고 가상 명부 시드를 넣지 않는다 |
| `HR_DB_PATH` | 메모리 | hr SQLite 파일 경로 |
| `HR_HANDOVER_MIN_COMPLETENESS` | 80 | 인수인계 확정 B/C 경계(미만이면 사람 승인) |
| `HR_HANDOVER_QUESTION_TTL_MIN` | 60 | 답 없는 인계자 질문을 다시 묻는 기한(분) |
| `HR_HOLD_TTL_MIN` | 10 | C 보류 자동 거절까지(분, 목 안전 레이어) |
| `HR_LOCK_TTL_MIN` | 1440 | 변경 잠금이 이 시간 넘게 남으면 자동으로 풀림 |
| `HR_CARD_AUTO_CONFIRM` | high | 이 확신도일 때만 카드를 자동 확정(나머지는 후보) |
| `HR_TIMER` | 꺼짐 | `1`이면 곧 끝나는 교대의 인수인계를 자동 시작 |
| `HR_PLANNING_MODE` | mock | `a2a`면 planning을 A2A로 부름 |
| `HR_MEMORY_MODE` | mock | `http`면 memory를 REST로 부름 (`HR_MEMORY_URL`) |
| `HR_DEMO_PORT` | 8114 | 데모 포트 |

## 4. 등급표 (신고 초안, 레지스트리 반영은 태윤 PR)
| 액션 | 등급 | 비고 |
|---|---|---|
| `memory.card_write`·`memory.card_link` | A | 이미 등록(회사 기억) |
| `record_case`·`start_handover`·`answer_handover_question`·`request_assignment_change`·`start_onboarding` | A | |
| `confirm_handover` | B | planning이 진행 중 항목 누락을 교차검증 |
| `confirm_handover_incomplete` | C | 완성도 기준 미만 |
| `confirm_reassignment` | B | 담당 공백·중복 없음(코드 규칙) |
| `confirm_offboarding` | C | 항상 사람 승인 |

## 5. 상태 전이
- 인수인계: `draft → input_required → ready → confirmed`, 보류되면 `held`.
- 변경(발령·교대·입사): `requested → confirmed`. 퇴직은 `requested → confirmed(재배치 끝) → offboarded | cancelled`. 취소된 퇴직은 같은 요청을 다시 보내면 (관문을 거쳐) `confirmed`로 다시 열린다. 어느 단계든 `HR_LOCK_TTL_MIN`이 지나도록 방치되면 `expired`로 닫히고 더 확정할 수 없다.
- 잠금: 같은 사람·같은 영역의 변경이 끝나기 전에는 다른 변경이 `LOCKED`. 퇴직은 `offboarded`/`cancelled`에서 풀린다.

## 6. 장애 대응
| 증상 | 원인 | 조치 |
|---|---|---|
| 결과가 `failed` / `UPSTREAM_UNAVAILABLE` | memory·planning 응답 없음/형식 오류 | 해당 서비스 확인. 상태는 바뀌지 않았다. 같은 요청을 다시 보내면 된다 |
| 결과가 `gate_rejected` | 안전 레이어 무응답·미등록·검증 거절 | safety 상태와 레지스트리 등록 확인. fail-closed이므로 hr은 아무것도 바꾸지 않는다 |
| `input-required` / `human_input` | C 보류(퇴직 확정·미달 인수인계) | 대시보드 보류 큐에서 사람이 승인·거절. 승인 후 **같은 요청을 다시 보낸다** |
| `LOCKED` | 같은 대상의 다른 변경 진행 중 | 먼저 변경을 끝내거나 `HR_LOCK_TTL_MIN`이 지나길 기다린다 |
| `CONFLICT` | 같은 사건에서 후임·답변이 다른 요청 | 새 사건(`context_id`)으로 다시 요청 |
| `COVERAGE_GAP` | 퇴직하면 담당이 비는 영역이 있음 | 그 영역의 후임을 먼저 정한다 |
| `GATE_UNAVAILABLE` | 안전 레이어가 판정하지 못함(무응답 등). 사람이 거절한 것과 구분된다 | 아무것도 바뀌지 않았다. safety가 살아난 뒤 같은 요청을 다시 보낸다 |
| `LOCK_LOST`·`EXPIRED`·`BAD_STATE` | 잠금을 다른 변경이 잡았거나 방치돼 만료됨 | 새 요청(새 `context_id`)으로 다시 시작한다 |
| `NOTHING_TO_APPLY` | 대상자가 이미 그 영역 담당이 아님 | 명부를 확인한다 |
| 카드가 계속 후보 | 이유·대안·근거 부족 | 호출 쪽이 사실을 채워 다시 보낸다 |
| 데모 호출이 `TIMEOUT` | 요청 마감이 지남 | 데모 러너는 실제 시각에서 30초로 잰다. 직접 요청이면 `deadline`을 확인 |

## 7. 감사 로그
`context_id` 하나로 hr이 한 일을 시간순으로 본다. 데모의 "감사 로그" 탭, 또는 코드에서 `AuditLog.timeline(context_id)`. 기록 종류: `start_handover`, `answer_handover_question`, `confirm_handover(.held/.rejected)`, `start_onboarding`, `assignment.requested`, `reassignment.confirmed/.rejected/.undone`, `offboarding.held/.cancelled/.reopened/.confirmed/.blocked`, `handover.question_reasked`, `timer.shift_handover`, `record_case.*`, `check_plan`.

## 8. 하지 않는 것 (바꾸지 말 것)
- 상태를 바꾸는 일을 `execute()` 없이 하지 않는다. 쓰기 전에 관문을 거치는 순서를 바꾸지 않는다.
- 숫자·판정을 LLM에 맡기지 않는다. 외부 입력(`untrusted`)을 지시로 읽지 않는다.
- 권한 회수·부여를 직접 실행하지 않는다(목록 전달만). 개인 정보를 명부에 두지 않는다.
- 같은 멱등성 키를 다른 대상에 재사용하는 요청은 관문이 거부한다. 키는 `context_id + 액션 + 대상`으로 만든다.

## 9. 대시보드 화면용 skill (조회 6 + 사람 입력 3)
- 조회: `list_handovers`·`list_open_questions`·`list_onboardings`·`list_changes`·`list_candidate_cards`·`get_audit_timeline`, 그리고 기존 `get_roster`·`check_plan`. 상태를 바꾸지 않고 관문을 거치지 않으며 저장된 값을 그대로 돌려준다.
- 사람 입력: `answer_handover_question`(질문 답변), `update_onboarding_step`(온보딩 단계 완료), `decide_candidate_card`(후보 카드 승인·폐기). 모두 `dashboard-api`가 요청할 수 있고 관문(A)을 거친다.
- 호출 구조와 응답 샘플은 `dashboard-api-contract.md`, `dashboard-fixtures/`. 샘플은 `uv run python agents/hr/scripts/make_dashboard_fixtures.py`로 다시 만든다.
- 후보 카드는 `status=candidate`다. 승인하면 `active`가 되고 같은 영역의 옛 결정을 대체됨으로 연결한다.
