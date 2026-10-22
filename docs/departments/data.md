# 데이터분석 에이전트(data) 구성안

Oct 21, 2026 · @재현

## 1. 한눈에 보기

데이터분석 에이전트(`data`)는 판매 사이트의 업무 지표를 감시해 이상을 먼저 찾고, 사건이 생기면 피해 규모를 숫자로 계산해 다른 부서의 판단 근거를 만든다. 공통 모듈인 회사 기억(`memory`)은 모든 부서의 카드를 한 곳에 저장·검색하는 저장소로 만든다.

| 항목 | 내용 |
| --- | --- |
| 에이전트 이름 | `data` (포트 8106) |
| 공통 모듈 | `memory` 회사 기억 (포트 8002) |
| 담당 | 재현 |
| 한 줄 역할 | 업무 지표 이상 탐지, 장애 영향 분석, 신규 판매자 사기 위험 점수, KPI 계산. 숫자는 코드가 내고 LLM은 설명 문장만 쓴다 |
| 조직도 위치 | 데이터분석 — 이상 탐지, 영향 분석. 기획개발과 짝꿍(원인 추적 루프) |
| 첫 고객사 | 가상 오픈마켓 (도서·문구 판매자 시드) |

**맡는 일과 하지 않는 일**

| 맡는 일 | 하지 않는 일 (README "하지 않는 것") |
| --- | --- |
| 업무 지표(주문 실패율, 결제 실패율, 문의량 등) 이상 탐지 | 인프라 지표 감시 (서버 응답 시간·에러율은 DejaFix) |
| 장애 구간의 실패 주문·영향 고객·피해 금액 계산 | 보상액 결정·지급 (고객지원), 손실 확정·정산 (재무/정산) |
| 신규 판매자 사기 위험 점수와 근거 | 입점 승인·거절 판단 (재무/정산) |
| KPI 계산 (MTTR, 자동 처리율, 1차/2차 사건 비교) | KPI 화면 (병환의 대시보드) |
| 회사 기억: 카드 저장·검색 API | 카드 문장 작성 (각 부서가 자기 카드를 씀) |
| 패턴 감지 임계값 조율 (고객지원 요청 시) | 판매 사이트 DB 직접 조회, 다른 부서 데이터 수정 |

## 2. skill과 등급 신고표

데이터분석의 skill은 모두 조회(read)라 상태를 바꾸지 않는다. 대신 모든 결과에 근거(쿼리, 데이터 요약, 참조 로그 ID)를 붙인다. memory의 카드 쓰기만 기록성 A 등급이다.

**data 에이전트**

| skill id | 이름 | grade | 우선순위 | allowedRequesters |
| --- | --- | --- | --- | --- |
| `anomaly_detection` | 업무 지표 이상 탐지 | read | 필수 | orchestrator, infra-dejafix |
| `impact_analysis` | 장애 영향 분석 | read | 필수 | orchestrator, infra-dejafix, support, finance, planning |
| `fraud_score` | 판매자 사기 위험 점수 | read | 권장 | orchestrator, finance |
| `kpi_summary` | KPI 계산 | read | 권장 | orchestrator, dashboard-api |
| `threshold_advise` | 패턴 감지 임계값 제안 | read | 선택 | support, marketing |

**memory (회사 기억 API)**

| 기능 | 이름 | grade | 우선순위 | 호출 가능 |
| --- | --- | --- | --- | --- |
| `card_write` | 카드 저장 | A | 필수 | hr, support, marketing, planning, orchestrator |
| `card_search` | 유사 카드 검색 | read | 필수 | 모든 에이전트 |
| `card_get` | 카드 단건 조회 | read | 필수 | 모든 에이전트, dashboard-api |
| `card_link` | 카드 대체·폐기 연결 | A | 권장 | hr |

`card_write`는 내부 기록이라 A 등급이지만, 규약대로 `execute()`를 거친다. 카드 문장은 쓰는 부서가 책임지고, memory는 저장·검색만 한다.

## 3. skill 상세

모든 skill은 같은 원칙을 따른다: 숫자는 shop-api 조회 결과를 코드(SQL·pandas)로 계산하고, LLM은 결과를 한두 문장으로 설명하는 데만 쓴다. LLM이 실패하면 템플릿 문장으로 대체해 결과 숫자는 그대로 돌려준다.

### anomaly\_detection — 업무 지표 이상 탐지 (필수)

DejaFix가 서버 지표를 보고, data는 장사가 잘못되고 있는지(업무 지표)를 본다.

- **감시 지표**: 결제 실패율, 주문 실패 건수, 주문 수, 문의 접수량
- **계산**: 5분 단위로 집계하고, 지난 7일 같은 시간대 평균과 표준편차로 기준선을 잡는다. 기준선 대비 3σ 초과 또는 평소의 N배 초과면 이상으로 판정
- **동작 방식**: 주기 실행으로 감시하다 이상을 찾으면 이벤트 버스에 `metric.anomaly` 이벤트를 발행한다 (통합 에이전트가 사건을 연다). 요청을 받으면 지정 구간만 분석해 돌려준다
- **출력**: `metric`, `window`, `observed`, `baseline`, `ratio`, `severity`(low/mid/high), `summary`(LLM 문장), `evidence`

### impact\_analysis — 장애 영향 분석 (필수)

- **입력**: `contextId`, 장애 구간(`from`, `to`), 선택: 영향 범위(결제·배송 등)
- **계산**: shop-api의 실패 주문 조회로 구간 내 실패 주문 수, 영향 고객 목록(ID만), 피해 금액(실패 주문 금액 합), 카테고리별 분포를 계산
- **출력**: `failedOrders`, `affectedCustomers`(ID 목록), `lostAmount`, `byCategory`, `summary`, `evidence`(조회 조건, 집계 쿼리, 조회 시각)
- **쓰는 곳**: 고객지원(보상 대상), 재무/정산(손실 집계), 기획개발(배포 영향 확인), 마케팅(공지 범위)
- 개인정보는 고객 ID만 넘기고 이름·연락처는 다루지 않는다

### fraud\_score — 판매자 사기 위험 점수 (권장)

- **입력**: `sellerId`
- **계산 (규칙 점수, 0\~100)**: 가입 기간, 카테고리 중앙값 대비 비정상 저가 상품 비율, 사업자 정보 중복, 짧은 기간 대량 등록, 과거 사기 카드와의 유사도(memory 검색)를 가중합
- **출력**: `score`, `level`(low/mid/high), `reasons`(규칙 코드와 값), `similarCards`, `summary`, `evidence`
- 점수는 판단 재료일 뿐이고 승인·보류는 재무/정산이 정한다

### kpi\_summary — KPI 계산 (권장)

- **입력**: 기간 또는 `contextId` 목록
- **계산**: 공통 로그 저장소에서 사건별 감지→복구 시간(MTTR), 감지→정상화 공지 시간, 자동 처리율, 등급별 처리·차단 건수, 이관 건수, 1차·2차 유사 사건 처리 시간 비교
- **쓰는 곳**: 병환의 KPI 화면이 이 결과를 표시한다. 사람 기준값(팀원이 수동 처리해 측정한 시간)과 나란히 보여 준다

### threshold\_advise — 패턴 감지 임계값 제안 (선택)

고객지원·마케팅이 "평소 대비 N배"의 N을 정할 때, 과거 데이터의 분포를 보고 오탐이 적은 값을 제안한다.

## 4. 회사 기억(memory) 설계

인사의 결정 카드, 고객지원의 사례·공지 카드, 사건 포스트모템이 각자 저장소를 만들지 않도록 memory가 **모든 카드의 단일 저장소**가 된다. 각 부서는 자기 카드를 써서 넣고, 검색 API로 꺼내 쓴다. 판정 문장은 각 부서가 만들고 memory는 저장·검색만 한다.

### 카드 종류

| kind | 쓰는 부서 | 내용 | 쓰는 곳 |
| --- | --- | --- | --- |
| `incident` | hr | 사건 포스트모템: 타임라인·원인·조치 | 2차 장애 때 유사 사건 참조 (SCN-L) |
| `decision` | hr | 결정·근거·버린 대안·상태(유효/대체/폐기) | 계획 비교(기각된 안 경고), 인수인계 |
| `case` | support | 문의·클레임 사례와 판정 결과 | 유사 분쟁 검색, 공지 문구 점검 |
| `notice` | support (마케팅 이벤트를 받아) | 공지·프로모션과 약속(`promises`) | 응대 근거, 보상 중복 확인 |
| `policy` | data (시드) | 환불·보상·입점 정책 문서 | 모든 부서의 근거 인용 |
| `fraud` | finance | 사기 판정 사례 | 사기 위험 점수의 유사도 |

### 공통 필드

`cardId`, `kind`, `tenantId`, `contextId`, `title`, `body`, `tags`, `status`(active / superseded / retired), `supersededBy`, `sourceRefs`(원문 로그 ID·근거 카드 ID), `author`(에이전트), `confidence`(low / mid / high), `createdAt`. 카드 종류별 추가 필드는 `extra`에 넣고 스키마는 `contracts/schemas/`에 정의한다.

### API

| 기능 | 입력 | 출력 |
| --- | --- | --- |
| `card_write` | 카드 본문 (`execute()` 경유, 멱등성 키 = contextId + kind + 제목 해시) | `cardId` |
| `card_search` | `query`, `kind` 필터, `status`(기본 active), `topK` | 카드 목록 + 유사도 점수 |
| `card_get` | `cardId` | 카드 |
| `card_link` | `oldCardId`, `newCardId`, 관계(superseded / retired) | 연결 결과 |

- 검색은 **벡터 유사도 + 필드 필터**를 함께 쓴다 (예: kind = decision, status = active, 최근 90일).
- 확신도 낮은(`confidence: low`) 카드는 검색 결과에서 표시만 하고, 판정 근거로는 쓰지 않도록 응답에 표시한다.
- 카드에 개인정보를 넣지 않는다 (고객 ID까지만).

### 저장소 선택: pgvector 추천

| 기준 | pgvector | Chroma |
| --- | --- | --- |
| 필터 + 벡터 검색 | SQL로 자연스럽게 결합 | 메타데이터 필터 지원, 복잡한 조건은 약함 |
| 운영 | Postgres 하나로 카드·인덱스 같이 관리 | 별도 서비스 추가 |
| 백업·마이그레이션 | 일반 DB 도구 그대로 | 별도 방식 |

카드는 상태 변경(대체·폐기)과 필터 검색이 많아서 pgvector가 맞다. 임베딩은 LLM과 같은 OpenAI 호환 래퍼로 Gemini 임베딩 모델을 쓰고, 모델명은 환경변수로 둔다.

## 5. 데이터 접근 계약

data는 판매 사이트 DB를 직접 읽지 않으므로, 아래 조회를 shop-api(재헌)가 제공해야 한다. 형식은 `contracts/data-access/`에 PR로 올리고 재헌과 합의한다. 공통 로그는 오케스트레이터·안전 레이어가 쌓는 저장소를 읽기 전용으로 쓴다.

**판매 사이트(shop-api)에 요청할 조회**

| 조회 | 입력 | 출력 | 쓰는 skill |
| --- | --- | --- | --- |
| 실패 주문 목록 | `from`, `to`, 선택: `category` | `orderId`, `customerId`, `amount`, `category`, `failedAt`, `reason` | impact\_analysis |
| 지표 시계열 | `metric`(order\_count, payment\_fail\_rate, order\_fail\_count), `from`, `to`, `bucket`(5m) | `[{ts, value}]` | anomaly\_detection |
| 판매자 정보 | `sellerId` | 가입일, 사업자 정보 해시, 상품 수, 등록 추이 | fraud\_score |
| 판매자 상품 가격 | `sellerId` | `productId`, `category`, `price` + 카테고리 중앙값 | fraud\_score |
| 문의 접수 시계열 | `from`, `to`, `bucket`, 선택: `category` | `[{ts, count}]` | anomaly\_detection, threshold\_advise |

- 응답에는 조회 시각(`collectedAt`)과 잘림 여부(`truncated`)를 포함한다 (MCP 공통 봉투와 같은 형태).
- 개인정보는 ID까지만 받는다. 사업자 정보는 원문 대신 해시로 받아 중복만 판별한다.
- shop-api가 준비되기 전에는 같은 형식의 목(mock) 클라이언트와 시드 데이터로 단독 개발한다.

**공통 로그 저장소에서 읽을 것 (kpi\_summary)**

- contextId별 Task 시작·종료 시각과 상태
- 안전 레이어 판정 결과(등급, 실행·차단)
- 이관 기록
- 회사 기억 참조 여부 (2차 사건 비교용)

**이벤트 계약 추가 요청 (준하·재헌)**

- `metric.anomaly`: data가 발행. `metric`, `window`, `observed`, `baseline`, `severity`
- 판매 사이트의 `order.failed`, `payment.latency`, `inquiry.created`, `seller.registered`는 구독만 한다

## 6. 통신 지도와 원인 추적 루프

data는 대부분 "요청을 받아 숫자를 돌려주는" 쪽이고, 먼저 움직이는 건 이상 탐지 이벤트 하나뿐이다. 짝꿍인 기획개발과는 원인 추적 루프로 묶인다.

```
판매 사이트 ──지표 조회(API)──▶ data ──metric.anomaly 이벤트──▶ 통합 에이전트 (사건 개시)

A2A(수신)
  orchestrator / infra-dejafix ──▶ anomaly_detection, impact_analysis
  support / finance / planning  ──▶ impact_analysis
  finance                        ──▶ fraud_score
  dashboard-api                  ──▶ kpi_summary

A2A(요청)
  data ──▶ planning.deploy_trace   (이상 구간 직전 배포가 있었나)
  data ──▶ memory.card_search      (유사 사건·사기 사례)

읽기 전용
  data ──▶ 공통 로그 저장소          (KPI 계산)
```

### 데이터분석 ↔ 기획개발 원인 추적 루프

다른 두 루프(기획개발↔인사, 마케팅↔고객지원)와 같은 분업이다. data는 숫자와 상관관계까지만 말하고, 원인 확정과 조치는 기획개발이 한다.

| # | 방향 | 방식 | 내용 |
| --- | --- | --- | --- |
| ① | data → planning | 동기 질의 | 이상 구간을 주고 "직전 배포가 있었나"를 묻는다 (`deploy_trace`). 배포가 있으면 결과에 "배포 후 지표 변화"를 근거로 첨부 |
| ② | planning → data | 동기 질의 | 롤백·배포 후 "지표가 돌아왔나"를 묻는다 (`anomaly_detection` 구간 분석). 복구 검증의 업무 지표 쪽 근거 |
| ③ | data → planning | 제안 | 같은 지표 이상이 반복되면 개선 티켓 초안 재료(반복 횟수·구간·지표)를 보낸다. 티켓 생성은 기획개발이 결정 |

**루프 규칙**

- data는 "배포 때문이다"라고 단정하지 않는다. "배포 X 이후 결제 실패율이 4.2배"처럼 시간 관계와 숫자만 말한다.
- 원인 판정과 롤백 결정은 기획개발 한 곳에서만 한다.
- ①②의 응답은 같은 Task의 결과로 돌려준다. 새 요청이 아니므로 루프 거부 대상이 아니다.

## 7. 시나리오별 역할

data는 네 시나리오 모두에 들어가며, SCN-A와 SCN-L에서는 흐름의 시작과 증명을 맡는다.

| 시나리오 | data의 역할 | memory의 역할 |
| --- | --- | --- |
| SCN-A 결제 장애 | 결제 실패율 급증을 감지해 `metric.anomaly` 발행 → 영향 분석(실패 주문·영향 고객·피해 금액)으로 보상·손실·공지 범위의 숫자를 제공 → 롤백 후 지표 복구 확인 | 사건 종료 후 hr가 쓴 포스트모템 카드 저장 |
| SCN-B 판매자 입점 | 신규 판매자 사기 위험 점수와 규칙 근거 제공 | 과거 `fraud` 카드 유사도 검색 |
| SCN-L 학습 효과 | 1차·2차 사건 처리 시간을 `kpi_summary`로 비교 | 2차 장애 때 1차 포스트모템을 찾아 각 부서에 제공 → 처리 단축의 근거 |
| SCN-S 안전장치 | 문의 접수량 급증이 인젝션 시도일 때도 숫자만 보고 지시는 따르지 않음 (문의 원문은 untrusted로만 받음) | 카드 본문의 외부 문구도 untrusted로 저장 |

**SCN-A에서 data 구간 (요약)**

1. 5분 집계에서 결제 실패율이 기준선의 4배 → `metric.anomaly`(severity high) 발행
2. 통합 에이전트가 사건을 열고 DejaFix와 data에 진단·영향 분석 요청 (같은 contextId)
3. data가 기획개발에 `deploy_trace` 질의 → "14:02 결제 모듈 배포 이후 실패율 상승" 근거 첨부
4. 영향 분석 결과(실패 주문 수, 영향 고객 ID, 피해 금액)를 통합 에이전트가 고객지원·재무·마케팅에 분배
5. 롤백 후 기획개발이 복구 확인 질의 → data가 지표 정상 범위 복귀를 확인

## 8. 안전장치 적용

대부분은 agent-template과 안전 레이어가 제공하고, data는 쓰는 쪽에서 지킬 것만 챙긴다. data는 조회 위주라 실행 관문 대상은 memory 카드 쓰기뿐이다.

| 안전장치 | data / memory에서 할 일 |
| --- | --- |
| 실행 관문 `execute()` | memory `card_write`, `card_link`만 해당 (A 등급). data skill은 조회라 대상 없음 |
| contextId 필수 | 모든 결과·이벤트·카드에 포함, 없으면 요청 거부 |
| 루프 거부·홉 제한·타임아웃 | 템플릿 사용. 기획개발 질의(①)는 짧은 마감 시각을 둔다 |
| 인증·ACL | Agent Card의 `allowedRequesters` 그대로. 특히 `fraud_score`는 finance·orchestrator만 |
| 신뢰 경계 | 문의 원문·판매자 입력 문구는 `untrusted`로만 받고, 숫자 계산에는 쓰지 않는다 |
| 구조화 출력 | 결과는 pydantic 스키마로 검증, LLM 설명 문장 실패 시 템플릿 문장으로 대체 |
| 계산은 코드로 | 모든 숫자는 SQL·pandas로 산출. LLM이 쓴 문장 속 숫자가 계산 결과와 다르면 문장을 버리고 템플릿 문장 사용 |
| 근거 첨부 | 조회 조건, 집계 쿼리, 조회 시각, 참조 카드 ID를 `evidence`에 |
| 멱등성 | 카드 쓰기는 contextId + kind + 제목 해시 |
| fail-closed | memory 쓰기 판정이 응답 없으면 저장하지 않고 실패 보고 |
| 요청 예산 (권장) | 이상 탐지 주기 실행은 LLM을 부르지 않고, 이상 판정 시에만 설명 문장 생성 |
| CI에서 LLM mock | 모든 테스트는 MockLLMClient + 목 shop 클라이언트 + 시드 데이터 |

## 9. 폴더 구성과 README

두 폴더 모두 독립 패키지이며 서로 import하지 않는다. data가 memory를 쓸 때도 A2A(`card_search`)로 호출한다.

```
ax-backend/
├─ agents/data/                 데이터분석 에이전트 · 재현 (8106)
│  ├─ pyproject.toml · README.md · Dockerfile · agent-card.json
│  ├─ src/data_agent/
│  │  ├─ skills/                anomaly.py · impact.py · fraud.py · kpi.py
│  │  ├─ metrics/               집계·기준선 계산 (코드만, LLM 없음)
│  │  ├─ clients/               shop_client.py (실제 + mock) · log_client.py
│  │  └─ explain.py             LLM 설명 문장 + 템플릿 대체
│  └─ tests/
└─ memory/                      회사 기억 · 재현 (8002)
   ├─ pyproject.toml · README.md · Dockerfile
   ├─ src/memory/
   │  ├─ api.py                 card_write · card_search · card_get · card_link
   │  ├─ store.py               pgvector 저장·검색
   │  ├─ embed.py               임베딩 (OpenAI 호환 래퍼)
   │  └─ seed/policies/         정책 문서 시드
   └─ tests/
```

**README 5항목 (agents/data)**

1. **역할**: 업무 지표 이상 탐지, 장애 영향 분석, 판매자 사기 위험 점수, KPI 계산. 숫자는 코드, 설명은 LLM.
2. **실행 방법**: `docker compose -f infra/compose.yml up data memory`, 목 모드는 `DATA_MOCK=1` (shop-api 없이 시드 데이터로 동작).
3. **입출력 계약**: 2·3장 skill 표, 결과는 `contracts/schemas/`의 AgentResult + `evidence`.
4. **의존**: agent-template·contracts(준하), shop-api 데이터 접근(재헌), 공통 로그 저장소(준하·태윤), memory.
5. **하지 않는 것**: 1장 오른쪽 열.

## 10. 개발 순서와 단독 테스트

1\~3단계는 다른 사람의 규격을 기다리지 않고 바로 시작할 수 있다.

| 단계 | 할 일 | 의존 |
| --- | --- | --- |
| 1 | 시드 데이터와 목 shop 클라이언트: 주문·실패 주문·판매자·문의 시계열 (장애 구간이 들어간 데이터) | 없음 |
| 2 | 계산 코드: 5분 집계, 기준선(7일 같은 시간대), 영향 분석 집계, 사기 규칙 점수 + 단위 테스트 | 없음 |
| 3 | memory 저장소: pgvector 테이블, 임베딩, 검색, 정책 문서 시드 | 없음 |
| 4 | 데이터 접근 계약 PR (5장 표) — 재헌과 합의 | 재헌 |
| 5 | 규격 고정 후: agent-template 위에 data 에이전트·memory API 연결, Agent Card | 준하 (v0.1.0) |
| 6 | `metric.anomaly` 이벤트 발행, 기획개발 `deploy_trace` 질의 연동 | 준하, 예진 |
| 7 | kpi\_summary: 공통 로그 저장소 연동, 병환 KPI 화면에 제공 | 준하·태윤, 병환 |
| 8 | fraud\_score, SCN-L 리허설 (2차 장애에서 memory 참조) | 태윤, 준하 |

**단독 테스트 시나리오 (통합 에이전트 없이 A2A 요청만)**

1. 장애 구간이 들어간 시드로 `anomaly_detection` 요청 → 해당 구간이 high로 판정되는지
2. 같은 구간으로 `impact_analysis` → 실패 주문 수·피해 금액이 시드의 정답값과 일치하는지
3. 같은 입력을 다시 보내 결과가 같은지 (결정성)
4. MockLLMClient가 숫자가 틀린 설명 문장을 내면 템플릿 문장으로 대체되는지
5. allowedRequesters에 없는 요청자로 `fraud_score` → 거부되는지
6. 경로에 `data`가 있는 요청 → 루프로 거부되는지
7. memory에 1차 사건 카드를 넣고, 비슷한 설명으로 `card_search` → 상위에 나오는지

**발표 슬라이드 1장**

- **As-Is**: 장애가 나면 누군가 엑셀로 실패 주문을 뽑아 피해 규모를 계산하고, 과거 비슷한 장애는 기억에 의존해 찾음.
- **To-Be**: 데이터분석 에이전트가 업무 지표 이상을 먼저 감지하고, 피해 규모를 근거와 함께 수 초 안에 계산해 각 부서에 제공. 회사 기억이 과거 사건을 찾아 줘서 2차 장애는 더 빨리 처리.
- **핵심 수치 후보**: 이상 감지까지 걸린 시간, 영향 분석 소요 시간, 1차 대비 2차 사건 처리 시간 단축률, 오탐률.

## 11. 확인 필요

| # | 질문 | 누구와 | 제안 |
| --- | --- | --- | --- |
| 1 | memory를 모든 카드의 단일 저장소로 쓰는 것 (인사 결정 카드, 고객지원 사례·공지 카드 포함) | 준하, 병환 | 각 부서는 카드 작성만, 저장·검색은 memory. 카드 종류는 `kind`로 구분 |
| 2 | 인프라 지표(DejaFix)와 업무 지표(data)의 감시 경계 | 예진 | 서버 응답 시간·에러율은 DejaFix, 주문·결제 실패율·문의량은 data |
| 3 | `metric.anomaly` 이벤트를 계약에 추가 | 준하 | 4장 필드 기준으로 EventType에 추가 |
| 4 | shop-api 데이터 접근 형식 (5장 표) | 재헌 | contracts/data-access/에 PR로 합의, 사업자 정보는 해시로 |
| 5 | 공통 로그 저장소의 위치와 스키마 | 준하, 태윤 | kpi\_summary와 dashboard-api가 같은 저장소를 읽으므로 한 번에 정하기 |
| 6 | 이상 판정 임계값 (3σ / N배) 초기값 | 병환(고객지원 패턴 감지와 공유) | 시드 데이터로 오탐률을 보고 조정 |
| 7 | 사기 위험 점수 규칙과 가중치 | 태윤 | 초기 규칙은 data가 제안, 승인·보류 기준은 재무/정산 |
| 8 | 임베딩 모델 (Gemini 임베딩) 호출 한도 | 팀 | 카드 쓰기 때만 임베딩, 검색은 질의 1회 임베딩. 한도 걸리면 로컬 임베딩 모델로 교체 가능하게 환경변수로 |
