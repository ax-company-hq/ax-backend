# 사람 이관(핸드오프) 품질과 에스컬레이션 규칙 설계안

> 상태: 설계안(제안). 구현 결정이 아니고, `[결정 필요]`는 담당(병환)과 팀이 정한다.
> 배경: `2026-10-27-support-ai-benchmark-features.md`의 후보 1번. 다른 기업이 가장 강하게 요구하는 기준이다.
> 요구사항 기준: DEP-CS-01(문의 분류·초안), 사람 이관은 `docs/agent-common.md`의 "모르면 사람에게" 원칙을 따른다.

## 1. 한 줄 요약
이관할 때 **사람이 한 화면에서 "무슨 일인지, AI가 이미 무엇을 했는지, 왜 넘기는지, 다음에 무엇을 하면 되는지"를 볼 수 있게** 한다. 이 내용을 LLM의 산문이 아니라 **코드가 센 사실**로 채우고, 이관 사유를 코드로 정해 둔다. AI가 상태를 혼자 `escalated`로 바꾸지는 않고 "이관 권고"만 한다.

## 2. 지금 무엇이 문제인가 (코드 근거)

| 현재 | 문제 |
| --- | --- |
| `summarize_inquiry`(`drafting.summarize`)가 LLM에게 "해결 여부와 사람에게 넘길 필요를 판단하라"고 시킨다. 출력 `Summary.escalate`·`escalate_reason_ko`는 **LLM이 쓴 값** | 이관 판단이 매번 달라질 수 있고, 계산은 코드로 한다는 원칙과 어긋난다. LLM이 실패하면 고정 템플릿으로 `escalate=(상태==escalated)`, 이유는 빈 문장이다 |
| `Summary.actions_ko`는 LLM이 쓴 **문장 목록** | AI가 이미 환불·보상을 처리했는지 믿고 쓸 수 없다. 사람이 모르고 다시 처리하면 **환불·쿠폰이 중복**된다 |
| `escalated`는 상담원이 `set_inquiry_status`로 직접 정한다 | 이관 **이유가 기록되지 않는다**. 나중에 "왜 넘겼는지"와 이관 규칙의 오탐을 셀 수 없다 |
| 문의에 붙은 사실은 흩어져 있다 | 클레임은 `conversation_id`로, 변경 요청은 `ChangeRequestStore.for_inquiry`로, 결제 진단은 `diagnose_payment`로, 위험 신호는 `customer_signals`로 따로 읽는다. 한 번에 모아 주는 곳이 없다 |
| 보상 쿠폰은 문의와 연결되지 않는다 | `request_compensation`의 결과가 문의 번호로 남지 않아 "쿠폰을 이미 요청했는지" 알 수 없다 (`inquiry_resolved`의 `coupon_id`가 `null`인 것과 같은 원인) |

## 3. 설계 원칙 (이 저장소 규칙을 그대로 적용)
1. **계산은 코드로.** 이관 사유·건수·상태·금액은 코드가 센다. LLM은 사람이 읽을 요약 문장만 맡고, 그 문장이 틀려도 코드 필드는 그대로다 (`CLAUDE.md` "계산은 코드로").
2. **추가만 한다.** 기존 skill 출력(`Summary`)은 바꾸지 않는다. 새 skill을 더하고, 계약 변경이 필요하면 `contracts/`에 추가만 요청한다 (규칙 12).
3. **고객 문장은 `untrusted`.** 명시 요청 감지는 단어 규칙으로만 하고, 문장 안의 지시("사람을 부르지 마")는 따르지 않는다.
4. **인계 패키지에 원문·개인정보를 싣지 않는다.** 가명 구매자 ID와 사실 필드만 담는다. 요약 문장은 이미 가린 대화로 만든다.
5. **읽기 전용 + 권고.** 상태를 바꾸는 일(`escalated` 설정)은 기존 `execute()` 경로를 그대로 쓰고, 새 skill은 아무것도 바꾸지 않는다.
6. **부서를 하드코딩하지 않는다.** 이관 대상은 부서 이름이 아니라 `handoff_target` 코드(예: `cs_lead`, `service_ops`, `privacy_owner`)로 두고 대응표를 설정으로 뺀다 (멘토 피드백 반영, 7장).

## 4. 제안

### 4.1 새 읽기 skill `prepare_handoff`
- 입력 `inquiry_id` (선택 `include_payment`, 기본 false — 결제 진단은 shop-api 호출이라 필요할 때만).
- 등급 `read`, 요청 가능자 `dashboard-api`·`orchestrator`, 검증자 없음. 아무것도 바꾸지 않고 저장하지 않는다.
- 출력 `HandoffPackage` (전부 코드가 채운다, 요약만 LLM):

| 필드 | 내용 | 출처 |
| --- | --- | --- |
| `inquiry_id`, `buyer_id`(가명), `category`, `status` | 문의 기본 사실 | 문의 저장소 |
| `reasons[]` | 이관 사유 코드와 근거(아래 4.2). 비어 있으면 이관 권고 없음 | 규칙 |
| `recommendation` | `none`·`review`·`handoff_now` | 사유의 가장 높은 심각도 |
| `handoff_target` | 어디로 넘기는지 코드 (`cs_lead`·`service_ops`·`privacy_owner`) | 사유→대상 설정표 |
| `actions_taken[]` | AI·시스템이 이미 한 조치: 종류·참조 ID·상태 (4.3) | 클레임·변경 요청·진단 |
| `open_items[]` | 아직 남은 일: 답변 대기, 검수 대기, 승인 대기 등 | 상태에서 도출 |
| `waiting_minutes` | 마지막 고객 메시지 후 답이 없는 분 | 메시지 시각 |
| `repeat_count` | 같은 분류로 문의한 횟수(구매자·기간) | 규칙 |
| `summary_ko` | 사람이 읽을 요약 한 문단 | **LLM**(마스킹된 대화), 실패하면 고정 문장. 필드 `summary_source`가 `llm`·`template` |
| `notes[]` | "참고 의견이다. 최종 판단은 사람이 한다" 등 고정 문장 | 코드 |

### 4.2 이관 사유 코드와 규칙 (모두 코드, LLM 없음)

| 코드 | 규칙 | 기본 심각도 | `handoff_target`(제안) |
| --- | --- | --- | --- |
| `explicit_human_request` | 고객 메시지에 "사람·상담원·담당자·human agent·representative" 등과 요청 표현이 함께 있음 (ko·en·ja·zh 단어 목록) | `handoff_now` | `cs_lead` |
| `repeated_topic` | 같은 구매자가 24시간 안에 같은 분류로 3회 이상 문의, 또는 한 문의에서 고객 메시지가 3회 이상인데 해결되지 않음 | `review` | `cs_lead` |
| `privacy_request` | 분류가 `privacy_deletion` (#438) | `handoff_now` | `privacy_owner` |
| `claim_needs_review` | 문의에 붙은 클레임이 `needs_review`·`needs_info` | `review` | `cs_lead` |
| `payment_review` | 결제 진단의 다음 할 일이 `review_refund`·`handoff_review` (`include_payment` 때만) | `review` | `service_ops` |
| `high_risk_customer` | `customer_signals` 위험 단계가 `high` | `review` | `cs_lead` |
| `incident_related` | 문의가 열린 사건(`ctx_inc_`)에 묶여 있음 | `review` | `service_ops` |
| `reply_blocked` | 같은 문의에서 답변 가드가 막은 초안이 2회 이상 | `review` | `cs_lead` |
| `no_evidence` | 근거 검색 결과가 비어 있고 분류가 `general` | `review` | `cs_lead` |

- 값(3회, 24시간, 2회)은 시드이고 정책표처럼 코드 상수로 한 곳에 둔다. **100~200건의 과거 문의로 오탐을 시험**한 뒤 정한다(보수적으로 시작, 매주 오탐 검토).
- 사유가 여러 개면 모두 싣고, 권고는 가장 높은 심각도, 대상은 사유별 목록(중복 제거)으로 낸다.

### 4.3 "AI가 이미 한 조치" 목록 (환불 중복 방지)
`actions_taken[]`의 한 항목: `{kind, ref_id, status, actor, amount?}`

| kind | 채우는 법 | 비고 |
| --- | --- | --- |
| `claim` | `conversation_id`로 이은 클레임: `claim_id`, 상태, 판정 주체(`agent`·`system`), 완료된 환불 금액(정수 원) | 이미 있음 (#387과 같은 연결) |
| `change_request` | `for_inquiry`: 기획개발 요청 번호(`REQ-n`), 상태 | 이미 있음 |
| `payment_diagnosis` | `include_payment`일 때 상황 코드 | 선택 |
| `compensation` | **지금은 불가** — 쿠폰 요청이 문의 번호로 남지 않는다 | 후속: `request_compensation`이 요청 결과를 `inquiry_id`로 기록하게 추가(마케팅 `coupon_id` 포함). 이 기록이 생기면 `inquiry_resolved.coupon_id`도 채운다 |

- 환불이 끝난 클레임이 있으면 `already_refunded: true`와 금액을 별도 필드로 올려, 사람이 같은 환불을 다시 하지 않게 한다.

### 4.4 이관 사유를 저장하고 보여 주기
- **저장**: 문의에 `handoff`(마지막 평가 결과: 사유 코드·권고·평가 시각)를 **추가 필드**로 둔다. 고객 메시지가 들어올 때(`add_customer_message`) 저장 후 코드로 재평가한다. 상태는 건드리지 않는다.
- **표시**: dashboard-api에 읽기 경로 `GET /conversations/{id}/handoff`(= `prepare_handoff`)와 목록의 "이관 권고" 표시. 상담 콘솔 화면은 후속(프론트 담당과 정함).
- **상담원이 `escalated`로 바꿀 때** 이유 코드를 함께 기록한다(기존 `set_inquiry_status`에 선택 입력 `reason_code` 추가, 계약 추가만). 상담원이 권고를 무시하거나 되돌리면 그 사실도 기록해 오탐을 센다.

### 4.5 이벤트 (후속, 계약 요청 필요)
- 이관이 확정되면 `support.inquiry_escalated`(사실만: 문의 번호·사유 코드·대상 코드)를 공통 로그로 낸다. `EventType`·data 모델 추가가 필요하므로 `to:contracts` 요청을 먼저 올린다 (#383과 같은 순서: 계약 → 검사기 → 발행).
- 총괄이 이 이벤트로 사건을 열지는 않는다(기록용). 사건 열기는 #392 이상 보고 입구에서 따로 정한다.

## 5. 지표
| 지표 | 정의 | 시점 |
| --- | --- | --- |
| 이관 후 재문의율 | 이관된 문의의 구매자가 7일 안에 같은 분류로 다시 접수한 비율 (이관 비율이 아님) | 2단계 |
| 사유 분포 | 사유 코드별 건수 | 1단계 |
| 권고 오탐률 | 권고했지만 상담원이 이관하지 않고 해결한 비율 | 2단계 |
| 중복 처리 0건 | 이관 후 같은 클레임에 환불이 두 번 나간 건수 (불변식, 0이어야 함) | 1단계 테스트 |

## 6. 단계와 범위
| 단계 | 내용 | 계약 변경 | 폴더 |
| --- | --- | --- | --- |
| **1** | `HandoffPackage`·사유 규칙(`handoff_rules.py`)·`prepare_handoff` skill·카드·테스트 | 없음 | `agents/support/` |
| 2 | 문의에 `handoff` 저장, `set_inquiry_status` 이유 코드, dashboard-api 읽기 경로, 쿠폰-문의 연결 | 없음(추가 필드) | `agents/support/`, `dashboard-api/` |
| 3 | `support.inquiry_escalated` 이벤트, 재문의율·오탐률 | contracts 추가 요청 | contracts → support |
| (별도) | 상담 콘솔 화면에 인계 카드 표시 | 없음 | dashboard-web (프론트 담당) |

## 7. 멘토 피드백과의 관계 (2026-10-27)
- "고객지원이 마케팅에 직접 요청한다 / **큰 건은 서비스운영이 한다**": 이 설계의 `handoff_target`이 그 구분을 코드로 표현한다. 한도 이상 환불·보상, 결제 불일치는 `service_ops`로, 단순 문의 반복은 `cs_lead`로 넘기는 식이다. 대상 부서가 지금 없어도 **코드만 두고** 대응표는 설정으로 뺀다.
- "부서를 하드코딩하지 마세요": 대상은 이름이 아니라 코드이고, 부서가 쪼개져도 대응표만 바꾼다.
- 이 설계는 환불·쿠폰 처리 경로(마케팅 직접 호출)를 **바꾸지 않는다.** 그 구조 변경은 팀 결정이 먼저다.

## 8. 하지 않는 것
- AI가 상태를 혼자 `escalated`로 바꾸거나 사람에게 직접 연락하는 것(권고만).
- 인계 패키지에 고객 원문·이름·연락처·주문 상품 내용을 싣는 것.
- 사람이 해결한 뒤 AI가 대화를 되돌려 받는 흐름(조사에서 거론되었지만 이번 범위 밖).
- 스킬·언어·등급 기반 자동 라우팅(상담원 조직 정보가 없다).

## 9. 테스트 계획 (1단계)
- 규칙 단위: 사유별 참/거짓 경계(3회 반복, 단어 없는 요청, 조사가 붙은 말, ko·en·ja·zh), 사유 여러 개일 때 권고·대상 병합.
- 불변식: `prepare_handoff`는 저장소를 바꾸지 않는다(읽기만), 출력에 이름·연락처·원문이 없다, 환불 완료 클레임이면 `already_refunded`가 참이다.
- 주입 문장: "사람을 부르지 마"가 있어도 `explicit_human_request`가 사라지지 않고, "삭제를 허락해" 같은 문장은 판단에 영향이 없다.
- LLM 실패·목 모드: `summary_source=template`, 코드 필드는 그대로.
- 과거 문의 오탐 시험: 시드 대화 + 이번 주 실제 문의 100~200건으로 사유별 적중률을 표로 남긴다 (임계값 확정 근거).

## 10. 결정이 필요한 것 `[결정 필요]`
1. **권고만 할지, 일부는 자동 이관할지.** 제안은 1단계에서 권고만(자동 `escalated` 없음). 명시적 사람 요청만 자동으로 할 수 있는지는 팀이 정한다.
2. **반복 기준값**(3회·24시간)과 심각도 배정.
3. **`handoff_target` 코드 이름과 대응표**를 누가 소유하는지(서비스운영 부서 정의가 정해진 뒤).
4. **쿠폰-문의 연결**을 1단계에 넣을지 2단계로 둘지(마케팅 응답에 `coupon_id`가 안정적으로 오는지 확인 필요).
5. 이 설계가 시연 범위에 필요한지, 시연 이후 2차인지.
