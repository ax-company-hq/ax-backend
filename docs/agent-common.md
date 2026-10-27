# 에이전트 공통 구성안 — 6개 부서 통합 기준

Oct 21, 2026 · @재현

## 1. 개요

6개 부서 에이전트 구성안(기획개발·고객지원·마케팅·인사·재무/정산·데이터분석)을 비교해, 모든 에이전트가 똑같이 따라야 하는 부분만 뽑아 하나로 정한 문서다. 각 구성안은 이 문서의 공통 항목을 다시 정의하지 않고 "공통안 n장을 따른다"로 쓰고, 자기 부서만의 내용(skill, 흐름, 연동 상세)만 담는다.

- **우선순위**: 이 문서 > 각 부서 구성안. 충돌하면 이 문서를 따르고, 바꾸려면 이 문서를 먼저 고친다.
- **출처**: 6개 구성안 중 가장 잘 정리된 안을 기준으로 채택했다. 공통 봉투·이벤트 원칙은 마케팅(재헌), 안전 레이어·실행 관문은 재무/정산(태윤), Task 상태 매핑은 고객지원(병환)·기획개발(예진), 결정 카드 패턴은 인사(준하), 회사 기억은 데이터분석(재현) 안이 기준이다.
- **표기**: \[공통\] 이 문서로 확정하는 안, 13장 안건 8개는 2026-10-21 추천안대로 확정했다.

## 2. 에이전트와 서비스 목록

에이전트는 8개(부서 6 + DejaFix 래퍼 + 통합 에이전트)이고, 나머지는 LLM 없이 코드로만 도는 서비스다. 서비스에는 LLM을 넣지 않는다.

**에이전트 (LLM이 판단, Agent Card로 A2A 통신)**

| 이름 | 부서 | 담당 | 포트 | 한 줄 역할 |
| --- | --- | --- | --- | --- |
| `orchestrator` | 통합 | 준하 | 8000 | 사건 개시·분배·취합·종료, 이관 처리 |
| `planning` | 기획개발 | 예진 | 8101 | 변경 요청 허브, 장애-PR 연결 |
| `support` | 고객지원 | 병환 | 8102 | 문의 응대, 클레임 판정, 개별 보상 |
| `marketing` | 마케팅 | 재헌 | 8103 | 공지, 캠페인, 사건 보상 쿠폰 |
| `hr` | 인사 | 준하 | 8104 | 업무 기록·결정 카드, 인수인계 |
| `finance` | 재무/정산 | 태윤 | 8105 | 금액 교차검증, 대사·정산, 손실 리포트 |
| `data` | 데이터분석 | 재현 | 8106 | 업무 지표 이상 탐지, 영향 분석 |
| `infra` | 인프라 (DejaFix 래퍼) | 예진 | 8107 | 장애 진단, 롤백, 복구 확인 |

**서비스 (코드만, LLM 없음)**

| 이름 | 담당 | 포트 | 역할 |
| --- | --- | --- | --- |
| `shop-api` | 재헌 | 8010 | 판매 사이트: 주문·결제·상품·판매자 데이터 원천, 이벤트 발행 |
| `safety` | 태윤 | 8001 | 안전 레이어: 실행 관문, 등급 판정, 보류 큐, 감사 로그 |
| `memory` | 재현 | 8002 | 회사 기억: 모든 카드의 저장·검색 |
| `dashboard-api` | 병환 | 8020 | 대시보드 서버 (공통 로그 저장소만 읽음) |
| `contracts`, `agent-template` | 준하 | — | 계약 스키마, 공통 뼈대 (코드 패키지) |
| 이벤트 버스 (Redis) | 공동 | 6379 | 이벤트 전달 |

폴더 이름은 `agents/infra-dejafix/`, A2A 상의 에이전트 이름은 `infra`로 쓴다.

## 3. 공통 표기·형식

6개 안 중 4개(마케팅·재무·고객지원·기획개발)가 이미 쓰는 형식을 기준으로 맞춘다.

| 항목 | \[공통\] 규칙 | 예 |
| --- | --- | --- |
| 필드 이름 | 우리가 정의하는 모든 필드는 **snake\_case**. A2A SDK가 정한 프로토콜 필드만 SDK 그대로 둔다 | `context_id`, `tenant_id`, `idempotency_key` |
| ID | 접두사 + 문자열 | `ctx_`, `evt_`, `ord_`, `pay_`, `prd_`, `sel_`, `buy_`, `cpn_`, `exe_`, `rev_`, `card_` |
| context\_id 접두사 | 사건은 `ctx_inc_`, 변경 요청은 `ctx_req_`, 공지·패턴은 `ctx_ntc_`, 캠페인은 `ctx_cmp_`, 화면(대시보드) 호출은 `ctx_ui_`(dashboard-api가 발급), 쇼핑몰에서 시작한 고객 클레임은 `ctx_shop_`(shop-api가 발급) | `ctx_inc_0001` |
| 구매자 ID (`buyer_id`) | `buy_` + 4자리 숫자(0 채움), 가명 ID. 사용자 번호 N은 `buy_{N:04d}`(1번 → `buy_0001`)로 대응한다. shop-api가 발급하고 개인정보를 담지 않는다 | `buy_0001` |
| tenant\_id | 모든 테이블·요청·이벤트·카드에 포함, 값은 하나 | `shop-demo` |
| 시간 | UTC ISO8601로 저장, 화면에서만 KST | `2026-10-21T05:41:00Z` |
| 금액 | 정수 원(KRW) | `30000` |
| API 경로 | `/api/<부서>/<복수 명사>` | `/api/support/claims` |
| API 응답 | 성공 `{data, meta}`, 실패 `{error: {code, message}}` |  |
| 환경변수 | 부서 접두사 대문자. 공통 값은 접두사 없이 | `SUPPORT_MOCK`, `PLANNING_LLM_MODEL` |
| 테이블 | 복수형 snake\_case, 모두 `tenant_id`·`context_id` 포함 | `claims`, `audit_logs` |
| 커밋 | Conventional Commits (영문 접두사) | `feat(data): add impact analysis` |
| 브랜치 | dev에서 `폴더/기능` | `data/impact-analysis` |

## 4. Agent Card 공통 형식

구성안마다 `action_grade`, `acl`, `grade`, `allowedRequesters` 등 이름이 달랐다. 아래 형식 하나로 맞추고, 안전 레이어의 액션 등급 레지스트리와 값이 다르면 레지스트리가 우선한다.

```json
{
  "name": "data",
  "owner": "재현",
  "description": "업무 지표 이상 탐지, 장애 영향 분석",
  "url": "http://data:8106/a2a",
  "version": "0.1.0",
  "capabilities": { "streaming": true, "push_notifications": false },
  "skills": [
    {
      "id": "impact_analysis",
      "description": "장애 구간을 받아 실패 주문 수, 영향 고객 ID, 피해 금액을 계산한다. 상태를 바꾸지 않는다.",
      "input_schema": "contracts/schemas/data/impact_analysis_input.json",
      "output_schema": "contracts/schemas/data/impact_analysis_output.json",
      "grade": "read",
      "allowed_requesters": ["orchestrator", "infra", "support", "finance", "planning"],
      "verifier": null,
      "priority": "required"
    }
  ]
}
```

| 필드 | 규칙 |
| --- | --- |
| `grade` | `read`(상태 변경 없음) / `A` / `B` / `C`. 기본 등급이며 실제 등급은 안전 레이어가 금액·누적으로 올릴 수 있다 |
| `allowed_requesters` | 이 skill을 요청할 수 있는 에이전트 이름. 처음엔 좁게 |
| `verifier` | B 등급일 때 교차검증 주체. **요청 부서와 같으면 안 된다** (7장) |
| `priority` | `required`(시나리오 A 필수) / `recommended` / `optional` |
| 입출력 | 스키마 파일 경로로 참조하고, 스키마는 `contracts/schemas/<부서>/`에 둔다 |
| skill 설명 | 다른 에이전트 LLM이 읽고 고를 수 있게 "무엇을 받아 무엇을 돌려주는지, 상태를 바꾸는지"를 쓴다. 모호한 설명은 이관의 원인이 된다 |

## 5. 메시지 공통 필드와 Task 상태

### A2A 요청·결과에 붙는 공통 필드

| 필드 | 필수 | 규칙 |
| --- | --- | --- |
| `context_id` | 필수 | 없으면 요청 거부. 발급 주체는 8장 |
| `tenant_id` | 필수 | `shop-demo` |
| `token` | 필수 | 에이전트 인증 토큰. `requester`는 토큰에서 꺼내고 본문 값은 믿지 않는다 |
| `path` | 필수 | 거쳐온 에이전트 목록. 자기 이름이 있으면 루프로 거부, 3홉 초과면 이관 큐로 |
| `deadline` | 필수 | 응답 마감. 넘으면 실행하지 않고 보고 |
| `idempotency_key` | 상태 변경 시 필수 | `context_id + 액션 + 대상` |
| `untrusted` | 외부 입력 있으면 | 고객 문의·상품 설명·판매자 소개·슬랙 문장 등. 지시로 해석하지 않는다 |
| `evidence` | 결과에 필수 | 쿼리, 조회 데이터 요약, 조회 시각, 참조 로그·카드 ID. 근거가 없으면 "확인할 수 없음" |
| `decision_reason`, `alternatives_considered` | 결정이 있을 때 | hr이 결정 카드를 만드는 재료. 채우면 기록 정확도가 올라간다 |

**루프 판정 규칙**: 루프 검사는 **새 요청에만** 적용한다. 동기 질의의 응답(예: marketing → support 질의의 답)은 같은 Task의 결과라 루프가 아니다.

### Task 상태 매핑

| 상태 | 쓰는 경우 | 주의 |
| --- | --- | --- |
| `submitted` | 접수 직후 |  |
| `working` | 처리 중, 대기 중(반품 입고 대기 등) |  |
| `input-required` | 다른 판단이 필요할 때. `reason_code`로 구분: `dept_input`(다른 부서), `customer_input`(고객 자료), `human_input`(사람 확정·승인) | 긴 대기는 Task를 쪼개 단계마다 마감을 둔다 |
| `completed` | 정상 처리. 결과가 거절이어도 정책상 거절이면 여기 (`outcome: auto_rejected`, `linked_duplicate` 등) |  |
| `rejected` | 에이전트가 요청 자체를 거부: 권한·형식·루프·토큰 위반 | 정책 거절과 섞으면 이관률·실패율 지표가 오염된다 |
| `canceled` | 마감 초과 |  |
| `failed` | 실행 오류 (외부 API 실패 등) |  |

C 등급 보류는 Task 상태가 아니라 안전 레이어의 실행 결과(`status: held`)로 표현하고, 요청한 Task는 `input-required`(`reason_code: human_input`)로 둔다.

## 6. 이벤트 공통 봉투와 카탈로그

마케팅 구성안 5장의 공통 봉투를 그대로 계약으로 채택한다. 원칙: 이벤트에는 사실만 담고(원인·판단 문장 금지), 외부 텍스트는 `untrusted`, 개인정보는 가명 ID만.

### 공통 봉투

`event_id`, `type`(`<대상>.<동사>`), `version`, `occurred_at`, `tenant_id`, `context_id`(사건이 아니면 `null`), `source`, `idempotency_key`, `data`, `untrusted`

- 받는 쪽은 같은 `event_id`를 두 번 처리하지 않는다. 발행 쪽은 outbox로 재전송한다 (최소 한 번 전달).
- 필드 추가는 `version` 유지, 삭제·이름 변경은 `version` 올림.
- 경로·마감·근거는 이벤트가 아니라 A2A Task에 둔다.

### 카탈로그 (시나리오 A·B 범위)

| type | 발행 | 주요 data | 구독 | 단계 |
| --- | --- | --- | --- | --- |
| `shop.degraded`, `shop.recovered` | shop-api | component, metric\_code, value, threshold | orchestrator, infra | 1차 |
| `payment.failed`, `payment.succeeded` | shop-api | payment\_id, order\_id, amount, failure\_code | finance, data | 1차 |
| `order.created`, `order.cancelled` | shop-api | order\_id, buyer\_id, items, total\_amount, seller\_ids (2차: items\[\].seller\_id·취소 금액) | finance, data, marketing | 1차 |
| `order.delivered`, `shipment.delayed`, `payment.refunded`, `return.received` | shop-api | 배송 완료 시각, 지연 분, 환불 금액, 반품 입고 시각 | support, finance | 2차 (불량·반품 흐름) |
| `seller.registered`, `product.created` | shop-api | 판매자·상품 ID, 카테고리, 가격 (소개·설명은 untrusted) | finance, marketing | 2차 |
| `support.inquiry_received` | support (문의 위젯) | inquiry\_id, buyer\_id, order\_id, channel\_code (원문은 untrusted) | orchestrator | 1차 |
| `support.inquiry_resolved` | support | inquiry\_id, order\_id, status(resolved·escalated), resolution(answered·claim\_decided·compensated·escalated), claim\_id, claim\_decision, coupon\_id, refund\_amount (원문·판단 문장 없음) | orchestrator | 1차 |
| `support.inquiry_surge` | support | topic\_code, count, window (근거는 Task 쪽, 샘플 문의는 untrusted) | orchestrator → marketing | 2차 |
| `metric.anomaly` | data | metric, window, observed, baseline, severity | orchestrator | 1차 |
| `infra.anomaly_detected` | infra | component, metric\_code, value, threshold, detected\_at, source\_ref (원인 판단은 넣지 않고 `diagnose_incident`로) | orchestrator | 1차 |
| `incident.opened`, `incident.resolved` | **orchestrator** | context\_id, component, 감지 신호 출처, 시각 | 전 부서 | 1차 |
| `notice.published` | marketing | notice\_id, channel\_code, context\_id (본문·사실·약속은 `get_notice`로) | support, memory | 1차 |
| `campaign.created` | marketing | campaign\_id, target\_codes, starts\_at, ends\_at, `promises[]` | support, finance | 2차 |
| `coupon.issued` | marketing | coupon\_id, campaign\_id(보상이면 null), `purpose_code`, count, total\_value | support, finance | 1차 |
| `safety.action_executed`, `safety.action_held`, `safety.review_decided` | safety | 실행 ID, 액션, 요청자, 등급, 계산 금액, 기준표 버전 | dashboard-api, memory | 1차 |
| `reconciliation.*`, `settlement.*` | finance | 대사·정산 결과 | memory, dashboard-api | 2차 |

`shop.degraded`·`shop.recovered`는 시연에서 장애 주입과 바로 이어지도록 shop-api가 직접 측정해 발행하고, Zabbix(DejaFix)는 보조 신호다. `metric_code`는 `p95_latency_ms`·`error_rate_5xx`, 기준값 초안은 **p95 1,000ms 초과 또는 5xx 2% 초과가 1분 지속**(시연용, 평소 p95 180ms·5xx 0.3% 기준). 기준값은 shop-api 설정으로 둔다 (예진 답변).

공지 본문·사실 필드·약속(`promises[{type, amount, condition_code, condition}]`)은 이벤트가 아니라 마케팅 `get_notice` 응답으로만 준다 (마케팅 v3·고객지원 합의). `promises.type`은 `points`·`coupon`, `condition_code`는 고객지원 정책표 코드(`LATE_3D`, `DEFECT_RETURN_OK` 등)이고, 목록에 없는 약속은 응대·보상에 쓰지 않는다. 문의는 양 급증은 data(`metric.anomaly`), 분류·정책 패턴은 support(`support.inquiry_surge`)로 나눈다. 단계: 1차 = 시나리오 A에 필요, 2차 = 불량·반품 흐름과 시나리오 B 이후.

## 7. 상태 변경과 안전 레이어

재무/정산 구성안 3장의 안전 레이어 설계를 공통 기준으로 채택한다. 모든 에이전트의 상태 변경(쓰기)은 `execute()` 하나로만 일어난다.

### 공통 규칙

- **쓰기 도구는 관문 뒤에만**: MCP 쓰기 도구(`shop_issue_coupon`, `github_create_pull` 등)는 LLM 도구 목록에서 빼고, `execute()`만 부를 수 있다.
- **등급은 규칙이 정한다**: 레지스트리 기본 등급과 금액 기준 중 높은 쪽. 요청에 적힌 등급(`claimed_grade`)은 기록만 하고 무시한다.
- **자기 검증 금지**: B 등급의 `verifier`는 요청 부서와 달라야 한다. 레지스트리가 등록을 거부한다.
- **요청 측 숫자 불신**: 금액은 안전 레이어가 다시 계산하고, 요청 값과 다르면 C(`amount_mismatch`).
- **누적 한도**: 건별 기준 + 일일·사건별 누적 기준을 함께 적용한다 (쪼개기 방지).
- **fail-closed**: 미등록 액션, 스키마 오류, 교차검증 거절·무응답, 기준표 조회 실패, 감사 로그 실패는 모두 실행하지 않는다.
- **멱등성**: 같은 `idempotency_key`의 두 번째 요청은 이전 결과를 돌려준다.
- **사후 검증**: B 액션 실행 뒤 요청 부서가 결과를 조회해 의도와 맞는지 확인한다.

### C 등급 보류 큐 (하나로 통일)

- 모든 부서의 C 건(PR 기준 초과, 기준 금액 이상 보상, 판매자 정산 확정 등)은 **safety의 보류 큐 하나**에 들어간다. 부서별 보류 목록(planning `/holds` 등)은 만들지 않는다.
- 대시보드가 이 큐를 보여 주고, 해당 부서 담당자가 승인·거절한다. 결정 API는 사람 토큰만 허용한다.
- **정해진 시간 안에 결정이 없으면 자동 거절한다** (결정안 2번). 재무 구성안 보류 큐에 이 규칙을 추가한다.

### 액션 등급 신고

각 부서는 개발 착수 전에 레지스트리 신고 양식(재무 구성안 3-6)으로 액션·기본 등급·검증자·금액 필드를 제출한다. Agent Card의 `grade`·`verifier`와 같은 값이어야 하며, 다르면 레지스트리가 우선한다. 기준 금액 등급표는 재무(태윤)가 관리하고 사람만 바꾼다.

- **DejaFix 조치 등록** (미등록이면 C 대신 등록한다): `rollback_deployment` B·검증자 planning, `restart_service` B·검증자 data(복구 지표 확인). 서버 재부팅·파일 삭제는 미등록으로 두어 C
- **금액이 아닌 기준 키** (planning `generate_pr` 검증용, 태윤님께 요청): `pr_changed_lines`, `pr_changed_files`, `pr_allowed_paths`. 기준표·레지스트리가 금액 외 기준도 담을 수 있어야 한다

## 8. 부서 간 책임 경계

구성안끼리 겹치거나 비던 지점을 "한 가지 일 = 한 주인"으로 정리한다. 롤백 주체를 포함해 모두 확정이다 (13장).

| 일 | 주인 | 나머지 부서 | 근거 |
| --- | --- | --- | --- |
| 사건 context\_id 발급, `incident.opened/resolved` | orchestrator | 감지 신호(`shop.degraded`, `metric.anomaly`, DejaFix 감지, `support.inquiry_received`)만 보냄 | 사건 하나 = context\_id 하나. 문의에서 파생된 보상은 문의 사건의 context\_id 승계 |
| 사건 아닌 일의 context\_id | 시작한 부서 (접두사 규칙) | — | 변경 요청은 planning, 캠페인은 marketing (캠페인 1개 = 1개) |
| 서버 지표 감시 | infra (DejaFix) | — | 응답 시간·에러율 |
| 업무 지표 감시 (주문·결제 실패율, 문의 총량) | data | — |  |
| 문의 접수 이벤트 | support (문의 위젯) `support.inquiry_received` | — | 위젯이 고객지원 소관 |
| 문의 분류·정책 패턴 감지 | support `support.inquiry_surge` | 공지 여부·문안은 marketing `draft_notice`가 결정 | 양은 data, 내용 패턴은 support |
| 영향 원천 숫자 (실패 건수·영향 고객·피해 금액) | data | finance는 이 결과로 손실 리포트 작성 | 같은 숫자를 두 곳에서 내지 않기 |
| 장애 관련 PR·배포 후보 | planning `find_related_prs` | data·infra가 질의 | 원인 판정은 하지 않음 |
| 원인 확정 | infra (DejaFix) | data는 업무 지표, planning은 배포·PR 근거를 제공 | 원인 확정과 롤백을 같은 곳에서 (예진 답변) |
| 롤백 실행 | infra `rollback_deployment` (B) | 검증자 planning. 검증은 "롤백 대상 배포가 장애 시각 직전이고, 그 사이 머지된 PR이 있는가"를 확인하는 읽기 skill (원인 판정은 하지 않음) | DejaFix 쓰기 도구 계약 PR 선행. 데모에서는 롤백이 판매 사이트 장애 주입 해제로 이어짐 (13장 후속 결정) |
| 쿠폰 지급 (캠페인·보상 모두) | marketing `issue_coupon` (B, 기준 이상 C), `purpose_code`로 구분 | 검증자 finance. 보상 쿠폰은 support 요청을 직렬 처리 + `sequence` | 판매 사이트 쿠폰 API는 마케팅 관문만 호출 |
| 보상 판정·요청 | support `compensation_request` (A, 요청만) | marketing이 지급 | 보상 대상·단가 판정은 support 한 곳 |
| 현금 환불 | support `refund_request` (B, 기준 이상 C) | 검증자 finance, 판매 사이트 환불 API는 고객지원 관문만 호출 | 마케팅은 관여하지 않음 |
| 공지 사실·약속 조회 | marketing `get_notice` (read) | support·finance가 조회 | 마케팅 DB 직접 조회 금지, 유일한 경로 |
| 금액 교차검증 | finance `verify_amount` | 안전 레이어가 라우팅. 요청 부서는 `execute()`만 부름 | 요청 부서가 재무를 직접 부르지 않음 |
| 기준 금액 등급표 | finance (사람만 변경) | 모든 부서는 조회. 캠페인·보상·환불 항목 분리 |  |
| 공지 게시 | marketing | support는 문구 점검(`notice_check`, 선택)·급증신호만 |  |
| 판매자 심사 판단 | finance | data는 사기 위험 점수만 |  |
| 결정 카드 문장·기각 판정 | hr | 다른 부서는 표시·활용만 |  |
| 카드 저장·검색 | memory | 각 부서는 자기 카드를 써서 넣음 | 결정·사건·사례·공지·정책·사기 카드 모두 |
| C 등급 보류 | safety 보류 큐 | 대시보드가 표시, 담당자가 결정, 10분 미처리 시 자동 거절 |  |

## 9. 짝꿍 루프 공통 패턴

세 루프(기획개발↔인사, 마케팅↔고객지원, 데이터분석↔기획개발)가 같은 4가지 연동과 같은 분업 원칙을 쓴다. 새 루프도 이 틀로 만든다.

**4가지 연동 유형**

| 유형 | 방향 | 방식 | 등급 |
| --- | --- | --- | --- |
| ① 이벤트·로그 | 결정하는 쪽 → 기록하는 쪽 | 비동기 이벤트 (결정 이유·검토한 대안 포함) | — |
| ② 질의 | 결정하는 쪽 → 판정하는 쪽 | 동기 질의, 같은 Task의 결과로 응답 | read |
| ③ 제안 | 판정하는 쪽 → 결정하는 쪽 | 초안을 상대 제안 큐에 등록, 채택은 상대가 결정 | A |
| ④ 조회 | 판정하는 쪽 → 결정하는 쪽 | 읽기 전용 조회 | read |

**분업 원칙**

1. 판정 문장은 한 곳에서만 만든다. 상대는 첨부·표시만 한다.
2. 판정하는 쪽은 읽기만 하고, 상대 영역의 변경은 초안까지만 제안한다.
3. 결정하는 쪽은 경고를 무시할 수 있지만, 무시한 사실은 감사 로그와 대시보드에 남는다.
4. 사람이 중간에서 옮겨 적지 않는다. 대신 안전장치(context\_id, 관문, 근거)는 전부 적용된다.

**세 루프 대응표**

| 루프 | 결정하는 쪽 | 판정·기록하는 쪽 | ② 질의 내용 | 판정 근거 카드 |
| --- | --- | --- | --- | --- |
| 개선 루프 | planning (기획서·티켓) | hr | 이 안이 과거에 기각됐나 (`check_plan`) | `decision` |
| 대외 소통 루프 | marketing (공지·상품) | support | 이 문구가 정책과 충돌하거나 분쟁을 불렀나 (`notice_check`) | `case`, `notice`, `policy` |
| 원인 추적 루프 | infra (원인 확정·롤백) | data (업무 지표), planning (배포·PR 근거) | 이상 구간 직전에 머지·배포된 PR이 있나 (planning `find_related_prs`) / 롤백 뒤 지표가 돌아왔나 (data `anomaly_detection`) | `incident` |

원인 추적 루프에서 ③(반복 이상 → 개선 요청 초안)은 그대로 planning이 받는다. 요청 등록·기획서 결정은 planning 일이다. data의 ②("롤백 뒤 지표가 돌아왔나")는 infra의 `verify_recovery`가 묻는다.

## 10. LLM·MCP 사용 공통 규칙

### LLM

| 항목 | \[공통\] 규칙 |
| --- | --- |
| 모델 | Gemini (DejaFix와 동일). 나중에 더 좋은 모델로 교체 가능 |
| 호출 | `agent-template`의 `LLMClient`(OpenAI 호환)로만. 부서가 별도 SDK(Anthropic 등)를 쓰지 않는다 |
| 설정 | `<부서>_LLM_MODEL`, `<부서>_LLM_BASE_URL`, `<부서>_LLM_API_KEY`를 먼저 읽고, 없으면 공통 `LLM_*`. 코드에 모델 이름을 쓰지 않는다 |
| 역할 | 숫자는 SQL·코드가 계산하고 LLM은 해석·문장만. LLM 문장 속 숫자가 계산 결과와 다르면 문장을 버린다 |
| 실패 대체 | LLM 실패·스키마 검증 실패 시 고정 템플릿(사실 필드만)으로 대체하고 `fallback` 표시 |
| 외부 입력 | `untrusted`는 프롬프트에 데이터 블록으로만 넣는다. 인젝션 테스트를 단독 테스트에 포함 |
| CI | 모든 테스트는 `MockLLMClient`. 실제 호출은 서버 PC 시나리오 테스트에서만 |
| 예산 | 주기 실행(감시 등)은 LLM을 부르지 않고 판정 후에만 문장 생성. context\_id별 호출 수 상한 |

### MCP

| 항목 | \[공통\] 규칙 |
| --- | --- |
| 봉투 | `{ok, collected_at, data, truncated}` (DejaFix common-mcp 원본) |
| 이름 | `<대상>_<동사>_<대상>`: `shop_list_orders`, `github_create_pull`, `memory_search_card` |
| 목 모드 | 모든 MCP 클라이언트는 목 모드를 갖고, `<부서>_MOCK=1`이면 시드 데이터로 단독 동작 |
| 쓰기 도구 | 관문 뒤에서만 호출 (7장) |
| A2A와의 구분 | 일을 맡기면 A2A, 도구·데이터를 쓰면 MCP. 다른 부서 데이터는 MCP·API·읽기 전용 뷰로만 읽고 DB에 직접 붙지 않는다 |

## 11. 구성안 공통 목차와 단독 테스트

6개 구성안의 목차가 제각각이라 비교가 어려웠다. 앞으로 고칠 때 아래 목차로 맞추고, 공통 항목은 이 문서를 참조만 한다.

**구성안 공통 목차**

1. 한눈에 보기 (이름·담당·한 줄 역할·하는 일/하지 않는 일)
2. skill과 등급 신고표 (4장 형식, `priority` 포함)
3. skill 상세 (입력·출력·계산 방식)
4. 이벤트 수신·발행 (6장 카탈로그에서 자기 것)
5. 통신 지도와 짝꿍 루프 (9장 패턴)
6. 시나리오별 역할 (12장 기준)
7. 안전장치 적용 (7장 중 자기 부서가 지킬 것)
8. 폴더 구성과 README 5항목
9. 개발 순서와 단독 테스트
10. 확인 필요

**README 5항목**: ① 역할 ② 실행 방법(목 모드 포함) ③ 입출력 계약 ④ 의존 ⑤ 하지 않는 것

**단독 테스트 공통 항목** (통합 에이전트 없이 테스트 클라이언트와 목 모드로)

| # | 항목 | 합격 기준 |
| --- | --- | --- |
| 1 | Agent Card | `/.well-known/agent-card.json`이 4장 형식을 통과 |
| 2 | 결정성 | 같은 입력을 두 번 보내면 같은 결과 |
| 3 | 루프 거부 | `path`에 자기 이름이 있는 새 요청은 `rejected` |
| 4 | 권한 | `allowed_requesters`에 없는 토큰은 `rejected` |
| 5 | 멱등성 | 같은 `idempotency_key` 두 번째 요청은 이전 결과 반환 |
| 6 | 인젝션 | `untrusted`에 지시문을 넣어도 상태 변경·문장에 영향 없음 |
| 7 | 계산 | LLM mock이 틀린 숫자를 내도 결과 숫자는 코드 계산값 |
| 8 | 상태 변경 (해당 부서) | B는 교차검증 후 실행, 기준 초과는 C 보류, 검증 무응답은 미실행 |

각 부서는 이 8개에 자기 skill 고유 테스트를 더한다.

## 12. 시나리오 A 필수 범위

6개 구성안의 skill을 합치면 50개가 넘어 7주 안에 다 만들 수 없다. 시나리오 A(결제 장애 → 공지·보상 → 롤백 → 복구 → 기록)를 끝까지 돌리는 데 필요한 것만 `priority: required`로 표시하고, 나머지는 `recommended`·`optional`로 내린다.

| 에이전트·서비스 | 시나리오 A 필수 | 그 외 (권장·선택) |
| --- | --- | --- |
| shop-api (재헌) | 주문·모의 결제, 장애 주입·해제 API, 1차 이벤트 발행, 공지 배너·쿠폰 API(마케팅 관문만), data용 조회(실패 주문·지표 시계열), 목 발행기 | 2차 이벤트(배송·반품·환불), 환불 API(고객지원 관문만), 판매자 화면 |
| orchestrator (준하) | 신호 수신 → 사건 개시·분배·종료, 이관 큐 | 실패 재시도, 요청 예산 |
| infra (예진) | `diagnose_incident`, `rollback_deployment`(B), `verify_recovery` | `restart_service` |
| data (재현) | `anomaly_detection`, `impact_analysis` | `fraud_score`, `kpi_summary` |
| planning (예진) | `find_related_prs`, `link_incident` | `create_ticket` 등 권장, 기획서→PR 파이프라인 선택 |
| marketing (재헌) | `draft_notice`, `publish_notice`, `get_notice`, `issue_coupon`(compensation, 직렬) | `draft_campaign`, `campaign_report`, `notice_check` 연동 |
| support (병환) | `inquiry_draft`, `reply_send`, `compensation_request`, 문의 위젯 이벤트 | `claim_evaluate`·보상 큐(불량·반품 흐름), `refund_request`, `support.inquiry_surge`, `notice_check` |
| finance (태윤) | `verify_amount`, 손실 리포트(data 결과 기반) | 일일 대사, 판매자 정산 |
| hr (준하) | `record_case` → memory에 사건 카드 저장 | 인수인계, 발령·교대, 온보딩, 계획 비교 |
| safety (태윤) | `execute()`: A 즉시 실행·C 보류 최소판 → B 교차검증, 보류 큐 | 누적 한도 세부 키 |
| memory (재현) | 카드 쓰기·검색 | 카드 대체·폐기 연결 |
| dashboard (병환) | 사건 타임라인, C 보류 큐 | KPI 화면, 부서별 탭, 연동 현황 |

시나리오 A가 연속 3회 무인으로 돌아간 다음에 시나리오 B(판매자 입점), 학습 효과(2차 장애에서 기록 참조), 각 부서의 권장 skill 순으로 올린다.

## 13. 결정 사항과 구성안별 수정 목록

### 결정 사항 (2026-10-21 확정)

| # | 안건 | 결정 | 관련 |
| --- | --- | --- | --- |
| 1 | 롤백 주체 | infra `rollback_deployment`(B), 검증자 planning. DejaFix 쓰기 도구 계약 PR은 예진이 바로 올린다. planning의 검증은 "롤백 대상 배포가 장애 시각 직전이고 그 사이 머지된 PR이 있는가"를 확인하는 읽기 skill이며 원인 판정은 하지 않는다. 원인 확정도 infra | 예진, 요구사항 DEP-DEV-02 → DJF-04로 이동 |
| 2 | 기획개발 범위 | 시나리오 A 필수는 `find_related_prs`·`link_incident`. `create_ticket`·`list_in_progress`·`send_decision_event`는 권장, 기획서→PR 파이프라인(`write_spec`·`generate_pr`·`review_pr` 등)은 선택 (결정안 6번 유지). 개발 순서로만 그렇고, 10/27 기획발표에서는 요청 → 기획서 → PR 흐름을 기획개발의 핵심으로 보여 준다. 파이프라인은 시나리오 A가 3회 무인으로 돈 다음 첫 순서 | 예진 |
| 3 | planning `generate_pr`의 검증자 | 안전 레이어 코드 규칙(변경 크기·허용 경로)이 검증한다. 이를 위해 기준표·레지스트리에 금액이 아닌 키(`pr_changed_lines`·`pr_changed_files`·`pr_allowed_paths`)를 추가한다. planning 안 검수 Agent는 `review_pr`(A, PR 코멘트만)로 남고 통과 판정에 쓰지 않는다 | 예진, 태윤 |
| 4 | 필드 표기 | 우리가 정의하는 필드는 전부 snake\_case. A2A SDK 프로토콜 필드만 SDK 그대로 | 준하 계약 |
| 5 | 기준 금액 | 재무 기준표가 원본: 쿠폰 건별 30만·단가 2만·일일 100만에, 결정안 값을 키로 추가(`compensation_per_context` 100만, `compensation_per_customer` 5만). 모두 시작값이며 테스트하며 조정 | 태윤 |
| 6 | C 보류 자동 거절 시간 | 데모 10분, 운영값은 기준표 설정으로 분리 | 태윤, 병환 |
| 7 | PR 리뷰 규칙 | (개발 레포) 코드 폴더 PR은 다른 부서 에이전트 리뷰(`agent-review`) + CI 통과 시 dev 자동 머지, 자동화·규칙·문서는 사람 승인 (`docs/automation-spec.md`). (제품 기능) planning `generate_pr`가 만든 PR은 자동 머지 대상에서 뺀다 — "머지는 사람이 누른다"가 기획개발 설계의 핵심이고 변경 대상이 다른 부서 폴더일 수 있기 때문 | 전원, 예진 조건 |
| 8 | 판매 사이트 요청 우선순위 | 1차(시나리오 A): 주문·모의 결제, 장애 주입·해제, `shop.degraded`·`payment.failed`, 공지 배너·쿠폰 API, 실패 주문·지표 시계열 조회, 문의 위젯 자리(문의 이벤트는 고객지원 `support.inquiry_received`). 2차(불량·반품 흐름, 시나리오 B 이후): 배송·반품·환불 이벤트와 환불 API, `items[].seller_id`·취소 금액, 결제 조회 API, 판매자 정보·가격 조회 | 재헌 |

### 후속 결정 (마케팅 v3·고객지원 구성안 반영, 2026-10-21)

| # | 안건 | 결정 | 관련 |
| --- | --- | --- | --- |
| 9 | 보상 지급 경로 | 보상 판정은 support, 지급은 marketing `issue_coupon`(purpose\_code=compensation), support 요청은 직렬 + `sequence`. 현금 환불은 support `refund_request`(B)가 고객지원 관문으로 | 재헌·병환 합의 채택 |
| 10 | 공지 정보 전달 | `notice.published`는 사실 ID만, 본문·사실·약속은 `get_notice`로만. 약속은 `promises[{type, amount, condition_code, condition}]` | 재헌·병환 합의 채택 |
| 11 | 문의 이벤트 | 문의 접수는 support `support.inquiry_received`, 패턴 신호는 `support.inquiry_surge`(분류·정책), 총량 이상은 data `metric.anomaly` | 재헌·병환 합의 + data 경계 |
| 12 | 교차검증 결과 상태 | 재무 거절 = completed + `outcome: rejected_by_review`, 마감 초과 = canceled, 실행 오류 = failed | 재헌·병환·준하 |
| 13 | 교차검증 호출 | 마케팅·고객지원은 `execute()`만 부르고, 안전 레이어가 finance `verify_amount`로 라우팅한다. 마케팅 v3의 "마케팅 → 재무 직접 요청" 표현은 이 방식으로 읽는다 | 재헌·태윤 |
| 14 | 이벤트 전달 방식 | 이벤트 버스(Redis Streams) 하나로 통일. shop-api는 agent-template `events.publish`로 발행. 마케팅 v3의 `POST /events` 안은 쓰지 않음 | 재헌·준하 |
| 15 | 데모 장애 복구 | infra `rollback_deployment`가 실행되면 shop-api 장애 주입 해제 API를 관문 경유로 호출해 `shop.recovered`가 나오게 한다. 사람이 해제 버튼을 누르지 않아도 시나리오 A가 무인으로 끝난다 | 예진·재헌 |
| 16 | 이벤트 단계 | 마케팅 v3의 \[필수\] 12종 중 배송·반품·환불 4종은 2차(고객지원 불량·반품 흐름)로 개발 순서만 뒤로. 스키마 정의는 1차에 함께 | 재헌·병환 |

### 후속 결정 (기획개발·인프라 답변 반영, 2026-10-21)

| # | 안건 | 결정 | 관련 |
| --- | --- | --- | --- |
| 17 | `shop.degraded` 신호 원천 | shop-api가 직접 측정·발행, Zabbix는 보조. `p95_latency_ms`·`error_rate_5xx`, 기준 초안 p95 1,000ms 또는 5xx 2%가 1분 지속 (6장) | 예진 → 재헌 |
| 18 | DejaFix 조치 레지스트리 | `rollback_deployment` B·검증자 planning, `restart_service` B·검증자 data, 서버 재부팅·파일 삭제는 미등록(C) (7장) | 예진 → 태윤 |
| 19 | `record_case` kind | `spec_confirmed`·`pr_merged`·`rollback_executed`에 `decision_extracted`·`request_closed`를 더한다. `rollback_executed`는 infra가, 나머지는 planning이 보낸다 | 예진 → 준하 |
| 20 | planning 요청·조회 skill 이름 | 요청 등록 `create_ticket`, 진행 중 조회 `list_in_progress` 하나로 유지 (둘 다 시나리오 A 범위 밖) | 예진 → 병환·준하 |
| 21 | 원인 추적 루프 | 결정하는 쪽은 infra(원인 확정·롤백), 판정·기록은 data(업무 지표)와 planning(배포·PR 근거) (8·9장) | 예진 |

### 구성안별 수정 목록

| 구성안 | 고칠 것 |
| --- | --- |
| 공통 (계약 프롬프트, CLAUDE.md) | (반영 완료) snake\_case, 재헌 공통 봉투, 6장 카탈로그(후속 결정 포함), record\_case kind 확장, 18항목 규약 통합 — prompt-junha-contracts.md, CLAUDE.md |
| 기획개발 (예진) | (답변으로 전부 수용, 구성안에 반영 예정) LLM을 Gemini로(`PLANNING_LLM_*`), tenant\_id 확정, 포트 8101·8107, 우선순위 표시(`find_related_prs`·`link_incident` required, 파이프라인 recommended), `/holds` 제거(C 보류는 safety 큐, Task는 `input-required`·`human_input`), 검수 Agent는 `review_pr`(A). 변경 요청 context\_id는 planning이 `ctx_req_`로 발급, `github_create_pull` 같은 쓰기 도구는 `execute()` 뒤에만 |
| 고객지원 (병환) | (v3 대조 반영 후 남은 것) LLM을 Gemini로(문서상 아직 Anthropic), 포트 8102, 사례 카드·공지 색인은 memory 사용, 상담원 승인·C 보류는 safety 보류 큐 + 10분 자동 거절(확인 필요 1·2·11 정리), 레포 배치 확정(확인 필요 10), 필수 skill 표시(12장) |
| 마케팅·판매 사이트 (재헌) | (v3 반영 후 남은 것) 교차검증은 execute()만 부르고 안전 레이어가 라우팅(후속 13), 이벤트 전달은 Redis Streams(후속 14), 롤백 시 장애 주입 해제 API 제공(후속 15), 배송·반품·환불 이벤트는 2차(후속 16), 결정 대기 4번(C 해제 주체)은 결정안 2번으로 닫기, 레포·tenant\_id 확정 반영 |
| 인사 (준하) | 계약 원본은 `contracts/` 한 곳(복사본 없음), 결정 카드는 memory에 저장, 확신 낮은 카드 승인은 대시보드 큐로, 필수는 `record_case`, 부서 목록에서 마켓 운영 제거 |
| 재무/정산·안전 레이어 (태윤) | 보류 큐에 자동 거절 추가, `incident_impact_report`는 data 영향 분석 결과 기반으로, memory 카드 쓰기(A) 레지스트리 등록 |
| 데이터분석 (재현) | (반영 완료) find\_related\_prs, 롤백 주체 infra, 원인 확정도 infra, 루프 ②는 infra `verify_recovery`가 질의, snake\_case, memory MCP 도구 이름, 영향 원천 숫자 경계, 구독 이벤트 정리 |
