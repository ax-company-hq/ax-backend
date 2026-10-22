# shop-api
담당: 재헌 · 포트: 8010
## 1. 역할
판매 사이트 서버(LLM 없음, 코드만). 도서·문구 오픈마켓의 상품·주문·가짜 결제 데이터의 원천이고, 모든 사건의 출발점인 이벤트를 발행한다.
- PR 1: 상품 관리, 주문(장바구니 없이 바로), 가짜 결제(성공·실패 응답), 시드 데이터, 주문·결제 이벤트 4종 발행.
- PR 2: 장애 주입·해제(SHOP-06)와 `shop.degraded`·`shop.recovered` 직접 측정.
- 이후 PR: 공지 배너·쿠폰 API(SHOP-08·10), data용 조회 API(SHOP-11). 환불 API(SHOP-12)와 2차 이벤트(배송·반품·환불)는 2차다.
## 2. 실행 방법
- 서버: `docker compose -f infra/compose.yml up shop-api`, 또는 `uv run uvicorn ax_shop_api.main:app --port 8010`
- 목 모드: `SHOP_MOCK=1` (CI는 항상 1). 저장소는 SQLite이며 `SHOP_DB_PATH`로 파일을 정한다(기본 `:memory:`). 비어 있으면 시드를 넣는다.
- 단독 테스트: `uv run pytest shop-api`
## 3. 입출력 계약
- 경로 `/api/shop/<복수 명사>`, 성공 `{data, meta}`, 실패 `{error: {code, message}}`. 금액은 정수 원, 시간은 UTC.

| 메서드·경로 | 내용 |
| --- | --- |
| `GET /api/shop/sellers`, `GET /api/shop/products[?seller_id&category_code]`, `GET /api/shop/products/{id}` | 판매자·상품 조회 |
| `POST /api/shop/products`, `PATCH /api/shop/products/{id}` | 상품 등록·수정(판매 중지는 `active=false`) |
| `POST /api/shop/orders` | 주문. 가격·총액은 DB 값으로 계산하고 재고를 차감한다 |
| `GET /api/shop/orders[?buyer_id]`, `GET /api/shop/orders/{id}` | 주문 조회 |
| `POST /api/shop/orders/{id}/payments` | 가짜 결제. `simulate`: `success`·`failure` |
| `GET·POST·DELETE /api/shop/admin/faults` | 장애 상태 조회·주입(`kind`: `latency`+`delay_ms`, `failure`)·해제. 주입은 `admin`, 해제는 `admin`·`infra` 토큰만(`Authorization: Bearer`) |
| `POST /api/shop/orders/{id}/cancel` | 미결제·결제 실패 주문 취소(결제된 주문의 환불은 SHOP-12) |

- 장애 측정: 결제 시도마다 지연·5xx를 기록하고, **p95 1,000ms 초과 또는 5xx 2% 초과가 1분 지속**되면 `shop.degraded`(`component: payment`, `metric_code`: `p95_latency_ms`·`error_rate_5xx`)를, 해제하거나 정상으로 돌아오면 `shop.recovered`를 발행한다. 기준값은 `SHOP_DEGRADED_P95_MS`·`SHOP_DEGRADED_5XX_RATE`·`SHOP_DEGRADED_SUSTAIN_S`·`SHOP_MONITOR_WINDOW_S`로 바꾸고, 주기 판정 간격은 `SHOP_MONITOR_INTERVAL_S`(0이면 끔)다. 지연은 값만 기록한다(실제로 기다리지 않는다).
- 발행 이벤트(공통 봉투, `source: shop-api`, `context_id: null`, 이벤트 버스는 `events.publish`만 사용): `order.created`, `order.cancelled`, `payment.succeeded`, `payment.failed`, `shop.degraded`, `shop.recovered`. 이벤트는 주문·결제와 같은 트랜잭션에 outbox로 저장하고 저장 뒤 발행한다(최소 한 번 전달, `event_id`는 키에서 만든다).
- 이벤트 `data` 모델은 계약(`contracts`)에 있으면 그것을 쓴다(`payment.failed`). 나머지 모델은 `to:contracts` 요청(#11)으로 추가 중이라, 반영되면 계약 모델로 교체한다.
- OpenAPI: `contracts/openapi/shop-api.json`은 CI가 자동 생성한다.
## 4. 의존
`ax-contracts`, `ax-agent-template`. 다른 부서 폴더를 import하지 않는다.
## 5. 하지 않는 것
- 판단·분석 문장을 만들지 않는다 (이벤트에는 사실만 담는다)
- 다른 서비스의 DB를 읽지 않는다. 판매 사이트 DB를 다른 서비스가 직접 읽게 하지 않는다 (API로만)
- 별도 HTTP 이벤트 엔드포인트를 만들지 않는다 (`events.publish`만)
- 결제된 주문을 환불 API 없이 취소하지 않는다
- 개인정보를 저장하지 않는다 (구매자는 가명 ID만)
