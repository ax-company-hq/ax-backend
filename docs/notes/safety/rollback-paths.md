# rollback_deployment 처리 경로 (SCN-A, 인프라 합의)

담당: safety(태윤). 관련 부서: infra(예진).

## 1. 인프라가 보내야 하는 params (숫자 4개, 0 이상 정수)

| 필드 | 뜻 | C로 올라가는 조건 |
| --- | --- | --- |
| `suspect_count` | 원인 후보 배포 수 | 1보다 크면 (후보가 여러 개) |
| `low_confidence` | 원인 확신이 낮으면 1, 아니면 0 | 0보다 크면 |
| `irreversible_count` | 되돌릴 수 없는 변경(DB 마이그레이션 등) 수 | 0보다 크면 |
| `prior_rollbacks` | 같은 사건에서 이미 한 롤백 수 | 0보다 크면 |

- 필드가 없거나, 정수가 아니거나(문자열·소수·true/false·null), 음수면 **C로 보류**한다 (fail-closed).
- 넷 다 통과(예: `1, 0, 0, 0`)하면 B로 planning `verify_rollback_target` 교차검증을 받는다.

## 2. 기준표 규칙 (`safety/src/ax_safety/rules/thresholds.toml`)

**기준표에 들어갔다 (사람 PR #400, 태윤).** 아래 4개 규칙이 실제 기준표에 있고 값은 그대로다.
infra-dejafix가 `handlers.path_params`로 4개 정수를 계산해 `execute(rollback_deployment)` params에 넣는다
(#389 머지 후, 데모 기본 경로는 `1, 0, 0, 0` → B).
테스트(`safety/tests/test_registry_actions.py`)는 이제 테스트 전용 표가 아니라 실제 기준표로 확인한다
(`test_thresholds_toml_has_rollback_rules`, 규칙별 C 승격, 경계값 B 유지, fail-closed).

```toml
[[rules]]
key = "rollback_multiple_suspects"
action = "rollback_deployment"
metric = "param"
field = "suspect_count"
op = ">"
value = 1

[[rules]]
key = "rollback_low_confidence"
action = "rollback_deployment"
metric = "param"
field = "low_confidence"
op = ">"
value = 0

[[rules]]
key = "rollback_irreversible"
action = "rollback_deployment"
metric = "param"
field = "irreversible_count"
op = ">"
value = 0

[[rules]]
key = "rollback_repeat"
action = "rollback_deployment"
metric = "param"
field = "prior_rollbacks"
op = ">"
value = 0
```

주의: 4개 필드를 안 보내는 롤백 요청은 전부 C가 된다. 인프라는 #389부터 4개를 모두 보낸다.

## 3. 검증자 거절 → 사람 보류

- `registry.toml`의 `rollback_deployment`에 `on_verify_reject = "hold"`.
- planning이 **거절**하면 `rejected`로 끝내지 않고 C 보류(`held`, `hold_reason = verify_rejected`).
  보고서 `verification`에 검증자 결과(`result`, `detail` = status·reason_code 등)가 들어가고,
  `safety.action_held` 이벤트가 나간다.
- planning이 **무응답·오류**면 지금처럼 C 보류(`verify_timeout`).
- 사람이 대시보드에서 승인하면 **같은 `idempotency_key`로 다시 요청**했을 때 `executed`를 받는다.
  거절·10분 만료면 `rejected`.
- 다른 액션은 그대로: 쿠폰 등은 검증자 거절 = `rejected`, 보류 큐에 넣지 않는다 (팀 결정 12).
