# dashboard-api
담당: 병환 · 포트: 8020
## 1. 역할
상담원 화면(ax-frontend `dashboard-web`)이 쓰는 API 서버. LLM 없이 코드로만 돈다.
- 문의·클레임·보상 큐는 support 에이전트(A2A skill), 주문·후기는 shop-api, KB는 memory, 집계는 공통 로그 저장소에서 받아 화면 형태로 돌려준다.
- 현재(1~2단계)는 목 데이터로 읽기 API만 연다. 쓰기(승인·지급 등)는 support의 `execute()`를 거치도록 이후 단계에서 연다.
## 2. 실행 방법
`docker compose -f infra/compose.yml up dashboard-api` / 목 모드: `DASHBOARD_MOCK=1`
목 모드가 아니면 `/api/dashboard/*`는 `503 UPSTREAM_NOT_CONNECTED`를 돌려준다.
## 3. 입출력 계약
응답은 성공 `{data, meta}`, 실패 `{error: {code, message}}`. 모델은 `schemas.py`이고 OpenAPI는 `contracts/openapi/dashboard-api.json`으로 CI가 내보낸다.

### 엔드포인트 (GET)
| 경로 (`/api/dashboard` 아래) | 옛 경로 (`/api/admin` 등) |
| --- | --- |
| `/health` | `/api/health` |
| `/users` | `/api/users` |
| `/metrics?days` | `/admin/dashboard?days` |
| `/kpis` | `/admin/kpi` |
| `/kb-chunks` | `/admin/kb` |
| `/conversations`, `/conversations/{id}` | `/admin/conversations`, `/admin/conversations/{id}` |
| `/orders?user_id` | `/admin/orders?user_id` |
| `/claims?status`, `/claims/stats`, `/claims/{id}` | `/admin/claims...` |
| `/queue-entries` | `/admin/queue` |
| `/reviews` | `/admin/reviews` |

## 4. 의존
ax-contracts, ax-agent-template
## 5. 하지 않는 것
- 다른 서비스 DB를 직접 읽지 않는다. 판매 사이트 환불·쿠폰 API를 직접 호출하지 않는다.
- LLM을 직접 호출하지 않는다 (답변 초안·가드·요약은 support 에이전트).
