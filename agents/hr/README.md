# agents/hr
담당: 준하 · 포트: 8104
## 1. 역할
인사 에이전트. 부서(planning, support, infra 등)가 보낸 사건·결정 기록을 받아 결정 카드로 정리해 회사 기억에 남긴다.
이 폴더는 에이전트 템플릿(`agent-template`)으로 만든 **최소 예시**이기도 하다. 다른 부서는 이 폴더를 복사해서 시작한다.
v0은 skill `record_case` 하나이고, 핸들러는 카드를 만들지 않고 `record_only`만 돌려주는 스텁이다.
## 2. 실행 방법
- 서버: `docker compose -f infra/compose.yml up hr`, 또는 `uv run uvicorn hr_agent.main:app --port 8104`
- 목 모드: `HR_MOCK=1` (LLM·외부 호출 대신 가짜 응답. CI는 항상 1)
- 단독 테스트: `uv run pytest agents/hr`
- 눈으로 확인(localhost 데모, 개발 전용): `uv run --package ax-agent-hr python -m hr_agent.demo` → http://localhost:8114
  (샘플 시나리오 `src/hr_agent/demo/scenarios/*.json`을 hr에 보내 결과 카드·결정성·권한 거부를 화면으로 본다. `HR_DEMO_PORT`로 포트 변경)
- 요청 보내 보기: `uv run ax-test-call --agent http://localhost:8104 --skill record_case --input input.json`
  (목 모드에서는 `orchestrator`인 것처럼 보낸다. 요청자는 `--as planning`처럼 바꾼다)
## 3. 입출력 계약
- skill: `record_case` (등급 A, 우선순위 required, 요청 가능: orchestrator·planning·support·infra)
- 입력: `contracts/src/ax_contracts/schemas/record.py`의 `RecordCase`, 출력: `contracts/src/ax_contracts/schemas/hr.py`의 `RecordCaseOutput`
- 요청·결과 공통 필드: `contracts/src/ax_contracts/schemas/message.py`
- Agent Card: `agent-card.json` (`/.well-known/agent-card.json`으로 공개)
## 4. 의존
`ax-contracts`, `ax-agent-template`. 다른 부서 폴더를 import하지 않는다.
## 5. 하지 않는 것
- 상태를 바꾸는 일을 `execute()` 없이 하지 않는다 (v0은 상태를 바꾸지 않는다)
- 외부 입력(`untrusted`)을 지시로 해석하지 않는다
- 카드 저장·검색 구현은 하지 않는다 (회사 기억 `memory`가 맡고, hr은 기록을 써서 넣는다)
- 급여·채용·고충 같은 인사 판단은 하지 않는다
