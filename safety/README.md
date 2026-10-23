# safety
담당: 태윤 · 포트: 8001
## 1. 역할
모든 에이전트의 상태 변경이 지나는 **안전 레이어**(실행 관문의 서버 쪽). 등급은 LLM이 아니라 레지스트리와 기준표가 정한다.

- 등급 판정 (SAF-01): 레지스트리 기본 등급과 금액 규칙 중 높은 쪽. `claimed_grade`는 기록만 한다
- 레지스트리·기준표 (SAF-02): `src/ax_safety/rules/registry.toml`, `thresholds.toml`. TOML이 원래 값이다. 레지스트리와 `[auto_review]`·경로 목록은 사람만 PR로 바꾼다
- 기준 바꾸기 (재무 AI 결정): 숫자 기준(`rules`의 `value`, `review_timeout_seconds`)은 대시보드에서 요청하면 안전 장치(코드) → 재무 AI `review_threshold_change` 순서로 결정해 실행 중에 바로 적용한다(version + 1). 재무 AI가 사람에게 넘기거나 답하지 않으면 재무 담당자가 승인·거절한다. 아래 "기준 바꾸기" 참고
- A 즉시 실행 (SAF-03), B 교차검증 (SAF-04): 레지스트리의 검증자 skill(재무 `verify_amount` 등)을 A2A로 부른다
- C 보류 큐 (SAF-05): 근거 리포트와 함께 보류, 사람 토큰으로만 결정, 기한 안에 결정이 없으면 자동 거절. 기한은 데모 기본 10분, 운영은 `SAFETY_REVIEW_TIMEOUT_SECONDS`(예: 86400=24시간)로 덮어쓴다
- AI 1차 판단: C 건 중 요청이 잘못된 것(금액 불일치·미등록·형식 오류)은 규칙이 바로 거절하고, 쿠폰·환불·판매자 심사는 재무 AI `review_hold`에 먼저 묻는다(승인·거절·사람에게). 재무 AI가 답하지 않거나 사람에게라고 하면 사람이 결정한다. 정산 확정은 항상 사람. 설정: `thresholds.toml`의 `[auto_review]`(사람만 변경, 끄면 모든 C 건을 사람이 결정).
  - **기본 꺼짐**: 기준표에서 켜져 있어도 환경변수 `SAFETY_AUTO_REVIEW=1`일 때만 돈다. 기준 문서(SAF-05 "담당자가 결정")를 바꾸는 docs PR이 머지되기 전에는 켜지 않는다
  - 자기 검증 금지: 요청자가 검토자(`reviewer`, finance)인 액션(예: `seller_review`)은 AI에게 묻지 않고 사람에게 둔다. 기준표 `actions`에 있으면 시작할 때 경고를 남기고 뺀다
  - 재무 AI 입출력은 계약 `ReviewHoldInput`/`ReviewHoldOutput`(contracts)이고, 형식이 다르면 사람에게 넘긴다
- 감사 로그 (SAF-06): 접수·검증·판정·인증 거부·보류 결정을 `context_id`와 함께 남긴다
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
| `GET /api/safety/thresholds` | 지금 쓰는 기준표 + `baseline`(TOML 원래 값, 바꿀 수 있는 기준만) | — |
| `GET /api/safety/registry` | 레지스트리 | — |
| `POST /api/safety/threshold-changes` | `{key, new_value, reason}` → 기준 바꾸기 기록 | 사람 또는 finance |
| `GET /api/safety/threshold-changes` | 기준 바꾸기 기록 (최근 것부터) | — |
| `POST /api/safety/threshold-changes/{change_id}/decision` | `{decision: approved\|rejected, note}` | **사람만** |

- 요청·판정 형식은 `contracts`의 `ExecuteRequest`, `ExecuteDecision`, `HoldReason`.
- B 교차검증 요청 입력(검증자 skill의 `input`): `execution_id`, `action`, `requester`, `params`, `computed_amount`, `threshold_version`. 검증자는 `completed` + `decision: "approved"`(또는 `approved: true`)일 때만 승인이다.
- 발행 이벤트: `safety.action_executed`, `safety.action_held`, `safety.review_decided` (data 모델은 contracts 요청 중, 지금은 공통 구성안 6장 필드의 dict).

### 기준 바꾸기

흐름: 요청(사람 토큰 또는 finance 토큰) → 안전 장치(코드) → 재무 AI → 적용·거절·사람 대기.

| 안전 장치 (코드, 재무 AI·사람도 못 넘음) | 걸리면 |
| --- | --- |
| 바꿀 수 있는 기준: 숫자 규칙(`coupon_total` 등)과 `review_timeout_seconds`만. `auto_review.*`, 경로 목록(`paths_within`·`paths_avoid`: `pr_allowed_paths`·`merge_allowed_paths`·`pr_protected_paths`·`merge_protected_paths`), `verify_timeout_seconds`, 규칙 삭제는 불가 | `rejected` (`decided_by: guardrail`) |
| 새 값은 양의 정수, TOML 원래 값의 0.5배 ~ 2배 | 〃 |
| `review_timeout_seconds`는 3600 ~ 259200초 | 〃 |
| 이유는 1 ~ 300자, 믿지 않는 글로 다룬다(재무 AI에는 A2A `untrusted`로만 넘김) | 〃 |
| 지금 값과 같으면 | 〃 |

- 재무 AI 입력(`input`): `change_id, key, current_value, new_value, baseline, stats`. `stats`는 safety 기록에서 코드로 센다:
  `action`, `holds_24h`(최근 24시간 그 기준에 걸려 멈춘 건수, 마감 기준은 모든 보류), `executed_today`(그 액션의 오늘 실행 합계),
  `ai_approved_24h`, `human_approved_24h`, `expired_24h`. 이유는 `untrusted.reason_untrusted_text`.
- 재무 AI 답: `approve` → 적용(`applied`, `version_after`), `reject` → `rejected`, `escalate`·무응답·오류 → `pending`(사람 결정).
- 기록 필드: `change_id(thc_…), key, old_value, new_value, baseline, reason, requested_by, status(pending|applied|rejected),
  decided_by(guardrail|finance-ai|사람 이름), explanation, created_at, decided_at, version_after, ai_review, stats`.
- 이미 결정된 건에 사람 결정을 보내면 409. 사람이 승인해도 안전 장치를 다시 검사한다.
- 감사 로그: `threshold_change_requested|ai_review|applied|rejected` (`context_id = ctx_req_<change_id>`).
- 바뀐 기준은 메모리에만 있다(TODO: persist). 재시작하면 TOML 원래 값으로 돌아간다.

```bash
curl -X POST localhost:8001/api/safety/threshold-changes -H 'Authorization: Bearer human:taeyoon' \
  -H 'Content-Type: application/json' -d '{"key":"coupon_total","new_value":350000,"reason":"보류가 너무 많음"}'
```

## 4. 의존
ax-contracts, ax-agent-template (`call_agent`, `publish`, 토큰 검증), 검증자 에이전트(finance `verify_amount`, planning `verify_rollback_target`, data)

## 5. 하지 않는 것
- 쓰기 도구를 직접 호출하지 않는다 (판정만 하고 실행은 요청 에이전트가 한다)
- LLM을 쓰지 않는다 (서비스, 코드 규칙만)
- 레지스트리, `[auto_review]`, 경로 목록, 안전 장치 범위는 API로 바꾸지 않는다 (사람이 PR로만). API로는 숫자 기준만 안전 장치 범위 안에서 바꾼다
- 부서별 보류 목록을 따로 두지 않는다 (C 보류 큐는 여기 하나)
- 금액이 아닌 규칙(`param`·`paths_within`·`paths_avoid`): `pr_changed_lines`·`pr_changed_files`·`pr_allowed_paths`·`pr_protected_paths`(planning `generate_pr`, 검증자 = 안전 레이어 코드 규칙), `merge_changed_lines`·`merge_changed_files`·`merge_allowed_paths`·`merge_protected_paths`·`pr_review_mismatch`·`pr_linked_incident`·`merge_irreversible`(planning `merge_pr`), `seller_risk`(판매자 심사). 값은 제안값. 규칙 키는 파일 안에서 겹치면 로드를 거부한다
- 보호 경로(`paths_avoid`, #169): `changed_paths` 중 하나라도 `protected` 목록의 경로와 같거나 그 아래면 걸려서 항상 C다. 결제 화면·로그인·API 클라이언트·환경값(shop-web), DB·주문/결제·쿠폰·시드(shop-api), `.github/`·`safety/`·`contracts/`. planning이 보낸 `irreversible_count`와 상관없이 안전 담당이 정한다. 필드가 없거나 문자열 목록이 아니면 걸린 것으로 본다(fail-closed)
- v0은 저장소가 메모리라 재시작하면 기록이 비워진다 (TODO: postgres)
