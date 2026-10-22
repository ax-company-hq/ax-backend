# #118 클레임 주문을 shop-api 주문(ord_)으로 바꾸기 — 보류 사유와 전환 계획

## 현재 상태
- 클레임은 `orders.py` 시드 주문(숫자 id 101·102)을 읽는다. `refund_request`의 `order_id`도 이 숫자다.
- 재무는 숫자도 받으므로(목 데이터 101·102) 지금 동작은 깨지지 않는다.

## 왜 이번에 코드를 바꾸지 않았나
`GET /api/shop/orders/{order_id}` 응답은 `order_id(ord_…)`, `buyer_id`, `status`, `total_amount`,
`created_at`, `items[product_id, seller_id, qty, unit_price]`, `payments[...]`뿐이다. 클레임 판정
(`claim_rules.py`)이 쓰는 값이 없다.

| 판정에 필요한 값 | shop-api 응답 |
| --- | --- |
| 배송 완료 시각 `delivered_at` (7일 환불 기간) | 없음 (`status`도 배송 단계 없음) |
| 배송비 `shipping_fee` (배송비 환불·차감) | 없음 |
| 포인트 사용 `points_used`·품목별 `points_share` | 없음 |
| 품목 id·품목명·품목별 실결제액 | `product_id`, `unit_price × qty`만 있음 |
| 숫자 `user_id` | 문자열 `buyer_id` (`usr_…`) |

시드를 그냥 `ord_` 주문으로 바꾸면 위 값을 지어내야 하고, 판정 금액이 실제 결제와 어긋난다.
또 `order_id`는 `Claim`·`CreateClaimInput`·`ClaimStore`·테스트 전반에서 `int`라, 문자열로
바꾸면 `claim_schemas.py`·`claims.py`·`claim_handlers.py`·테스트가 함께 바뀐다.

## 전환 계획 (shop-api가 위 값을 주면 진행)
1. `orders.py`에 shop-api 조회 어댑터를 둔다 (`httpx`, 주소는 `.env`, 읽기 전용). 응답을 `Order`로 매핑한다.
2. `Order.id`와 `Claim.order_id`·`CreateClaimInput.order_id`를 `str`(`ord_…`)로 바꾸고 `user_id`는 `buyer_id`로 맞춘다.
3. `process_claim_queue`의 `refund_request` `order_id`를 `ord_…`로 보낸다 (`ref_ids`도 동일).
4. 목 모드는 시드 주문을 `ord_` 번호로 바꿔 유지하고, 재무 목 데이터(101·102)와 번호를 맞춘다.
5. 환불 금액이 배송비를 포함하면 shop-api 결제 합계에도 배송비가 들어 있어야 재무 검증이 맞다 (이슈 확인 항목).
   현재 shop-api `total_amount`는 상품 합계뿐이라 배송비 포함 환불은 재무에서 불일치로 보류된다.

## 요청
- shop-api: 주문 응답에 `delivered_at`, `shipping_fee`, `points_used`, 품목별 `order_item_id`·`title`·`paid_amount`·`points_share`를 추가하고, `total_amount`/결제 금액에 배송비 포함 여부를 정해 달라.
