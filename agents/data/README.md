# agents/data
담당: 재현 · 포트: 8106
## 1. 역할
(TODO: 담당자가 작성)
## 2. 실행 방법
`docker compose -f infra/compose.yml up data` / 목 모드: `DATA_MOCK=1`

### 목 모드 데모 시각 정렬
- 시드는 고정 날짜다: 실패 주문 2026-10-21 05:00~05:30Z (`seed.py`), 지표 10/14~10/22 (`seed_metrics.py`).
- 요청 구간이 `seed.DEMO_ALIGN_FROM`(2026-10-23T00:00Z) 이후에 시작하면 목이 시드 장애를 데모 시각으로 옮긴다. 그 전 구간은 고정 날짜 그대로다. 기존 테스트는 이 경로를 쓴다.
- **장애 시각(anchor)은 프로세스 안에서 기억하고, impact_analysis만 정한다** (`demo_anchor.py`).
  - anchor가 없거나, 요청 구간 [from, to)가 저장된 장애와 겹치지 않고 장애 끝 이후에 시작하면 새 사건이다. 이때 anchor = 요청 구간 시작. SCN-A에서는 사건 시각 −15분이다.
  - 저장된 장애와 겹치는 구간은 같은 사건의 재조회라 anchor를 유지한다 (재무 손실 리포트 등).
  - 지표(anomaly_detection·감시)는 anchor를 읽기만 한다. anchor가 없으면 정상 값이다.
  - 장애 = [anchor, anchor + `DATA_DEMO_INCIDENT_MINUTES`) (기본 6분, 최소 5분). 결제 실패율이 5배가 된다.
  - 안전장치: 마지막 anchor 요청 뒤 `DATA_DEMO_RESET_MINUTES`(기본 60분)가 지나면 anchor를 비운다.
- 실패 주문: 장애 6건을 장애 길이에 비례해 압축한다(6분이면 0.6~5.8분). 그래서 `EXPECTED_*`(6건·4명·95,000원)가 그대로 나온다. 함정 3건은 anchor 기준 −5분·+45분·+90분이다.
- 지표: 7일 기준선 + 위 장애. anchor + 장애 길이 이후 구간은 정상이라 `verify_recovery`의 복구 확인을 통과한다.
- `MockMetricSource()`는 기본(`starts_incident=False`)으로 읽기만 한다. 그래서 감시 루프(`monitor.run_forever`)나 복구 확인이 장애를 만들지 않고, 시연이 만든 장애는 한 번만 감지한다. 지표만으로 시연·테스트할 때는 `starts_incident=True`로 지표 요청도 anchor를 정하게 한다.
- 구매자 ID는 shop-api와 같은 `buy_<4자리>`(`buy_0001`~`buy_0005`)다.

### 데모 순서 (SCN-A)
1. `/demo`에서 장애를 주입한다. 총괄이 impact_analysis를 부르면 anchor가 정해진다.
2. 장애 길이(기본 6분) 안에 롤백한다.
3. 복구를 확인한다. 롤백 뒤 구간의 anomaly_detection은 결제 실패율 정상(`any_anomaly=false`)이다.
4. 연속 리허설은 재시작 없이 그대로 다시 주입하면 된다. 새 사건의 impact 구간(사건 시각 −15분부터)이 이전 장애 끝 이후에 시작하면 새 anchor가 잡힌다. 그래서 사건 사이를 장애 길이보다 길게, 대략 7분 이상 둔다. 그보다 빨리 주입하면 이전 장애와 겹쳐 같은 사건으로 보고 impact가 6건보다 적게 나올 수 있다. 같은 프로세스에서 10분 간격 3회 연속은 테스트(`test_demo_consecutive.py`)로 확인한다.

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
