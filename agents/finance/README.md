# agents/finance
담당: 태윤 · 포트: 8105
## 1. 역할
재무/정산 + **마켓운영** 에이전트. 마켓운영(판매자 심사·정산 보류)은 별도 부서가 아니라 재무의 도메인 skill로 흡수했다 (기획서 2장, 구성안 수정 요청).
숫자는 전부 코드로 계산하고, 요청 쪽이 적은 총액·등급은 믿지 않는다.

| skill | 등급 | 요청자 | 우선순위 | 하는 일 |
| --- | --- | --- | --- | --- |
| `verify_amount` | read | safety | required | 안전 레이어가 라우팅한 B 액션 금액을 원천 데이터로 재계산해 승인·거절 (DEP-FIN-02) |
| `loss_aggregation` | read | orchestrator | required | data 영향 분석 + 같은 사건의 보상 쿠폰 지급 합계 = 손실 리포트 (DEP-FIN-01) |
| `seller_review` | B (검증자 data) | orchestrator | recommended | 서류 확인 + data 위험 점수로 판매자 승인 요청 (DEP-FIN-03) |
| `settlement_hold` | B (검증자 data) | orchestrator | recommended | 위험 판매자 정산 보류 요청 (DEP-FIN-04) |

`verify_amount` 판정 규칙:
- `issue_coupon`: `count × unit_value`를 다시 계산해 요청 총액(`total_value`·`claimed_total`)과 safety 계산값이 모두 같아야 한다. 보상 쿠폰(`compensation`)은 `buyer_id`가 data 영향 고객이어야 한다
- `refund_request`: 결제 금액 − 이미 환불한 금액(`payment.refunded` 누적) 이하여야 한다
- 원천 데이터를 못 받으면 승인하지 않는다(`source_unavailable`). 같은 `execution_id`는 이전 판정을 돌려준다

## 2. 실행 방법
`docker compose -f infra/compose.yml up finance` / 목 모드: `FINANCE_MOCK=1` (시드 `src/finance_agent/mock_seed.json`)

테스트: `uv run pytest agents/finance`

```bash
# 손실 리포트 (목 모드, orchestrator로 요청)
echo '{"window_from":"2026-10-22T05:00:00Z","window_to":"2026-10-22T05:40:00Z"}' > in.json
uv run ax-test-call --agent http://localhost:8105 --skill loss_aggregation --input in.json \
  --context-id ctx_inc_0001
```

## 3. 입출력 계약
- 입출력 모델: `src/finance_agent/schemas.py` (Agent Card의 `input_schema`·`output_schema`). safety와 함께 쓰는 `VerifyAmountInput`·`VerifyAmountOutput`은 contracts로 옮길 예정
- 구독 이벤트: `coupon.issued`(보상 쿠폰 지급 누적), `payment.refunded`(환불 누적)
- 부르는 것: data `impact_analysis`(`failed_order_count`·`affected_customer_count`·`damage_amount` + 영향 고객 ID `affected_customer_ids`), data `fraud_score`(`risk_score` 0~100), safety `execute()`(판매자 승인·정산 보류)

## 4. 의존
ax-contracts, ax-agent-template, data 에이전트(`impact_analysis`, `fraud_score`), safety

## 5. 하지 않는 것
- 영향 숫자(실패 주문·영향 고객·피해 금액)를 다시 계산하지 않는다 (원천은 data)
- 실제 송금·정산 확정을 하지 않는다 (정산 확정은 safety 레지스트리상 항상 C)
- 판매 사이트 DB에 직접 붙지 않는다 (shop-api·data를 거친다)
- 판매자 소개·구매자 메모 같은 `untrusted` 문장을 금액·심사 판단에 쓰지 않는다
- 기준표를 바꾸지 않는다 (사람이 safety 레포 설정을 PR로)
