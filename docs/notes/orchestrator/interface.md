# 통합 에이전트(orchestrator) 인터페이스 (ORC-01)

담당 준하 · 포트 8000. 이 문서가 총괄이 받는 것과 내는 것의 기준이다. 계약 모델(`ax_contracts`)을 그대로 쓰고 새 계약은 만들지 않는다.

## 1. 받는 것
| 입구 | 형식 | 처리 |
| --- | --- | --- |
| 이벤트 버스 | `EventEnvelope` | `event_id` 중복 제거 → 사건 판단 → 사건이면 `ctx_inc_<hex>` 발급 |
| (후속) `POST /a2a` | `AgentRequest` | 총괄 자신의 skill(`get_incident`, `list_incidents`)은 O5 이후 |

구독 이벤트: `shop.degraded`, `shop.recovered`, `infra.anomaly_detected`, `metric.anomaly`, `support.inquiry_received`, `support.inquiry_surge`.

사건 판단 규칙(코드):
| 이벤트 | 판정 |
| --- | --- |
| `shop.degraded`, `infra.anomaly_detected` | 사건 열기 (열린 사건이 같은 `component`면 합침) |
| `metric.anomaly` | `severity`가 high 이상이면 사건 (아니면 기록만) |
| `shop.recovered` | 열린 사건에 "복구 이벤트 수신" 기록 |
| `support.*` | 사건 아님. 열린 사건이 있으면 근거로 붙임 |

## 2. 내는 것
| 출구 | 형식 | 내용 |
| --- | --- | --- |
| 부서 호출 | `AgentRequest` → `POST {부서}/a2a` | `meta.context_id`, `path`(자기 이름 추가), `deadline`, `idempotency_key=<context_id>:<skill>:<step_id>`, 외부 텍스트는 `untrusted` |
| 이벤트 | `EventEnvelope` | `incident.opened`, `incident.resolved` |
| 인사 기록 | `record_case` | 사건 종료 때 1회. `decision_reason`·`alternatives_considered`는 실제 단계 결과(완료·미완료·이관)에서 채움 |
| 읽기 API (목·화면용) | JSON | `GET /api/incidents`, `/api/incidents/{id}`, `/api/registry` |

## 3. 사건 상태
`open` → `resolved`(모든 필수 단계 완료) / `degraded`(일부 단계 실패·보류·건너뜀으로 종료).
단계 상태: `pending` `running` `completed` `failed` `held`(사람 승인 대기) `rejected_by_human` `skipped`(부서 없음/선행 실패, 사유 표시).

## 4. 호출 규칙
- 호출할 부서는 Agent Card의 skill→부서 표로 찾는다. 카드 수집 실패·skill 없음 → 해당 단계는 `skipped`("확인할 수 없음")로 보고하고 사건은 계속한다.
- `allowed_requesters`에 `orchestrator`가 없는 skill은 호출하지 않는다.
- 병렬 가능한 단계(선행이 끝난 것)는 동시에 호출한다. 같은 `serial_group`은 한 줄로(`sequence` 부여).
- 재시도: `failed` 2회, `canceled`(마감 안 지났을 때) 1회, `rejected`는 재시도하지 않고 이관 큐로.
- `input-required`:
  - `dept_input`: 결과의 `output.question`(`skill`, `input`)을 맞는 부서에 묻고, 답을 `input.answers`에 담아 같은 `context_id`로 다시 호출
  - `human_input`: 단계를 `held`로 두고 승인 대기열에 노출. 총괄은 승인하지 않는다. 마감(`hold_timeout`, 데모 기준 10분) 지나면 자동 거절로 처리
  - `customer_input`: 단계를 `skipped`(고객 자료 대기)로 두고 사건 요약에 남김
- 결과에 없는 값은 지어내지 않는다("확인할 수 없음").

## 5. 사건 계획(plan)
시나리오는 코드가 아니라 데이터다(`plans/scenario_a.py`). 단계 = `id`, `skill`, `after`(선행·성공 필요), `also_after`(끝났으면 됨), `serial_group`, `required`. 엔진은 plan만 실행한다. 시나리오 A 순서는 `docs/planning.md` 5장 "시나리오 A"를 따른다.

## 6. 호출 규칙 보충
- 모든 새 요청의 `meta.path`는 `["orchestrator"]`로 시작한다(루프·3홉 판단 기준).
- `loss_aggregation`에는 구간(`window_from`·`window_to`)과 `context_id`만 준다. 영향 숫자는 재무가 data에서 직접 조회하고 보상액은 재무 장부에서 센다.
- `verify_amount`는 총괄이 부르지 않는다(요청 가능 부서가 safety뿐). 쿠폰 교차검증은 마케팅 `execute()` → safety → finance 경로다.

## 7. 연동·확인
- 부서별 호출 입력과 내 에이전트를 총괄 목업에 붙여 보는 법: `department-guide.md`(plan에서 자동 생성)
- 보상·쿠폰은 총괄이 부르지 않는다(#228 결정 A). 고객지원이 문의 접수 때 처리한다.

## 8. 하지 않는 것
분석 문장·금액·등급 생성, C 승인, 부서 DB 직접 접근, 외부 텍스트를 지시로 해석.
