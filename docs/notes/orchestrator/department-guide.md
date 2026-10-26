# 부서별 연동 안내서 (총괄이 내 skill을 부르는 방식)

> 이 문서는 `orchestrator/scripts/make_department_guide.py`가 plan을 실제로 돌려 만든다. 직접 고치지 않는다.
> 입력 예시는 가짜 부서로 돌린 값이다(실제 값은 앞 단계의 실제 결과로 채워진다).

## 내 에이전트를 총괄 목업에 붙여 보는 법
1. 내 에이전트를 로컬에서 띄운다(예: 재무 `8105`).
2. 총괄을 띄운다. 빈 포트를 찾아 목 모드로 뜨고 브라우저가 열린다(8000번을 다른 서비스가 써도 안전).
```
uv run python -m ax_orchestrator
```
   내 부서만 실제로 붙이려면(나머지는 가짜) 부서를 지정한다.
```
ORCHESTRATOR_REAL_DEPTS=finance uv run python -m ax_orchestrator
```
   - 터미널에 나온 `http://localhost:<포트>/demo`를 연다. 포트를 직접 정하려면 `--port 8002`, 브라우저를 안 열려면 `--no-browser`.
   - 주소는 `AX_<부서>_URL`(없으면 기본 포트 표), 여러 부서는 쉼표로(`hr,finance`).
   - 내 에이전트도 목 모드(`AGENT_TEMPLATE_MOCK=1`)면 토큰 검증이 맞는다.
3. `/demo`에서 장애를 주입한다. 부서 카드에 `실제`/`목` 표시가 나오고, 내 skill 호출이 사건 흐름에 보인다.
4. `GET /api/registry`로 내 카드가 제대로 읽혔는지 확인한다(`unavailable`에 내 부서가 있으면 카드 수집 실패).

## 이벤트를 총괄에 보내 보는 법 (발행하는 부서용)
총괄이 구독하는 이벤트와 판정은 아래와 같다. 실제 전달은 Redis Streams(agent-template TODO)로 바뀌지만 판정 규칙은 같다. 지금은 개발용 입구 `POST /api/events`에 `EventEnvelope` JSON을 보내 확인한다.

| 이벤트 | 총괄의 판정 |
|---|---|
| `shop.degraded`, `infra.anomaly_detected` | 사건 열기. 같은 `component`의 열린 사건이 있으면 합침 |
| `metric.anomaly` | `data.severity`가 high·critical이면 사건, 아니면 기록만 |
| `shop.recovered`, `support.inquiry_received`, `support.inquiry_surge` | 사건 아님. 열린 사건이 있으면 근거로 기록 |
| 그 밖의 이벤트 | 무시 |

```
curl -X POST localhost:<포트>/api/events -H 'Content-Type: application/json' -d '{
  "event_id": "evt_demo01", "type": "shop.degraded", "occurred_at": "2026-10-16T01:00:00Z",
  "source": "shop-api", "idempotency_key": "evt_demo01",
  "data": {"component": "payment", "metric_code": "error_rate_5xx", "value": 0.42}}'
```
응답 `data.outcome`: `opened`(새 사건) · `merged`(열린 사건에 합침) · `noted`(기록만) · `ignored`(소관 아님) · `duplicate`(같은 `event_id`). 사건은 `GET /api/incidents`, `/api/incidents/{context_id}`와 `/demo`에서 본다.

## 총괄의 공통 규칙
- 모든 요청 `meta.path`는 `["orchestrator"]`로 시작, `idempotency_key = <context_id>:<skill>:<단계 id>`, `deadline`은 호출 시각 + 30초.
- 실패(`failed`)는 2번, 마감 초과(`canceled`)는 1번 재시도한다. `rejected`(`FORBIDDEN_SKILL`·`HOP_LIMIT`·소관 아님)는 같은 skill을 가진 다른 부서로 이관한다. 그런 부서가 없으면 지어내지 않고 단계를 "이관 큐로 넘김"(`transfer_queued`)으로 표시한다.
- `input-required`: `dept_input`이면 `output.question = {skill, input}`을 총괄이 해당 부서에 물어 답을 `input.answers`로 넣어 다시 부른다. `human_input`이면 사람 승인을 기다리고(총괄은 승인하지 않음), 마감이 지나면 자동 거절로 처리한다.
- 카드에 없는 skill, 총괄이 요청할 수 없는 skill(`allowed_requesters`)은 "확인할 수 없음"으로 건너뛴다.
- 문의 경로의 보상·쿠폰은 총괄이 부르지 않는다(#228 결정 A). 고객지원이 문의 접수 때 처리하고 마케팅에 직접 요청한다. 총괄은 영향 고객 일괄 보상만 고객지원 `request_incident_compensation`으로 요청한다.
- 계획대로 안 될 때: 원인 미확정(`diagnose_incident`가 `undetermined`)이면 롤백하지 않고 이관 큐로, `verify_recovery`가 `recovered=false`이면 정상화 공지 없이 이관 큐로 돌린다. 복구 확인은 인프라가 30초 간격으로 다시 확인하는 시간을 기다리도록 마감을 180초로 준다.


## infra

| skill | 등급 | 단계 | 선행 단계 | 필수 | 입력 예시 |
|---|---|---|---|---|---|
| `diagnose_incident` | read | 원인 진단 (infra) | 없음 (사건 시작 즉시) | 예 | `{"component": "payment"}` |
| `rollback_deployment` | B | 원인 배포 롤백 (B) | diagnose, impact | 예 | `{"from_version": "v2.4.1", "release_fault_injection": true, "service": "payment-api", "to_version": "v2.4.0"}` |
| `verify_recovery` | read | 복구 검증 (infra) | rollback | 예 | `{"component": "payment", "since": "2026-10-16T01:00:00Z"}` |

> 확인 필요: `rollback_deployment`의 `to_version`은 진단 결과(`SuspectDeployment`)에 이전 버전이 없어 출처가 정해지지 않았다. 지금 목은 `previous_version`을 가정한다. infra가 어디서 정할지 알려 주세요.

## data

| skill | 등급 | 단계 | 선행 단계 | 필수 | 입력 예시 |
|---|---|---|---|---|---|
| `impact_analysis` | read | 영향 분석 (data) | 없음 (사건 시작 즉시) | 예 | `{"scope": "payment", "window_from": "2026-10-16T00:45:00Z", "window_to": "2026-10-16T01:15:00Z"}` |
| `kpi_summary` | read | 처리 시간 요약 (data) | recovered_notice(끝나면) | 아니오 | `{"context_ids": ["ctx_inc_guide"]}` |

## planning

| skill | 등급 | 단계 | 선행 단계 | 필수 | 입력 예시 |
|---|---|---|---|---|---|
| `find_related_prs` | read | 관련 PR 찾기 (planning) | diagnose, impact | 아니오 | `{"component": "payment", "window_from": "2026-10-16T00:45:00Z", "window_to": "2026-10-16T01:15:00Z"}` |
| `link_incident` | A | 사건에 PR 연결 (planning) | related_prs | 아니오 | `{"incident_context_id": "ctx_inc_guide", "pr_numbers": [412, 418]}` |

## marketing

| skill | 등급 | 단계 | 선행 단계 | 필수 | 입력 예시 |
|---|---|---|---|---|---|
| `draft_notice` | read | 점검 공지 초안 (marketing) | diagnose, impact | 예 | `{"facts": {"component": "payment", "opened_at": "2026-10-16T01:00:00Z"}, "kind": "incident_opened"}` |
| `publish_notice` | A | 점검 공지 게시 (marketing) | notice | 예 | `{"draft_id": "drf_incident_opened"}` |
| `draft_notice` | read | 정상화 공지 초안 (marketing) | recovery | 예 | `{"facts": {"component": "payment", "opened_at": "2026-10-16T01:00:00Z", "resolved_at": "2026-10-16T01:00:00Z"}, "kind": "incident_resolved"}` |
| `publish_notice` | A | 정상화 공지 (marketing) | recovered_draft | 예 | `{"draft_id": "drf_incident_resolved"}` |

## support

| skill | 등급 | 단계 | 선행 단계 | 필수 | 입력 예시 |
|---|---|---|---|---|---|
| `request_incident_compensation` | A | 영향 고객 일괄 보상 (support) | impact | 아니오 | `{"buyer_ids": ["buy_0001", "buy_0002", "buy_0003", "buy_0004", "buy_0005", "buy_0006", "buy_0007", "buy_0008", "buy_0009", "buy_0010", "buy_0011", "buy_0012", "buy_0013", "buy_0014", "buy_0015", "buy_0016", "buy_0017", "buy_0018", "buy_0019", "buy_0020", "buy_` |

> 총괄이 부르는 skill은 영향 고객 일괄 보상 하나다. 문의 접수 때 자동응답·보상 판정·마케팅 쿠폰 요청은 고객지원이 직접 한다(#228 결정 A). 고객 목록은 데이터 영향 분석 결과(`affected_customer_ids`)를 그대로 넘기며, 50명을 넘으면 고객지원이 전체를 거부한다. 총괄은 `support.inquiry_received`·`support.inquiry_surge` 이벤트도 구독한다.

## finance

| skill | 등급 | 단계 | 선행 단계 | 필수 | 입력 예시 |
|---|---|---|---|---|---|
| `loss_aggregation` | read | 손실 리포트 (finance) | impact, publish_notice(끝나면), compensation(끝나면) | 예 | `{"context_id": "ctx_inc_guide", "window_from": "2026-10-16T00:45:00Z", "window_to": "2026-10-16T01:15:00Z"}` |
| `audit_report` | read | 사후 감사 (finance) | compensation(끝나면), loss(끝나면) | 아니오 | `{"context_id": "ctx_inc_guide"}` |

> 재무는 영향 숫자를 data에서 직접 조회하고 보상액은 자기 장부에서 센다. 총괄은 구간과 `context_id`만 준다. `audit_report`는 이 사건의 쿠폰·실행 기록 점검을 사건 끝 무렵에 요청한다. `verify_amount`는 총괄이 부르지 않는다(요청 가능 부서가 safety뿐).

## hr

| skill | 등급 | 단계 | 선행 단계 | 필수 | 입력 예시 |
|---|---|---|---|---|---|
| `record_case` | A | 사건 기록 (hr) | recovered_notice(끝나면), link_incident(끝나면), loss(끝나면), compensation(끝나면), audit(끝나면), kpi(끝나면) | 예 | `{"alternatives_considered": [], "area": "infra", "author": "orchestrator", "context_id": "ctx_inc_guide", "decision_reason": "scenario_a plan 실행 결과 — 완료: diagnose_incident, impact_analysis, rollback_deployment, find_related_prs, link_incident, draft_notice, pu` |
