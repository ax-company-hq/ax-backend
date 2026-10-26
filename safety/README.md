# safety
담당: 태윤 · 포트: 8001
## 1. 역할
모든 에이전트의 상태 변경이 지나는 **안전 레이어**(실행 관문의 서버 쪽). 등급은 LLM이 아니라 레지스트리와 기준표가 정한다.

- 등급 판정 (SAF-01): 레지스트리 기본 등급과 금액 규칙 중 높은 쪽. `claimed_grade`는 기록만 한다
- 레지스트리·기준표 (SAF-02): `src/ax_safety/rules/registry.toml`, `thresholds.toml`. 사람만 PR로 바꾼다
- A 즉시 실행 (SAF-03), B 교차검증 (SAF-04): 레지스트리의 검증자 skill(planning `verify_rollback_target`, data `fraud_score` 등)을 A2A로 부른다. 쿠폰·환불은 A이고 금액 규칙(계산·불일치·한도)에 걸리면 C
- C 보류 큐 (SAF-05): 근거 리포트와 함께 보류, 사람 토큰으로만 결정, 기한 안에 결정이 없으면 자동 거절. 기한은 데모 기본 10분, 운영은 `SAFETY_REVIEW_TIMEOUT_SECONDS`(예: 86400=24시간)로 덮어쓴다
- 감사 로그 (SAF-06): 접수·검증·판정·인증 거부·보류 결정을 `context_id`와 함께 남긴다
- 공통 로그 (COM-08): 판정마다 `execute_decided`(grade·status·action·requester), 인증 거부는 `rejected`(`reason_code` = `UNAUTHORIZED`/`FORBIDDEN`)를 `AX_COMMON_LOG_DB`에 남긴다. 대시보드 "안전 관문 로그"와 data `kpi_summary`가 이것을 읽는다. 계약 `LogKind`에 없는 종류는 저장소에 남지 않는다
- fail-closed (SAF-08): 미등록 액션·금액 필드 오류·총액 불일치·검증 무응답은 실행하지 않는다
- 누적 한도 (SAF-10): 일일(UTC)·사건별·고객별 누적 금액 규칙
- 규칙 기반 검증 (SAF-11): 금액은 코드가 건수 × 단가로 다시 계산한다

판정 흐름: 멱등성 → 레지스트리(요청자 = owner) → 금액 계산·총액 대조 → 금액 규칙 → A 실행 / B 교차검증 / C 보류 → 감사 로그·`safety.*` 이벤트.
`executed`는 "요청 에이전트가 이제 쓰기 도구를 불러도 된다"는 뜻이고, 실제 쓰기는 요청 쪽이 한다. C 건을 사람이 승인하면 같은 `idempotency_key`로 다시 요청할 때 `executed`를 받는다.

## 2. 실행 방법
`docker compose -f infra/compose.yml up safety` / 목 모드: `SAFETY_MOCK=1` (토큰 `mock:<에이전트>`, 사람 토큰 `human:<이름>`)

테스트: `uv run pytest safety`

```bash
# 쿠폰 실행 요청 (마케팅 토큰)
curl -X POST localhost:8001/api/safety/executions -H 'Authorization: Bearer mock:marketing' \
  -H 'Content-Type: application/json' -d '{"action":"issue_coupon","requester":"marketing",
  "context_id":"ctx_inc_0001","idempotency_key":"ctx_inc_0001:issue_coupon:buy_a1",
  "params":{"purpose_code":"campaign","buyer_id":"buy_a1","count":20,"unit_value":20000}}'
# → C 보류 (400,000원 ≥ coupon_total 300,000원). 보류 큐 확인과 사람 결정
curl localhost:8001/api/safety/reviews?status=pending
curl -X POST localhost:8001/api/safety/reviews/<review_id>/decision \
  -H 'Authorization: Bearer human:taeyoon' -d '{"decision":"rejected"}' -H 'Content-Type: application/json'
```

## 3. 입출력 계약
| 메서드·경로 | 용도 | 토큰 |
| --- | --- | --- |
| `POST /api/safety/executions` | 실행 관문. `ExecuteRequest` → `{data: ExecuteDecision}` | 에이전트 (`requester`와 같아야 함) |
| `GET /api/safety/executions?context_id=` | 실행 기록 | — |
| `GET /api/safety/reviews?status=pending` | C 보류 큐 | — |
| `GET /api/safety/reviews/{review_id}` | 보류 건과 근거 리포트 | — |
| `POST /api/safety/reviews/{review_id}/decision` | `{decision: approved\|rejected, note}` | **사람만** |
| `GET /api/safety/audit-logs?context_id=` | 감사 로그 | — |
| `GET /api/safety/thresholds`, `GET /api/safety/registry` | 기준표·레지스트리 | — |

- 요청·판정 형식은 `contracts`의 `ExecuteRequest`, `ExecuteDecision`, `HoldReason`.
- B 교차검증 요청 입력(검증자 skill의 `input`): `execution_id`, `action`, `requester`, `params`, `computed_amount`, `threshold_version`. 검증자는 `completed` + `decision: "approved"`(또는 `approved: true`)일 때만 승인이다.
- 발행 이벤트: `safety.action_executed`, `safety.action_held`, `safety.review_decided` (data 모델은 contracts 요청 중, 지금은 공통 구성안 6장 필드의 dict).

## 4. 의존
ax-contracts, ax-agent-template (`call_agent`, `publish`, 토큰 검증), 검증자 에이전트(planning `verify_rollback_target`, data)

## 5. 하지 않는 것
- 쓰기 도구를 직접 호출하지 않는다 (판정만 하고 실행은 요청 에이전트가 한다)
- LLM을 쓰지 않는다 (서비스, 코드 규칙만)
- 기준표·레지스트리를 API로 바꾸지 않는다 (사람이 PR로만)
- 부서별 보류 목록을 따로 두지 않는다 (C 보류 큐는 여기 하나)
- 금액이 아닌 규칙(`param`·`paths_within`·`paths_avoid`): `pr_changed_lines`·`pr_changed_files`·`pr_allowed_paths`·`pr_protected_paths`(planning `generate_pr`, 검증자 = 안전 레이어 코드 규칙), `merge_changed_lines`·`merge_changed_files`·`merge_allowed_paths`·`merge_protected_paths`·`pr_review_mismatch`·`pr_linked_incident`·`merge_irreversible`(planning `merge_pr`), `seller_risk`(판매자 심사). 값은 제안값. 규칙 키는 파일 안에서 겹치면 로드를 거부한다
- 보호 경로(`paths_avoid`, #169): `changed_paths` 중 하나라도 `protected` 목록의 경로와 같거나 그 아래면 걸려서 항상 C다. 결제 화면·로그인·API 클라이언트·환경값(shop-web), DB·주문/결제·쿠폰·시드(shop-api), `.github/`·`safety/`·`contracts/`. planning이 보낸 `irreversible_count`와 상관없이 안전 담당이 정한다. 필드가 없거나 문자열 목록이 아니면 걸린 것으로 본다(fail-closed)
- v0은 저장소가 메모리라 재시작하면 기록이 비워진다 (TODO: postgres)
