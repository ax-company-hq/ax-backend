# fault-injection

판매 사이트 결제 장애 주입·해제 스크립트 (SHOP-06, 실서버 시나리오 A)

```
python infra/fault-injection/payment_fault.py inject [--kind failure|latency] [--delay-ms N]
python infra/fault-injection/payment_fault.py clear
```

- shop-api `POST·DELETE /api/shop/admin/faults`를 부른다. 기본 `inject`는 `failure`(5xx)이고, `--kind latency`는 지연만 기록한다.
- 환경 변수: `SHOP_API_URL`(기본 `http://localhost:8010`), `SHOP_FAULT_TOKEN`(기본 `mock:admin`, 값은 출력하지 않는다), `SHOP_FAULT_TIMEOUT_S`(기본 10).
- 종료 코드: 0 성공, 1 호출 실패, 2 인자 오류. 표준 라이브러리만 써서 bash·pwsh 모두에서 돈다.
- 주입은 `admin` 토큰만 되므로, 목 모드가 아닌 환경에서는 `SHOP_FAULT_TOKEN`을 admin 토큰으로 넘겨야 한다.
- 테스트(목 서버): `python -m unittest infra/fault-injection/test_payment_fault.py`
