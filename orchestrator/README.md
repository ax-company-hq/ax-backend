# orchestrator
담당: 준하 · 포트: 8000

## 1. 역할
통합 에이전트(총괄). 이벤트를 받아 사건을 판단하고(`ctx_inc_` 발급), Agent Card 레지스트리로 부서를 골라 plan대로 순차·병렬 분배하고, `input-required`를 중계하고, 이관·재시도를 처리하고, 사건을 닫으며 hr에 기록을 요청한다. 인터페이스 기준은 `docs/notes/orchestrator/interface.md`.

## 2. 실행 방법
목 모드(가짜 부서 7곳, 실제 연동 없음). 빈 포트를 찾아 띄우고 브라우저를 연다:
```
uv run python -m ax_orchestrator
```
터미널에 나온 `http://localhost:<포트>/demo`에서 장애를 주입해 사건 흐름을 본다. `ORCHESTRATOR_HOLD_TIMEOUT`(초, 기본 600)은 C 등급 승인 대기 마감이다.
혼합 모드(내 부서만 실제, 나머지는 가짜): `ORCHESTRATOR_REAL_DEPTS=finance`(쉼표로 여러 부서). 부서별로 총괄이 부르는 skill·입력은 `docs/notes/orchestrator/department-guide.md`.
시연 콘솔: 백엔드의 `/demo`는 개발용 간단 화면이고, 같은 기능의 프론트엔드 시연 콘솔은 `ax-frontend`의 `orchestrator-web`(포트 3004)이다. 접속 방법(터미널 2개)은 그 레포 README의 "통합 에이전트 화면 접속"을 따른다. 운영 화면(관리자 회사 전체)은 `dashboard-web`(병환)이 `dashboard-api`를 거쳐 만든다.
테스트: `uv run pytest orchestrator`

## 3. 입출력 계약
`ax_contracts`의 `AgentRequest/AgentResult/EventEnvelope`를 그대로 쓴다. 새 계약 없음.

## 4. 구성
`registry.py`(카드 수집) · `hybrid.py`(부서별 실제/가짜 선택) · `real.py`(HTTP 카드 수집) · `guide.py`(연동 안내서 생성) · `manager.py`(이벤트→사건·사건 조회) · `engine.py`(plan 실행·재시도·이관·중계) · `plans/scenario_a.py`(시나리오 A 데이터) · `a2a.py`(총괄 자신의 `/a2a` skill `find_active_incident`) · `mock/`(가짜 부서·하네스) · `static/demo.html`(목업 화면)

## 5. 하지 않는 것
분석 문장·금액·등급을 만들지 않는다. C 등급을 승인하지 않는다(사람이 대시보드에서). 부서 DB를 직접 건드리지 않는다. 외부 텍스트를 지시로 해석하지 않는다.

## 6. 아직 없는 것 (실제 연동 단계)
Redis Streams 이벤트 버스(템플릿 TODO), 실제 부서 호출, SQLite 저장·재시작 복구, 총괄 `/a2a`의 나머지 skill(이상 보고·해결 보고 접수, 계약 선행). `find_active_incident`는 있다.
