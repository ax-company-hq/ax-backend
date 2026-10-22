# data-access

다른 서비스 데이터 접근 형식(API·읽기 전용 뷰)을 여기에 정의한다. 각 서비스 담당이 PR로 추가한다.
다른 서비스의 DB에 직접 붙지 않는다. 데이터는 이 형식으로 정의한 API·읽기 전용 뷰로만 읽는다.

형식은 import할 수 있게 파이썬 패키지 안(`src/ax_contracts/data_access/`)에 pydantic 모델로 둔다.
이 폴더는 목록과 규칙을 설명하는 자리다.

## 규칙

- 입력과 출력을 모델 하나씩(또는 쌍으로) 정의한다. 모든 모델에 `tenant_id`를 둔다.
- 개인정보는 가명 ID(`buyer_id` 등)만 쓴다. 금액은 정수 원, 시간은 UTC다.
- 파일 이름은 `<서비스>_<조회 내용>.py` (예: `shop_failed_orders.py`).

## 목록

| 파일 | 제공 | 사용 | 내용 |
| --- | --- | --- | --- |
| `shop_failed_orders.py` | shop-api (재헌) | data | 실패 주문 목록. 입력 `from`, `to`, `category` → 출력 `order_id`, `buyer_id`, `amount`, `category`, `failed_at`, `reason`, `tenant_id` (예시 스텁) |
