# DejaFix로 보내는 고객 개인정보 암호화 규격 (고객지원 → DejaFix)

대상: 예진(DejaFix 어댑터, `agents/infra-dejafix`), 병환(고객지원). 구현은 `agents/support/src/support_agent/secure_transfer.py`.

## 원칙
- 고객 개인정보는 **캣북스 쪽에만** 있다. DejaFix는 평문 개인정보를 받지도 저장하지도 않는다.
- 고객지원이 DejaFix로 보내는 요청의 고객 개인정보는 **필드 단위로 암호화**해서 보낸다. 나머지 사실(주문 ID, 구매자 가명 ID `buy_…`, 금액, 건수, 시각)은 평문이다.
- 암호화하지 않은 개인정보(전화번호, 이메일, 카드번호, 주민번호, 계좌)가 섞이면 고객지원은 **보내지 않는다**. 키가 없어도 보내지 않는다.

## 어떤 값이 암호화되나
- 요청 `input`: 이름이 아래에 있는 필드는 값이 무엇이든(중첩된 곳 포함) 암호화한다.
  `customer_name`, `name`, `email`, `phone`, `address`, `message`, `text`, `subject`, `description`, `receipt_text`, `images`
- 요청 `untrusted`: **모든 값**을 암호화한다 (고객이 쓴 글이므로).
- 그 밖의 값은 평문이지만, 개인정보 패턴이 있으면 요청 전체가 막힌다.

## 암호문 모양 (필드 하나)
```json
{ "__enc__": 1, "alg": "AES-256-GCM", "kid": "k1", "nonce": "<base64 12바이트>", "ct": "<base64>" }
```
- 알고리즘: AES-256-GCM, 키 32바이트, nonce 12바이트(요청마다 무작위).
- 평문: 값을 JSON(UTF-8, `ensure_ascii=False`, 공백 없는 구분자)으로 직렬화한 바이트.
- **AAD(추가 인증 데이터)**: `"1|<context_id>|<필드 이름>"` (UTF-8). 요청 `meta.context_id`와 필드 이름이 다르면 복호화가 실패한다. 같은 암호문을 다른 요청이나 다른 필드에 옮겨 붙일 수 없다.
- `kid`는 키 이름이다. 키를 바꿀 때 구분한다.

## 복호화 (DejaFix 쪽 기준 구현)
```python
import base64, json
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

def decrypt_field(blob: dict, *, context_id: str, field: str, key: bytes):
    assert blob.get("__enc__") == 1 and blob.get("alg") == "AES-256-GCM"
    nonce = base64.b64decode(blob["nonce"], validate=True)
    ct = base64.b64decode(blob["ct"], validate=True)
    aad = f"1|{context_id}|{field}".encode()
    return json.loads(AESGCM(key).decrypt(nonce, ct, aad))  # 실패하면 InvalidTag
```
`field`는 그 값이 들어 있던 키 이름이다 (`input.customer_name`이면 `customer_name`, `untrusted.message`이면 `message`).

## 키
- 환경변수 `SUPPORT_DEJAFIX_KEY`(고객지원 쪽), DejaFix 쪽은 같은 값을 자기 환경변수(이름은 DejaFix가 정함)에 둔다. base64로 인코딩한 32바이트.
- 만들기: `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`
- **레포·이슈·Slack에 키를 올리지 않는다.** 두 담당이 따로(직접, 비밀 저장소 등) 나눈다. `SUPPORT_DEJAFIX_KEY_ID`(기본 `k1`)로 키 이름을 붙여 두면 교체할 때 구분된다.
- 키를 바꾸면 새 `kid`로 보내고, DejaFix는 두 키를 잠시 함께 받는다.

## DejaFix가 지킬 것
- 복호화한 값은 **메모리에서만** 쓰고 저장하지 않는다 (DB, 로그, 이벤트, 활동 기록 모두).
- 복호화에 실패하면(`InvalidTag`) 요청을 거부한다. 키가 없다고 평문으로 대신 요청하지 않는다.
- 암호문이 아닌 평문 개인정보가 오면(규격 위반) 거부하고 고객지원에 알린다.
- 응답에 고객 개인정보가 필요하면 같은 규격으로 암호화해서 돌려준다 (필드 이름 목록은 위와 같고, `context_id`는 그 요청의 값).

## 아직 없는 것
- 지금 DejaFix 카드에는 고객지원이 부르는 skill이 없다. 이 규격은 미리 갖춰 둔 것이고, 어떤 skill에 어떤 고객 정보가 필요한지는 DejaFix가 정해 알려 주면 `CUSTOMER_FIELDS`에 필요한 이름을 더한다.
- 키 교체 절차와 DejaFix 쪽 키 보관 방식은 두 담당이 정한다.
