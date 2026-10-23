# agents/hr
담당: 준하 · 포트: 8104
## 1. 역할
인사 에이전트. 두 가지 일을 한다.
- **기억 쓰기**: 부서(planning, support, infra 등)가 보낸 사건·결정 기록을 결정 카드로 정리해 회사 기억에 남긴다.
- **사람 변화 대응**: 교대·발령·퇴직·입사가 있을 때 담당 공백 없이 일이 넘어가게 한다 (명부 관리, 인수인계, 온보딩, 재배치 검증, 새 계획과 과거 결정 비교).

판정(완성도·공백·충돌·경과 일수)은 모두 코드 규칙이 하고, LLM은 문장만 다듬는다. LLM이 없거나 실패하면 고정 문장으로 대체하고 `fallback`으로 표시한다.
이 폴더는 에이전트 템플릿(`agent-template`)으로 만든 예시이기도 하다. 다른 부서는 이 폴더를 복사해서 시작한다.
## 2. 실행 방법
- 서버: `docker compose -f infra/compose.yml up hr`, 또는 `uv run uvicorn hr_agent.main:app --port 8104`
- 목 모드: `HR_MOCK=1` (LLM·외부 호출 대신 가짜 응답과 가상 명부·진행 중 항목. CI는 항상 1)
- 단독 테스트: `uv run pytest agents/hr` (다른 에이전트 없이 돈다. 공통 8항목과 hr 고유 4항목은 `tests/test_standalone.py`, 안전 회귀 81건은 `tests/test_safety_regression.py`, 반복·퍼징은 `tests/test_fuzz.py`)
- 눈으로 확인(localhost 데모, 개발 전용): `uv run --package ax-agent-hr python -m hr_agent.demo` → http://localhost:8114
  (탭: 시나리오 · 1 기록 · 2 계획 비교 · 3 명부·교대 · 4 인수인계 · 5 발령·퇴직 · 6 온보딩 · 보류 큐 · 감사 로그. 샘플 시나리오 `src/hr_agent/demo/scenarios/*.json`을 hr에 보내 결과·결정성·권한 거부를 화면으로 본다. 헤더의 `+10분`으로 시계를 움직여 보류 만료를 볼 수 있고 `HR_DEMO_PORT`로 포트를 바꾼다)
- 요청 보내 보기: `uv run ax-test-call --agent http://localhost:8104 --skill get_roster --input input.json`
  (목 모드에서는 `orchestrator`인 것처럼 보낸다. 요청자는 `--as planning`처럼 바꾼다)
## 3. 입출력 계약
Agent Card: `agent-card.json` (`/.well-known/agent-card.json`으로 공개). 입력·출력 모델은 `contracts/src/ax_contracts/schemas/`의 `record.py`·`hr.py`·`planning.py`. 요청·결과 공통 필드는 `message.py`.

| skill | 등급 | 하는 일 | 요청 가능 |
|---|---|---|---|
| `record_case` | A | 기록을 결정 카드로 정리해 저장 (결정 없으면 기록만, 확신 낮으면 후보) | orchestrator·planning·support·marketing·finance·data·infra |
| `get_roster` | 조회 | 영역별 담당·교대, 담당 없음·교대 공백 영역 | orchestrator·dashboard-api·planning·support·marketing·finance·data·infra |
| `start_handover` | A | 인수인계 초안, 빈 자리 질문, 완성도(코드 계산) | orchestrator |
| `answer_handover_question` | A | 인계자 답변 반영, 같은 답은 한 번만 | orchestrator·dashboard-api |
| `confirm_handover` | B(planning 검증) / 완성도 미달 C | 인수인계 확정 | orchestrator |
| `start_onboarding` | A | 읽을 결정 카드 순서·확인 목록·진행률 | orchestrator |
| `request_assignment_change` | A | 교대·발령·퇴직·입사 요청: 확인 목록·후임 후보·권한 목록(전달만) | orchestrator |
| `confirm_reassignment` | B(코드 규칙) | 담당 공백·중복이 없을 때만 재배치 적용, 적용 뒤 재검증·되돌리기 | orchestrator |
| `confirm_offboarding` | C | 퇴직 확정은 보류 큐에서 사람이 승인해야 적용 | orchestrator |
| `check_plan` | 조회 | 새 계획을 과거 결정과 비교 (기각됨·충돌·일치·기록에 없음) | orchestrator·dashboard-api·planning |
| `update_onboarding_step` | A | 온보딩 단계 완료 표시, 진행률은 코드가 다시 계산 | orchestrator·dashboard-api |
| `decide_candidate_card` | A | 확신 낮은 후보 카드를 사람이 승인(확정)·폐기 | orchestrator·dashboard-api |
| `list_handovers`·`list_open_questions`·`list_onboardings`·`list_changes`·`list_candidate_cards`·`get_audit_timeline` | 조회 | 대시보드 인사 화면용 현황(인수인계·인계자 질문·온보딩·변경·후보 카드·감사 기록) | orchestrator·dashboard-api |

## 4. 의존
`ax-contracts`, `ax-agent-template`. 다른 부서 폴더를 import하지 않는다.
외부 연결은 `src/hr_agent/clients/`의 인터페이스 세 개(`MemoryClient`·`PlanningClient`·`GateClient`) 뒤에 있고, 지금은 목 구현이다. 실제 연동(memory 서비스, planning 호출, safety의 `execute()`)은 이 폴더만 바꾸면 된다.
저장은 hr 전용 SQLite(`HR_DB_PATH`, 기본 메모리)이고 테이블은 hr만 쓴다.
실제 연동 뼈대는 `clients/real.py`(`HR_PLANNING_MODE=a2a`, `HR_MEMORY_MODE=http`). 가정과 환경변수 전체는 `docs/notes/hr/real-integration-assumptions.md`·`operations-guide.md`.
## 5. 하지 않는 것
- 상태를 바꾸는 일을 `execute()` 없이 하지 않는다 (등급: A 바로, B 검증 뒤, C 사람 승인 뒤)
- 외부 입력(`untrusted`)을 지시로 해석하지 않는다 (LLM 프롬프트에서는 데이터 블록에만 넣는다)
- 카드 저장·검색 구현은 하지 않는다 (회사 기억 `memory`가 맡고, hr은 기록을 써서 넣고 조회한다)
- 권한 회수·부여를 직접 실행하지 않는다 (목록을 만들어 각 부서에 전달만 한다)
- 급여·채용·고충 같은 인사 판단은 하지 않는다. 명부에는 담당 영역과 역할만 두고 개인 정보는 두지 않는다
- 숫자(완성도·경과 일수·진행률)를 LLM이 정하지 않는다
