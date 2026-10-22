# Agent Card 초안 작성 가이드

각자 자기 부서 에이전트의 `agent-card.json` 초안을 만든다. 준하님의 contracts v0.1.0이 나오기 전에는 이 문서의 형식으로 초안만 잡아두고, 계약이 확정되면 필드 이름만 맞춰서 `agents/<부서>/agent-card.json`에 커밋한다.

## Agent Card가 하는 일

다른 에이전트와 통합 에이전트가 "이 에이전트는 무슨 일을 할 수 있고, 누가 시킬 수 있고, 얼마나 위험한 일인지"를 읽는 명함이다. 통합 에이전트는 이걸 보고 일을 분배하고, 실행 관문은 이걸 보고 권한(ACL)을 확인한다.

## 작성 순서

1. **skill 목록 뽑기**: 요구사항정의서 6장(DEP)에서 자기 부서 행을 가져온다. skill 하나 = 다른 에이전트가 요청할 수 있는 일 하나.
2. **id 정하기**: 영어 snake_case 동사구 (`impact_analysis`, `coupon_issue`).
3. **description 쓰기**: 다른 에이전트의 LLM이 읽고 "이걸 불러야겠다"고 판단할 수 있게 구체적으로. 입력으로 뭘 받고 무엇을 돌려주는지 한 문장씩.
4. **grade 정하기**: 아래 기준표로 판단.
5. **allowedRequesters 정하기**: 이 skill을 요청할 수 있는 에이전트. 처음에는 좁게 잡고, 필요해지면 넓힌다.
6. **입력·출력 예시 적기**: 초안 단계에서는 README에 JSON 예시로. 계약 확정 후 pydantic 모델로 옮긴다.

## grade 기준

| grade | 의미 | 예시 |
| --- | --- | --- |
| `read` | 조회만, 상태 변경 없음 | 영향 분석, 손실 집계, 배포 이력 조회 |
| `A` | 바로 실행해도 되는 상태 변경 | 공지 게시, 티켓 생성, 사건 기록 |
| `B` | 다른 에이전트 교차검증 후 실행 | 롤백, 쿠폰 지급, 정산 보류 |
| `C` | 사람 승인 대기열로 | 판매자 영구 정지, 기준 금액 이상 보상 |

`read`가 아닌 skill은 실행할 때 반드시 `execute()`를 거친다. 실제 등급은 안전 레이어가 금액·누적 한도로 올릴 수 있으니, 여기엔 **기본 등급**을 적는다.

## 형식

```json
{
  "name": "data-agent",
  "description": "데이터분석 — 지표 이상 탐지, 장애 영향 분석, 판매자 사기 위험 점수",
  "url": "http://data-agent:8106/a2a",
  "version": "0.1.0",
  "capabilities": { "streaming": true, "pushNotifications": false },
  "skills": [
    {
      "id": "impact_analysis",
      "name": "영향 분석",
      "description": "장애 구간(시작·종료 시각)을 받아 실패 주문 수, 영향 고객 목록, 피해 금액을 계산해 돌려준다. 숫자는 shop-api 조회 결과로 계산한다.",
      "tags": ["incident", "analysis"],
      "grade": "read",
      "allowedRequesters": ["orchestrator", "infra-dejafix", "support", "finance", "planning"]
    }
  ]
}
```

포트는 `CLAUDE.md`의 포트 표를 따른다.

## 부서별 skill 초안 (요구사항정의서 DEP 기준)

allowedRequesters는 시작 제안값이다. 각자 판단해서 조정한다.

| 폴더 | skill id | 이름 | grade | allowedRequesters (제안) |
| --- | --- | --- | --- | --- |
| planning | `deploy_trace` | 배포 추적 | read | orchestrator, infra-dejafix, data |
| planning | `rollback` | 롤백 | B | orchestrator |
| planning | `ticket_create` | 티켓 생성 | A | orchestrator, hr |
| infra-dejafix | `detect_diagnose` | 감지·진단 | read | orchestrator |
| infra-dejafix | `recovery_verify` | 복구 검증 | read | orchestrator, planning |
| support | `inquiry_response` | 문의 응대 | A | orchestrator |
| support | `compensation_calc` | 보상 계산 | B | orchestrator |
| support | `seller_notice` | 판매자 안내 | A | orchestrator, finance |
| marketing | `notice_post` | 공지 게시 | A | orchestrator |
| marketing | `product_pause` | 상품 판매 일시중지 | B | orchestrator |
| hr | `incident_record` | 사건 기록 | A | orchestrator |
| hr | `handover_doc` | 인수인계 문서 | A | orchestrator |
| finance | `loss_aggregation` | 손실 집계 | read | orchestrator |
| finance | `compensation_approval` | 보상 승인 | read | orchestrator, support |
| finance | `seller_review` | 판매자 심사 | B | orchestrator |
| finance | `settlement_hold` | 정산 보류 | B | orchestrator |
| data | `anomaly_detection` | 이상 탐지 | read | orchestrator, infra-dejafix |
| data | `impact_analysis` | 영향 분석 | read | orchestrator, infra-dejafix, support, finance, planning |
| data | `fraud_score` | 사기 위험 점수 | read | orchestrator, finance |

## 제출

- 초안은 `agents/<부서>/agent-card.json` + `README.md`(skill별 입력·출력 JSON 예시)로 dev에 PR
- 자기 폴더만 바뀌는 PR이라 CI 통과 시 자동 머지된다
- 다른 부서 skill을 호출할 계획이면, 그 부서의 allowedRequesters에 내가 들어가 있는지 확인하고 없으면 그 담당자에게 요청한다
