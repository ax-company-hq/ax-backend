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
  - impact 요청 구간 시작이 저장된 anchor와 1분 이내면 같은 사건의 재조회다 (재무 손실 리포트 등). anchor를 유지한다.
  - 그 밖에는 구간이 이전 장애와 겹쳐도 새 사건이다. anchor = 요청 구간 시작이고, SCN-A에서는 사건 시각 −15분이다.
  - 지표(anomaly_detection·감시)는 anchor를 읽기만 한다. anchor가 없으면 정상 값이다.
  - 장애 = [anchor, anchor + `DATA_DEMO_INCIDENT_MINUTES`) (기본 6분, 최소 5분). 결제 실패율이 5배가 된다.
  - 안전장치: 마지막 anchor 요청 뒤 `DATA_DEMO_RESET_MINUTES`(기본 60분)가 지나면 anchor를 비운다.
- 실패 주문: 장애 6건을 [1분, 장애 길이) 안으로 비례 압축한다(6분이면 1.5~5.8분). 같은 사건 재조회가 1분 늦게 시작해도 6건이 모두 들어가고, `EXPECTED_*`(6건·4명·95,000원)가 그대로 나온다. 함정 3건은 anchor 기준 −5분·+45분·+90분이다.
- 지표: 7일 기준선 + 위 장애. anchor + 장애 길이 이후 구간은 정상이라 `verify_recovery`의 복구 확인을 통과한다.
- `MockMetricSource()`는 기본(`starts_incident=False`)으로 읽기만 한다. 그래서 감시 루프(`monitor.run_forever`)나 복구 확인이 장애를 만들지 않고, 시연이 만든 장애는 한 번만 감지한다. 지표만으로 시연·테스트할 때는 `starts_incident=True`로 지표 요청도 anchor를 정하게 한다.
- 구매자 ID는 shop-api와 같은 `buy_<4자리>`(`buy_0001`~`buy_0005`)다.

### 데모 순서 (SCN-A)
1. `/demo`에서 장애를 주입한다. 총괄이 impact_analysis를 부르면 anchor가 정해진다.
2. 장애 길이(기본 6분) 안에 롤백한다.
3. 복구를 확인한다. 롤백 뒤 구간의 anomaly_detection은 결제 실패율 정상(`any_anomaly=false`)이다.
4. 연속 리허설은 재시작 없이, 쉬지 않고 다시 주입해도 된다 (e2e `--runs 3`). 회차마다 impact 구간 시작이 달라서 새 anchor가 잡힌다. 1분 안에 다시 주입하면 같은 사건으로 남지만, 장애 주문이 1분 뒤부터 있어 결과는 같다(6건·4명·95,000원). 1분 간격·10분 간격 3회 연속은 테스트(`test_demo_consecutive.py`)로 확인한다.

### 혼합 모드 리허설 (총괄 + 실제 data)
```
# 터미널 1: data 에이전트 (목)
DATA_MOCK=1 AGENT_TEMPLATE_MOCK=1 AGENT_NAME=data uv run --package ax-agent-data uvicorn data_agent.main:app --port 8106
# 터미널 2: 총괄, data만 실제
ORCHESTRATOR_REAL_DEPTS=data uv run --package ax-orchestrator python -m ax_orchestrator
```
`/demo`에서 장애를 주입하고 사건의 `impact` 단계를 연다(또는 `GET /api/incidents/<context_id>`의 `steps[impact].output`). 6건·4명·95,000원·`buy_0001~0004`가 나오면 정상이다.
### kpi_summary (DSH-04, SCN-L)
- 공통 로그 저장소를 **읽기만** 한다. `DATA_MOCK=1`이면 시드(`seed_logs.py`), 아니면 `AX_COMMON_LOG_DB`(compose는 `infra/common-log/data.yml`이 연결). 비어 있으면 `unavailable`.
- 사건 = `ctx_inc_` context (입력 `context_ids`를 주면 그 context). 숫자는 `metrics/kpi.py`가 계산한다.
  - detected_at: 사건의 첫 로그 시각
  - recovered_at: `incident.resolved` 발행 → 없으면 `verify_recovery` 완료(근거에 "복구 여부 미확인(verify 완료 기준)") → 없으면 None
  - resolved_notice_at: 두 번째로 완료된 `publish_notice`. mttr_seconds = recovered_at − detected_at
  - used_memory: `memory_referenced` 로그 유무. auto_handled_rate: `escalated` 없는 사건 비율
  - grade_counts(`execute_decided`), blocked_counts(`rejected` 사유별), escalation_count(`escalated`)
- 값을 정할 수 없으면 None·0으로 두고 `evidence.data_summary`에 "확인할 수 없음" 사유를 남긴다. 지금은 `execute_decided`·`rejected`·`escalated`를 쓰는 곳이 없어 실제 모드에서 이 셋은 0이다.
- LLM은 1차·2차 MTTR 비교 한 줄만 쓴다(`evidence.data_summary` 첫 줄). 숫자가 어긋나면 고정 문장.
- 목 정답: 1차 MTTR 9분(기억 참조 없음, 이관 1) / 2차 6분(기억 참조) → "2차 사건은 회사 기억을 참조해 1차보다 3분 빨리 복구했습니다." 등급 A3·B2·C1, 차단 3, 이관 1, 자동 처리율 0.5.

## 3. 입출력 계약
contracts/ 참조 (TODO)
## 4. 의존
ax-contracts, ax-agent-template
## 5. 하지 않는 것
(TODO: 담당자가 작성)
