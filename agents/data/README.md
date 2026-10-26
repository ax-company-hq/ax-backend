# agents/data
담당: 재현 · 포트: 8106
## 1. 역할
(TODO: 담당자가 작성)
## 2. 실행 방법
`docker compose -f infra/compose.yml up data` / 목 모드: `DATA_MOCK=1`

### 목 모드 데모 시각 정렬
- 시드는 고정 날짜다: 실패 주문 2026-10-21 05:00~05:30Z (`seed.py`), 지표 10/14~10/22 (`seed_metrics.py`).
- 요청 구간이 `seed.DEMO_ALIGN_FROM`(2026-10-23T00:00Z) 이후에 시작하면 목이 시드를 요청 시각으로 옮긴다.
  - 실패 주문: 시드 장애 시작을 요청 구간 시작에 맞춘다. 장애 6건은 [시작+3분, 시작+29분], 함정 3건은 −5분·+45분·+90분 (상대 위치 유지). 총괄의 사건 시각 ±15분 구간이면 `EXPECTED_*` 정답(6건·4명·95,000원)이 그대로 나온다.
  - 지표: 날짜 제한 없이 같은 시간대 패턴으로 7일 기준선을 만들고, 장애 30분(payment_fail_rate ×5)을 분석 구간 시작에 둔다.
- 목은 상태를 기억하지 않는다. 그래서 어떤 구간을 물어도 그 시작 30분이 장애다. 나중에 infra `verify_recovery`가 롤백 뒤 구간을 물으면 "복구 안 됨"이 나온다.
- 그 전 구간(시드 날짜 등)은 고정 날짜 그대로다. 기존 테스트는 이 경로를 쓴다.
- 구매자 ID는 shop-api와 같은 `buy_<4자리>`(`buy_0001`~`buy_0005`)다.

### 혼합 모드 리허설 (총괄 + 실제 data)
```
# 터미널 1: data 에이전트 (목)
DATA_MOCK=1 AGENT_TEMPLATE_MOCK=1 AGENT_NAME=data uv run --package ax-agent-data uvicorn data_agent.main:app --port 8106
# 터미널 2: 총괄, data만 실제
ORCHESTRATOR_REAL_DEPTS=data uv run --package ax-orchestrator python -m ax_orchestrator
```
`/demo`에서 장애를 주입하고 사건의 `impact` 단계를 연다(또는 `GET /api/incidents/<context_id>`의 `steps[impact].output`). 6건·4명·95,000원·`buy_0001~0004`가 나오면 정상이다.
## 3. 입출력 계약
contracts/ 참조 (TODO)
## 4. 의존
ax-contracts, ax-agent-template
## 5. 하지 않는 것
(TODO: 담당자가 작성)
