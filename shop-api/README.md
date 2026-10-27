# shop-api
담당: 재헌 · 포트: 8010
## 1. 역할
판매 사이트 서버(LLM 없음, 코드만). 도서·문구 오픈마켓의 상품·주문·가짜 결제 데이터의 원천이고, 모든 사건의 출발점인 이벤트를 발행한다.
- PR 1: 상품 관리, 주문(장바구니 없이 바로), 가짜 결제(성공·실패 응답), 시드 데이터, 주문·결제 이벤트 4종 발행.
- PR 2: 장애 주입·해제(SHOP-06)와 `shop.degraded`·`shop.recovered` 직접 측정.
- PR 3: 공지 배너 API(SHOP-08)와 쿠폰 발급 API(SHOP-10).
- 이후 PR: data용 조회 API(SHOP-11). 환불 API(SHOP-12)와 2차 이벤트(배송·반품·환불)는 2차다.
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
| `GET /api/shop/users`, `GET /api/shop/users/{buyer_id}`, `POST /api/shop/sessions` | 쇼핑몰 화면용 데모 사용자. 가명 ID(`buyer_id`)와 표시 이름(`display_name`)뿐이고 비밀번호·토큰은 없다. `sessions`는 `{buyer_id}`를 받아 `{user}`를 돌려주는 "선택"이며 서버는 세션을 저장하지 않는다 |
| `GET /api/shop/categories` | 판매 중 상품의 카테고리(`category_code`·`name`·`product_count`). 표시 이름을 모르는 코드는 `name`이 null |
| `POST /api/shop/cart/price` | 장바구니 가격 미리보기(저장·재고 차감 없음). `items`, 선택 `points_used`. 상품 합계 → 배송비(5만 원 미만 3,000원) → 쿠폰 → 적립금 순서. 쿠폰은 주문에 입력 경로가 없어 `coupon_discount`가 null이고 `coupon_unknown_reason`을 준다(확인 필요). `payable_amount`는 `POST /orders`의 `total_amount`와 같다 |
| `POST /api/shop/orders` | 주문. 가격·총액은 DB 값으로 계산하고 재고를 차감한다. 선택 `points_used`(원, 품목 합계 이하)를 받는다. `total_amount` = 품목 합계 − 포인트 + `shipping_fee`(품목 합계 5만 원 미만 3,000원, 이상 0원)로 **실제 결제(현금) 금액**이고 결제 `amount`와 같다 |
| `GET /api/shop/orders[?buyer_id]`, `GET /api/shop/orders/{id}` | 주문 조회. 주문 필드 `shipping_fee`·`points_used`·`delivered_at`(배송 전 null), 품목 필드 `order_item_id`(`oit_`)·`title`(주문 당시 이름)·`points_share`(포인트를 품목 금액 비율로 나눈 몫, 남는 1원 단위는 뒤 품목부터)·`paid_amount`(= 단가×수량 − `points_share`), 결제 필드 `created_at`. 항상 `total_amount` = Σ`paid_amount` + `shipping_fee` |
| `POST /api/shop/orders/{id}/payments` | 가짜 결제. `simulate`: `success`·`failure`. `pending_payment`·`payment_failed` 주문만 결제할 수 있고(그 밖은 409 `ORDER_NOT_PAYABLE`), 실패한 주문을 다시 부르면 새 결제 기록이 쌓이며 성공하면 `paid`가 된다. 상태 확인·결제 기록·상태 변경은 한 트랜잭션이라 동시에 들어와도 성공 결제는 1건이고 나머지는 `ORDER_NOT_PAYABLE`이다. 실패 코드(`PG_UNAVAILABLE` 서버 문제 / `PG_DECLINED` 거절)는 응답의 `payments[].failure_code`와 `payment.failed` 이벤트 `failure_code`에 그대로 담긴다 |
| `POST /api/shop/admin/orders/{id}/shipped` | 발송 시각 기록(시연·시드용). `admin` 토큰만, 결제된 주문만(409 `ORDER_NOT_PAID`), 본문 `shipped_at`(시간대 있는 시각, 비우면 지금), 이미 있으면 처음 값을 유지한다. 이미 도착한 주문이면 도착 시각보다 늦을 수 없다(409 `INVALID_TIME`). 주문 상태는 바꾸지 않고 이벤트도 내지 않는다. 주문 응답에는 `shipped_at`과 **약속 배송일 `promised_delivery_at`**(결제 성공 시각 + 3일, 결제 전·옛 주문은 null)이 있다 — 고객지원이 '약속한 날보다 늦었나'를 코드로 판정하는 근거 |
| `POST /api/shop/admin/orders/{id}/delivered` | 배송 완료 시각 기록(시연·시드용). `admin` 토큰만, 결제된 주문만(409 `ORDER_NOT_PAID`), 본문 `delivered_at`(시간대 있는 시각, 비우면 지금), 이미 있으면 처음 값을 유지한다. 주문 상태는 바꾸지 않고 `order.delivered` 이벤트도 내지 않는다 |
| `POST /api/shop/claims` | 고객 반품·환불 신청(토큰 없음, 주문 구매자만). 고객지원 `create_claim`을 A2A로 부른다(요청자 `shop-api`, 사건 ID `ctx_shop_<hex>`). 금액은 받지 않고, 사유 문장 `description`은 `untrusted`로만 넘긴다. 응답 `data`는 고객지원의 클레임이고 `meta.context_id`가 사건 ID다. 구매자가 다르면 403 `BUYER_MISMATCH`, 고객지원 거부·타임아웃·오류는 403·504·502 |
| `POST /api/shop/inquiries` | 고객 문의 접수(토큰 없음, #385 B안). 요청 `{buyer_id, order_id, subject?, body}`로 `order_id`는 필수이고 그 구매자의 주문이어야 한다(없는 주문과 남의 주문은 같은 404 `ORDER_NOT_FOUND`). 고객지원 `create_inquiry`를 A2A로 부른다(`buy_0007` → `user_id` 7, `customer_name`은 구매자 표시 이름, 제목·본문은 `untrusted`). 멱등 키는 구매자·주문·제목·본문과 시간 구간 번호(`floor(epoch/창)`)의 해시로 만들어, 같은 구간(`SHOP_INQUIRY_DEDUP_WINDOW_SEC`, 기본 600초, 0 이하·잘못된 값이면 기본값)의 같은 접수는 한 번만 처리되고 창이 지나면 새 문의가 된다(구간 경계에서 연타하면 키가 둘로 갈릴 수 있으나 수용). 원문은 로그·키에 남기지 않는다. 응답 `data`는 `{id, status, order_id, updated_at}`뿐이고 `meta.context_id`가 사건 ID다. 형식·빈 본문·길이 초과 400, 속도 제한 429 `RATE_LIMITED`(`SHOP_INQUIRY_RATE_PER_BUYER` 기본 5/분, `SHOP_INQUIRY_RATE_GLOBAL` 기본 60/분, 본문 `SHOP_INQUIRY_MAX_BODY` 2000자, 제목 `SHOP_INQUIRY_MAX_SUBJECT` 100자), 고객지원 오류는 502·504·403. 주문·결제는 읽기만 한다 |
| `POST /api/shop/admin/claims/{claim_id}/return-received` | 반품 입고(`admin` 토큰만). 고객지원 `receive_return`을 부른 뒤 `ret_` 번호로 기록하고 `return.received`를 발행한다. 같은 클레임을 다시 부르면 고객지원도 이벤트도 다시 내지 않고 이전 기록을 돌려준다 |
| `POST /api/shop/payments/{id}/refund` | 현금 환불(SHOP-12, **`support` 토큰만**, 마케팅·재무·관리자 거부). `refund_amount`·`idempotency_key`(`<claim.context_id>:refund_request:claim:<id>`)·선택 `context_id`. 같은 키·같은 내용은 이전 환불을 200으로 돌려주고(`meta.replayed`), 다르면 409, 성공한 결제의 환불 가능 금액을 넘으면 409 `REFUND_EXCEEDS_PAYMENT`. 반영되면 `payment.refunded`를 발행하고 주문 `refunded_amount`가 늘어난다(상태는 `paid` 그대로) |
| `GET·POST·DELETE /api/shop/admin/faults` | 장애 상태 조회·주입(`kind`: `latency`+`delay_ms`, `failure`)·해제. 주입은 `admin`, 해제는 `admin`·`infra` 토큰만(`Authorization: Bearer`) |
| `GET /api/shop/admin/orchestrator/incidents`, `GET /api/shop/admin/orchestrator/incidents/{context_id}` | 관리자 화면(shop-web)용 총괄 사건 읽기 중계(GET만, `admin` 토큰만). 총괄(`SHOP_ORCHESTRATOR_URL`, 기본 `http://127.0.0.1:8000`)의 `/api/incidents`를 그대로 전달한다. 총괄이 꺼져 있으면 502 `UPSTREAM_UNAVAILABLE`, 느리면 504 `UPSTREAM_TIMEOUT`, 사건이 없으면 404, 사건 번호 형식(`ctx_inc_<hex>`)이 아니면 400 `INVALID_CONTEXT_ID`. 쓰기·승인은 하지 않는다 |
| `GET /api/shop/admin/orchestrator/departments` | 관리자 화면(shop-web)용 부서 카드·사용량 읽기 중계(GET만, `admin` 토큰만). 총괄 `/api/departments`를 그대로 전달하고 `meta`의 `mock`·`window_seconds`·`bucket_seconds`를 함께 돌려준다. 오류 처리는 사건 중계와 같다(총괄이 꺼져 있으면 502, 느리면 504). 총괄 실제 모드는 501을 돌려주므로 `UPSTREAM_ERROR`(502)로 나온다 |
| `GET /api/shop/banners`, `POST /api/shop/banners`, `DELETE /api/shop/banners/{notice_id}` | 공지 배너. 고객 화면은 토큰 없이 게시 중이고 기간 안인 배너만 읽고, 게시·해제는 `marketing` 토큰만(같은 `notice_id` 재게시는 첫 배너 그대로) |
| `POST /api/shop/coupons`, `GET /api/shop/coupons[?buyer_id&purpose_code]`, `GET /api/shop/coupons/{id}` | 쿠폰. 발급은 `marketing` 토큰만, `idempotency_key` 필수(같은 키·같은 내용은 이전 쿠폰, 다르면 409), 총액은 서버가 건수×단가로 계산, 조회는 `marketing`·`support`·`finance`·`orchestrator` |
| `GET /api/shop/failed-orders?from&to[&category&limit]` | 실패 주문 목록(결제 `failed` 한 건이 한 줄, `category`는 첫 품목 분류, `reason`은 실패 코드). `data`·`admin` 토큰만. `from`·`to`는 시간대 있는 UTC 시각, `limit` 기본 1000(최대 5000)이고 넘으면 `truncated` |
| `GET /api/shop/metric-series?metric&from&to[&bucket]` | 지표 시계열(`order_count`·`order_fail_count`·`payment_fail_rate`·`payment_count`, 5분 버킷). 건수는 빈 버킷을 0으로 채우고 비율은 결제 시도가 없는 버킷을 뺀다(값이 없는 것과 0%를 섞지 않는다). 7일(2016점)을 넘으면 `truncated`. `data`·`admin` 토큰만 |
| `POST /api/shop/orders/{id}/cancel` | 미결제·결제 실패 주문 취소(결제된 주문의 환불은 SHOP-12) |

- 장애 측정: 결제 시도마다 지연·5xx를 기록하고, **p95 1,000ms 초과 또는 5xx 2% 초과가 1분 지속**되면 `shop.degraded`(`component: payment`, `metric_code`: `p95_latency_ms`·`error_rate_5xx`)를, 해제하거나 정상으로 돌아오면 `shop.recovered`를 발행한다. 기준값은 `SHOP_DEGRADED_P95_MS`·`SHOP_DEGRADED_5XX_RATE`·`SHOP_DEGRADED_SUSTAIN_S`·`SHOP_MONITOR_WINDOW_S`로 바꾸고, 주기 판정 간격은 `SHOP_MONITOR_INTERVAL_S`(0이면 끔)다. 지연은 값만 기록한다(실제로 기다리지 않는다).
- `notice.published`·`coupon.issued`는 shop-api가 아니라 마케팅 에이전트가 `execute()` 뒤에 발행한다(shop-api의 배너·쿠폰 API는 그 결과를 반영만 한다).
- 발행 이벤트(공통 봉투, `source: shop-api`, `context_id: null`, 이벤트 버스는 `events.publish`만 사용): `order.created`, `order.cancelled`, `payment.succeeded`, `payment.failed`, `shop.degraded`, `shop.recovered`. 이벤트는 주문·결제와 같은 트랜잭션에 outbox로 저장하고 저장 뒤 발행한다(최소 한 번 전달, `event_id`는 키에서 만든다).
- 이벤트 `data` 모델은 계약(`contracts`)에 있으면 그것을 쓴다(`payment.failed`). 나머지 모델은 `to:contracts` 요청(#11)으로 추가 중이라, 반영되면 계약 모델로 교체한다.
- OpenAPI: `contracts/openapi/shop-api.json`은 CI가 자동 생성한다.
## 4. 의존
`ax-contracts`, `ax-agent-template`. 다른 부서 폴더를 import하지 않는다.
## 4-1. 보유기간 삭제 (retention)
개인정보처리방침 제4조의 보유기간을 코드로 옮긴 것이다(`retention.py`). **삭제는 기본 dry-run(건수만 센다)이고, 실제 삭제(apply)는 사람이 켠다.** 5년은 방침 제4조 기준이며, 모의 거래에도 적용할지(제4조 ⑦)는 미확정이라 설정값이다. 외부에 열린 삭제 엔드포인트는 없고, 삭제 요청의 집행은 사람 승인 뒤 운영자가 CLI로만 한다.

| 환경변수 | 기본값 | 뜻 |
| --- | --- | --- |
| `SHOP_RETENTION_TRANSACTION_DAYS` | 1825 (5년) | 주문·품목·결제·환불·반품·쿠폰의 보유 일수(기준은 각 행의 `created_at`) |
| `SHOP_RETENTION_OUTBOX_DAYS` | 90 | **발송이 끝난**(`sent_at` 있음) outbox 이벤트의 보유 일수 |
| `SHOP_RETENTION_BUYER_DAYS` | 90 | 구매자 행의 보유 일수(남은 주문·쿠폰이 없을 때만) |
| `SHOP_RETENTION_ENABLED` | 1 | 하루 한 번 도는 백그라운드 작업 켜기/끄기(`0`·`false`·`off`·`no`면 끔) |
| `SHOP_RETENTION_APPLY` | (없음) | `1`일 때만 백그라운드가 실제로 지운다. 아니면 건수만 로그 |

- 일수는 양의 정수(상한 36500)만 받고, 비었거나 정수가 아니거나 0 이하면 기본값이다.
- 만료 기준은 "기준 시각 이전 또는 같음"(`created_at <= now − N일`)이다.
- 백그라운드 작업은 **DB가 파일일 때만**(`SHOP_DB_PATH`) 앱 시작 때 켜지고 하루에 한 번 돈다. `:memory:`면 건너뛴다. 실패해도 앱은 계속 돌고 로그에는 예외 종류만 남는다.

| 대상 | 지우는 조건 | 남기는 것 |
| --- | --- | --- |
| `refunds`·`returns` | `created_at`이 5년 이상 지남 | 5년 안의 행 |
| `payments` | 5년 이상 지났고 남은 환불이 없음 | 환불이 남아 있는 결제 |
| `order_items`·`orders` | 주문이 5년 이상 지났고 남은 결제·환불·반품이 없음(품목은 주문과 함께) | 더 새로운 결제·환불·반품이 붙은 주문 |
| `coupons` | `created_at`이 5년 이상 지남 | 5년 안의 쿠폰 |
| `outbox_events` | 발송 완료(`sent_at` 있음) + 90일 이상 | **발송 안 된 이벤트는 몇 년이 지나도 남김** |
| `buyers` | 90일 이상 지났고 남은 주문·쿠폰이 없음 | 5년 보관 주문·쿠폰이 있는 구매자(외래키) |
| 시드 데모 구매자(`seed.SEED_BUYER_IDS`) | **절대 지우지 않음.** 그 구매자의 주문·쿠폰도 대상에서 제외 | 전부 |

- 지우는 순서는 자식 → 부모(refunds, returns, payments, order_items, orders, coupons, outbox_events, buyers)이고 한 트랜잭션이다.
- 지우기 전에 **일별 집계**만 `retention_daily_stats`(`stat_date`·`kind`·`rows_count`·`amount_sum`·`tenant_id`)에 더한다. 구매자·주문 번호 같은 식별 정보는 넣지 않는다. 테이블은 `CREATE TABLE IF NOT EXISTS`라 기존 DB 파일에도 시작할 때 만들어진다.
- 보고서·로그·CLI 출력에는 건수만 있다(구매자 ID·금액 없음).

CLI(`SHOP_DB_PATH`가 가리키는 파일 DB에서 실행, 기본은 dry-run):
```
python -m ax_shop_api.retention run [--apply]
python -m ax_shop_api.retention purge-buyer <buyer_id> [--apply] [--include-retained]
```
- `run`: 만료분 정리. `--apply`가 없으면 건수만 센다.
- `purge-buyer`: **정보주체 삭제 요청의 집행부.** AI의 1차 본인 확인과 사람의 최종 판단이 끝난 뒤에만 운영자가 쓴다.
  - 기본: 법정 보존 대상(주문·품목·결제·환불·반품·쿠폰, 5년)을 **남기고**, 그 구매자 관련 **발송 완료 outbox 이벤트**(페이로드의 `buyer_id` 또는 그 구매자 주문 번호로 찾음)와, 남은 주문·쿠폰이 없을 때의 구매자 행만 지운다. 발송 안 된 이벤트는 남긴다.
  - `--include-retained`: 그 구매자의 주문·품목·결제·환불·반품·쿠폰까지 지운다(프로젝트 종료 또는 법적 보존 비적용이 확정됐을 때만).
  - 시드 구매자는 거부(종료 코드 2), 없는 구매자는 종료 코드 1.
- 종료 코드 2: `:memory:` DB에서는 실행하지 않는다.
## 5. 하지 않는 것
- 판단·분석 문장을 만들지 않는다 (이벤트에는 사실만 담는다)
- 다른 서비스의 DB를 읽지 않는다. 판매 사이트 DB를 다른 서비스가 직접 읽게 하지 않는다 (API로만)
- 별도 HTTP 이벤트 엔드포인트를 만들지 않는다 (`events.publish`만)
- 결제된 주문을 환불 API 없이 취소하지 않는다
- 개인정보를 저장하지 않는다 (구매자는 가명 ID만)
