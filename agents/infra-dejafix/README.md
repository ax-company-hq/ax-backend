# agents/infra-dejafix
담당: 예진 · 포트: 8107
## 1. 역할
인프라 에이전트(DejaFix). 시나리오 A(SCN-A 11·12장)에서 장애 원인 배포를 찾고, 롤백하고, 복구를 확인한다.

- 진단(`diagnose_incident`): 배포 이력에서 사건 구간 안의 배포를 원인 후보로 낸다. 확신도는 코드 규칙으로 정한다(LLM 안 씀).
  - 실제 모드: 구간(`INFRA_DIAGNOSE_LOOKBACK_MIN`, 기본 60분) 안 후보 1개 + 15분 이내 → high, 1개 + 그보다 오래됨 → mid, 여러 개 → 최신 mid·나머지 low, 없음 → `undetermined`
  - 목 모드: 최신 배포를 high로 낸다(시연 시각과 무관하게 같은 결과)
  - 복구 예상 시각은 내지 않는다
- 롤백(`rollback_deployment`, B): 대상 버전(`to_version`)은 이력에서 코드로 고른다(롤백된 배포는 건너뜀). 요청에 다른 버전이 오면 거부한다.
  - 경로 판정용 정수 4개를 코드로 계산해 params에 넣는다: `suspect_count`·`low_confidence`·`irreversible_count`·`prior_rollbacks`. safety 기준표에서 하나라도 걸리면 C(사람 승인)
  - `execute(rollback_deployment)` → safety가 planning `verify_rollback_target`으로 교차검증 → `executed`면 자동 경로(`path: auto`), `held`면 `input-required` + `review_id`. 사람이 승인한 뒤 같은 요청을 다시 보내면 같은 `idempotency_key`로 `executed`를 받아 실행한다(`path: human_approved`)
  - 시연의 "롤백"은 shop-api 장애 주입 해제(`DELETE /api/shop/admin/faults`)다
- 복구 확인(`verify_recovery`): 30초 간격 최대 5회(`INFRA_RECOVERY_ATTEMPTS`·`INFRA_RECOVERY_INTERVAL_S`). 인프라 지표(shop-api 장애 상태)와 업무 지표(data `anomaly_detection`: `payment_fail_rate`·`order_fail_count`)가 둘 다 정상이어야 `recovered`. 업무 지표를 못 받으면(`null`) 복구로 보지 않는다. 마감(`deadline`) 전에 멈춘다

코드 구성 (`src/infra_agent/`):

| 파일 | 내용 |
| --- | --- |
| `data/deployments.json` | shop-api 배포 이력 (planning 시드 PR #28 `shop-api v2.4.1`과 맞춤) |
| `deployments.py` | 배포 이력 읽기, 직전 정상 배포 찾기, 롤백 표시 |
| `diagnosis.py` | 원인 후보·확신도 규칙 |
| `clients.py` | shop-api·data 호출 (URL이 없으면 목) |
| `runtime.py` | 프로세스 메모리 상태 (진단 결과, 실행한 롤백) |
| `handlers.py` | A2A skill 3개 |

## 2. 실행 방법
- 서버: `docker compose -f infra/compose.yml up infra-dejafix`, 또는 레포 루트에서 `uv run uvicorn infra_agent.main:app --port 8107`
- 목 모드: `INFRA_MOCK=1` (또는 템플릿 공통 `AGENT_TEMPLATE_MOCK=1`). CI는 항상 1
  - `AX_SHOP_API_URL`·`AX_DATA_URL`이 없으면 shop-api·data는 목(장애 해제 성공, 업무 지표 정상)
  - 실행 관문은 템플릿 `execute()`를 그대로 쓴다
- 단독 테스트: `uv run pytest agents/infra-dejafix`
- 요청 보내 보기: `uv run ax-test-call --agent http://localhost:8107 --skill diagnose_incident --input input.json --as orchestrator`

## 3. 입출력 계약
A2A (`POST /a2a`, `agent-card.json` → `/.well-known/agent-card.json`). 입출력은 `contracts/src/ax_contracts/schemas/infra.py`, 공통 필드는 `message.py`.

| skill | 등급 | 요청 가능 | 입력 → 출력 |
| --- | --- | --- | --- |
| `diagnose_incident` | read | orchestrator, marketing, support | `DiagnoseIncidentInput` → `DiagnoseIncidentOutput`. evidence `ref_ids`는 `deploy:<deploy_id>` |
| `rollback_deployment` | B (검증 planning) | orchestrator | `RollbackDeploymentParams` → `{rolled_back_to, path, fault_released, post_check_ok}`. 보류면 `input-required` + `{review_id, hold_reason}`, 거절은 `completed` + outcome `rejected`, safety 무응답은 `failed` + `safety_unavailable`. 멱등 키 `<context_id>:rollback_deployment:<service>:<from_version>` |
| `verify_recovery` | read | orchestrator | `VerifyRecoveryInput` → `VerifyRecoveryOutput`. outcome `recovered` / `not_recovered` |

safety로 보내는 params: 계약 필드 + 정수 4개 + `deployed_at`(+ 받았으면 `incident_opened_at`). planning 교차검증이 이 두 시각을 쓴다.

## 4. 의존
- ax-contracts, ax-agent-template
- safety(실행 관문), planning(`verify_rollback_target`, safety가 부름)
- shop-api `/api/shop/admin/faults` (토큰 `infra`), data `anomaly_detection`

## 5. 하지 않는 것
- 원인·확신도·경로를 LLM으로 정하지 않는다. 등급(B/C)은 safety 기준표가 정한다
- 운영 서버에 셸·SSH로 접속하거나 배포 도구를 직접 부르지 않는다. 시연 롤백은 장애 주입 해제뿐이다
- 사람 승인을 대신하지 않는다. C는 보류 큐에서 사람이 결정한다
- 복구 예상 시각을 추정하지 않는다
- 미결: 계약 `to_version` 선택 필드화, 롤백 출력 계약, orchestrator의 `incident_opened_at` 전달, safety 기준표 4개 규칙(사람 PR), Zabbix 등록·`infra.anomaly_detected` 이벤트(DJF-02)
