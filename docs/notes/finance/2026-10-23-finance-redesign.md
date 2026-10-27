# 재무 AI 재설계: 캣북스 재무팀 (2026-10-23)

담당: 태윤 (finance·safety·contracts). 상태: **확정 (태윤 결정, 2026-10-23)**. 기준 문서 개정안은 `docs-change-finance-redesign.md`.

## 1. 한 줄 요약

재무 AI는 **캣북스의 재무팀**이다. 실제 회사 재무팀이 하는 일(결산, 대사, 판매자 정산, 예산·한도 정책, 사후 감사)을
시중 재무 솔루션을 벤치마킹해 캣북스에 제공한다. **건별 금액 승인(예: 10만 원 환불 하나를 재무가 승인)은 재무의 일이 아니다.**
건별 검사는 안전 관문의 코드 규칙이 하고, 재무는 쿠폰·환불을 장부에 기록하고, 그 규칙이 쓰는 한도의 변경을 *제안*하고, 나중에 전수로 점검한다. 한도를 정하는 것은 사람(기준표 PR)이고 보상 단가·건수 상한은 고객지원 정책표다 (2026-10-27 문구 정리).

## 2. 원칙

1. **자동 수집 → 규칙으로 맞추고 분류 → 예외만 사람에게 → 결산·리포트** (Stripe·BlackLine·포트원·Ramp 공통 패턴).
2. **정책(한도)은 사람이 정하고, 집행은 시스템이 한다.** 한도 값은 사람만 바꾸는 기준표(`thresholds.toml`, #202)에 있고,
   재무 AI는 바꿀 값을 *제안*만 한다. 안전 관문이 그 값으로 건별 검사를 한다.
3. **숫자는 코드가 계산한다.** LLM은 리포트의 설명 문장만 쓴다. 목 모드·LLM 실패 때는 코드 템플릿 문장으로 대신한다.
4. **돈을 실제로 움직이지 않는다.** 판매자 정산 지급은 `confirm_settlement`(C, 사람 승인)로만, 그것도 데모에서는 기록만 한다.
5. **원천 데이터는 API로만 읽는다.** 판매 사이트 DB에 붙지 않는다(CLAUDE.md 4번). 실제 모드는 shop-api, 목 모드는 패키지 시드.

## 3. 모듈 (1차 범위 5개)

| 모듈 | 재무 AI가 하는 일 | 사람이 하는 일 | 참고 |
|---|---|---|---|
| ① 결산·리포트 | 일·월 손익 요약(총거래액 GMV, 수수료 매출, 쿠폰 비용, 환불, PG 수수료, 영업 결과), 자동 분개 미리보기, 쉬운 말 설명. 장애 때 나간 보상 쿠폰도 쿠폰 비용에 들어감(장애 손실 리포트 `loss_aggregation`은 2026-10-27 장애 흐름에서 뺌, 12장) | 결산 확정 | Stripe RevRec, BlackLine |
| ② 매출·PG 대사 | 주문 ↔ 결제·환불 ↔ PG 정산을 건별로 맞춤. 상태 `matched`(일치)·`mismatch`(금액 다름)·`missing_in_pg`(PG에 없음)·`pending`(아직 정산 전)·`fee_anomaly`(수수료율 이상) | 불일치 건 처리 메모 | 포트원 거래대사, 토스 정산 API |
| ③ 판매자 정산 | 판매자별 정산금 = 판매액 − 판매 수수료 − 판매자 부담 쿠폰 − 환불. 보류 사유 자동 표시(진행 중 클레임, 구매 미확정, 위험 판매자). 정산 내역서 | 정산 묶음 지급 승인(C, 안전 관문 보류 큐) | 포트원 파트너 정산, 쿠팡·스마트스토어 |
| ⑤ 예산·한도 정책 | 정책표 보기(쿠폰 한도, 환불 한도, 보상 한도, 판매자 위험), 오늘·이번 달 소진율과 경보, 데이터 근거로 한도 변경 *제안* | 정책 변경(기준표 PR) | Ramp, Pigment |
| ⑦ 내부통제·이상 탐지 | 사후 감사 리포트: 한도 바로 아래로 쪼개기, 같은 고객 반복 환불, 같은 고객 쿠폰 중복, 환불률 급변, 실행 관문 보류·거절 추이 | 조사 결론 메모 | 비즈플레이 모니터링, Ramp |

나중에: ④ 비용·영수증(영수증 읽기, AI 사용비), ⑥ 세무(부가세·세금계산서), ⑧ 현금흐름 예측.

## 4. 없애는 것 / 바뀌는 것

| 대상 | 지금 | 바뀜 |
|---|---|---|
| `verify_amount` (재무 skill) | 쿠폰·환불 B 건마다 안전 관문이 재무에 재계산 요청 | **삭제.** 건별 검사는 안전 관문 코드 규칙(금액 계산·`claimed_fields` 불일치·기준표 한도) |
| `issue_coupon`, `refund_request` (registry) | B, verifier finance | **A**, verifier 없음. 금액이 기준표 기준 이상이면 C. 요청 총액이 계산값과 다를 때의 C(`amount_mismatch`)는 `claimed_fields`가 있는 `issue_coupon`에만 걸린다(`refund_request`는 금액 필드 하나라 해당 없음) |
| `review_hold` (재무 skill, #110) | C 보류 건 AI 1차 판단 | **삭제.** #110·#266은 닫는다. ⑦ 사후 감사로 대신 |
| `review_threshold_change` (#124·#276) | AI가 기준 변경 판단 | **삭제 (2026-10-26).** 재무는 한도를 제안만 하고 사람이 기준표 PR로 바꾼다. 제안 폭은 `finance_policy.toml` `[proposal] max_step_ratio` |
| `loss_aggregation` | 손실 리포트 | ~~유지~~ → **장애 흐름에서 뺌 (2026-10-27, 12장).** 총괄이 사건 중에 부르지 않는다. skill·API 정리는 후속 코드 PR |
| `seller_review`, `settlement_hold` | 판매자 심사, 정산 보류 | **유지**(③의 일부). 보류된 판매자는 정산 묶음에서 빠진다 |
| 캣북스 연동(#146) | 환불·교환 신청 읽기 | **삭제 (2026-10-26).** 신청을 건별로 다시 확인하는 일이라 재무 일이 아니다(건별 검사는 안전 관문) |
| 장부(`coupon.issued`, `payment.refunded` 구독) | 쿠폰·환불 합계 | 유지. ①·⑤의 자료 |

## 5. 데이터

| 자료 | 실제 모드 | 목 모드 |
|---|---|---|
| 주문·결제·환불 | shop-api `GET /api/shop/orders` (`items[].seller_id`, `payments[]`, `refunded_amount`, `shipping_fee`, `points_used`) | `finance_books_seed.json` (2주치 주문, 판매자 3곳) |
| 쿠폰 | shop-api `GET /api/shop/coupons` + 장부 이벤트 | 시드 |
| 판매자 | shop-api `GET /api/shop/sellers` | 시드(`sel_0001` 책마루, `sel_0002` 종이나라, `sel_0003` 북앤펜) |
| PG 정산 | 실제 PG 없음 → **토스 정산 형식 목**(`amount`, `fee`, `pay_out_amount`, `sold_date`, `paid_out_date`)을 결제에서 만든다. 데모용 불일치 몇 건을 일부러 넣는다 | 같음 |
| 정책(한도) | 안전 관문 `GET /api/safety/thresholds` | 같은 기준표 파일 기본값 |
| 판매 수수료율·정산 주기 | 재무 설정 `finance_policy.toml`(기본 수수료 10%, 문구 8%, 주 정산) | 같음 |
| 실행 관문 기록 | 안전 관문 `GET /api/safety/executions` | 시드 |

못 받은 값은 "확인할 수 없음"이고 추정값을 만들지 않는다(fail-closed).

## 6. 재무 API (대시보드용, 읽기 + 사람 결정)

모두 `{data, meta}` 봉투. `meta.mock`, `meta.collected_at`.

- `GET /api/finance/overview` → 오늘 요약: `gmv`, `net_revenue`, `refund_total`, `coupon_cost`, `recon_exceptions`, `settlement_pending`, `audit_findings`, `policy_alerts`, `todo[]`(사람이 할 일)
- `GET /api/finance/closing?period=day|month&date=YYYY-MM-DD` → `ClosingReport`: 기간, 손익 줄(`lines[]` = {code, label, amount}), 분개 미리보기(`journal[]` = {account, debit, credit, memo}), `commentary`(설명 문장), `status`(`draft`|`confirmed`), `confirmed_by`
- `POST /api/finance/closing/confirm` (사람 토큰) `{period, date}` → 확정 기록
- `GET /api/finance/reconciliation?date=` → `ReconciliationResult`: `counts`(상태별), `items[]` = {order_id, payment_id, order_amount, pg_amount, fee, expected_fee, status, note}
- `POST /api/finance/reconciliation/{payment_id}/note` (사람 토큰) `{note}`
- `GET /api/finance/settlements?period=` → 판매자별 `SellerStatement[]` = {seller_id, name, gross, commission_rate, commission, seller_coupon, refunds, payout, hold_reasons[], status(`ready`|`held`|`requested`|`approved`|`rejected`)}
- `POST /api/finance/settlements/batch` → 보류 아닌 판매자들을 묶어 `confirm_settlement`를 `execute()`로 요청(C → 안전 관문 보류 큐). 응답에 `review_id`
- `GET /api/finance/policy` → 정책표(`rules[]` = {key, label, limit, used_today, used_month, ratio, alert}), 한도 *제안* `proposals[]` = {key, current, proposed, reason}
- `GET /api/finance/audit?days=7` → `AuditReport`: `findings[]` = {code, severity, title, detail, refs[]}, `stats`(환불 건수·금액, 환불률 지난주 대비, 쿠폰 건수, 관문 보류·거절 수)
- 유지: `GET /api/finance/ledger`, `/loss-reports`, `/self-test`, `/self-test/last`, `/market-ops`
- 삭제: `/api/finance/verifications`, `/api/finance/ai-reviews`, `/catbooks/sync`·`/catbooks/items`·`/threshold-reviews`(2026-10-26, 4장)

사람 토큰: 안전 관문과 같은 형식(`Authorization: Bearer human:<이름>`). 재무 화면은 `VITE_SAFETY_HUMAN_TOKEN`을 쓴다.

## 7. A2A skill (Agent Card)

| skill | 등급 | 요청 가능 | 비고 |
|---|---|---|---|
| `loss_aggregation` | read | orchestrator | 장애 흐름에서 뺌 (2026-10-27, 12장). 정리 후속 |
| `seller_review` | B | orchestrator | 유지 |
| `settlement_hold` | B | orchestrator | 유지 |
| `closing_report` | read | orchestrator, data | 새로. 기간 손익 요약 |
| `audit_report` | read | orchestrator, data | 새로. 사후 감사 결과 |
| ~~`verify_amount`~~, ~~`review_hold`~~ | | | 삭제 |
| ~~`review_threshold_change`~~ | | | 삭제 (2026-10-26): AI가 기준 변경을 결정하면 "재무는 제안만"과 충돌 |

## 8. 대시보드 재무 화면 (`/finance`)

위에서 아래로:
1. 머리말 + 재무 AI 테스트 버튼(유지) + "처음이라면: 관리자가 할 일"(새 흐름으로 고침)
2. **지금 사람이 할 일**: 정산 묶음 승인 대기, 결산 확정 대기, 대사 불일치, 감사 의심 건
3. **오늘의 재무** 카드: 총거래액, 순매출(수수료), 환불, 쿠폰 비용, PG 수수료
4. 탭 또는 섹션 5개: ① 결산·리포트 ② 매출·PG 대사 ③ 판매자 정산 ⑤ 예산·한도 정책 ⑦ 내부통제·감사
5. 장애 손실 리포트: 장애 흐름에서 뺌(2026-10-27, 12장), 화면 정리 후속. 캣북스에서 들어온 일은 삭제(2026-10-26, 4장)

없애는 화면: 거래별 검증 목록, AI가 결정한 건 목록, 네 갈래 처리 흐름 막대(건별 승인 중심이라).

## 9. 영향받는 다른 부서

- 마케팅·고객지원: 쿠폰·환불이 B → A. 한도 안이면 바로 실행되고, 넘으면 지금처럼 C 보류. 응답 상태 `verifying`이 더 이상 오지 않는다.
- 총괄: 장애 중에 재무를 부르지 않는다 (2026-10-27, 12장). 플랜의 `loss_aggregation`·`audit_report` 노드 제거는 후속. `closing_report`·`audit_report`는 평상시 조회용 선택.
- 대시보드(병환): 재무 화면은 재무가 고친다. 보류 큐에 `confirm_settlement`가 나타난다.

## 10. 테스트

- 재무: 모듈별 단위 테스트(계산식, 대사 상태 분류, 정산 공식, 소진율, 감사 규칙), API 테스트, 실제 모드 원천 호출 형식 테스트(계약·응답 형식)
- 안전 관문: 쿠폰·환불 A·executed(한도 안), C(한도 초과·금액 불일치), finance 교차검증 호출이 없음
- 화면: 타입 검사·빌드, 브라우저로 5개 섹션과 정산 묶음 요청 확인

## 11. API 응답 형식 (백엔드·화면 공통 기준)

금액은 정수 원, 시각은 UTC ISO8601. 모든 응답 `{"data": ..., "meta": {"mock": bool, "collected_at": str, "source": "shop-api"|"seed"}}`.
값을 못 받으면 해당 필드는 `null`이고 `meta.unavailable: [필드 이름...]`에 적는다.

```jsonc
// GET /api/finance/overview
{"date": "2026-10-23",
 "kpis": {"gmv": 1250000, "commission_revenue": 118000, "refund_total": 64000, "coupon_cost": 42000, "pg_fee": 38750, "net_result": 37250},
 "todo": [ {"kind": "settlement_batch"|"closing_confirm"|"recon_exception"|"audit_finding"|"policy_alert",
            "title": "판매자 정산 3곳 지급 승인 대기", "count": 3, "amount": 980000, "link": "settlements"} ]}

// GET /api/finance/closing?period=day|month&date=YYYY-MM-DD   (date 생략 = 오늘, month면 그 달)
{"period": "day", "date": "2026-10-23", "from": "...", "to": "...",
 "lines": [ {"code": "gmv", "label": "총거래액", "amount": 1250000},
            {"code": "commission_revenue", "label": "판매 수수료 매출", "amount": 118000},
            {"code": "coupon_cost", "label": "쿠폰 비용(회사 부담)", "amount": -42000},
            {"code": "refund_total", "label": "환불", "amount": -64000},
            {"code": "pg_fee", "label": "PG 수수료", "amount": -38750},
            {"code": "net_result", "label": "영업 결과", "amount": 37250} ],
 "journal": [ {"account": "외상매출금(PG)", "debit": 1250000, "credit": 0, "memo": "카드 결제 매출"} ],
 "commentary": "오늘 총거래액은 125만 원으로 ...", "commentary_source": "llm"|"template",
 "status": "draft"|"confirmed", "confirmed_by": null, "confirmed_at": null}

// POST /api/finance/closing/confirm   body {"period": "day", "date": "2026-10-23"}   (Authorization: Bearer human:<이름>)
// → 위 ClosingReport (status=confirmed). 사람 토큰 없으면 401 {"error": {...}}

// GET /api/finance/reconciliation?date=YYYY-MM-DD   (생략 = 최근 7일)
{"from": "...", "to": "...",
 "counts": {"matched": 41, "mismatch": 2, "missing_in_pg": 1, "pending": 5, "fee_anomaly": 1},
 "totals": {"order_amount": 2300000, "pg_amount": 2280000, "pg_fee": 71300, "pay_out": 2208700},
 "items": [ {"order_id": "ord_...", "payment_id": "pay_...", "paid_at": "...", "order_amount": 32000, "pg_amount": 30000,
             "fee": 960, "expected_fee": 928, "pay_out_amount": 29040, "paid_out_date": "2026-10-25"|null,
             "status": "mismatch", "reason": "PG 금액이 주문보다 2,000원 적음", "note": null} ]}
// items는 matched를 포함한 전체. 화면은 기본으로 matched 이외만 보여 준다.

// POST /api/finance/reconciliation/{payment_id}/note   body {"note": "PG사에 문의함"}  (사람 토큰) → 바뀐 item

// GET /api/finance/settlements?period=YYYY-Www   (생략 = 지난주, 월~일)
{"period": "2026-W42", "from": "...", "to": "...", "pay_date": "2026-10-24",
 "statements": [ {"seller_id": "sel_0001", "name": "책마루", "orders": 18, "gross": 540000, "commission_rate": 0.10,
                  "commission": 54000, "seller_coupon": 0, "refunds": 32000, "payout": 454000,
                  "hold_reasons": [ {"code": "open_claim", "label": "처리 중인 환불·교환 1건"} ],
                  "status": "ready"|"held"|"requested"|"approved"|"rejected", "review_id": null} ],
 "batch": {"status": "none"|"requested"|"approved"|"rejected", "review_id": null, "execution_id": null, "total_payout": 0, "seller_ids": []}}

// POST /api/finance/settlements/batch   body {"period": "2026-W42"}
// → 보류 없는(ready) 판매자만 묶어 execute(confirm_settlement) 요청. safety C 보류 → batch.status="requested", review_id 채움.
//   safety가 꺼져 있으면 503 {"error": {"code": "safety_unavailable", ...}} (fail-closed, 지급 기록 없음)

// GET /api/finance/policy
{"rules": [ {"key": "compensation_coupon_per_customer", "label": "보상 쿠폰 1인 한도", "limit": 10000, "unit": "원",
             "used_today": null, "used_month": null, "ratio": null, "alert": "ok"|"warn"|"over"|null, "source": "thresholds.toml"},
            {"key": "coupon_budget_month", "label": "이번 달 쿠폰 예산", "limit": 3000000, "unit": "원",
             "used_today": 42000, "used_month": 1240000, "ratio": 0.41, "alert": "ok"} ],
 "proposals": [ {"key": "refund_auto_limit", "current": 100000, "proposed": 80000, "reason": "최근 2주 환불 95%가 8만 원 이하",
                 "evidence": {"sample": 120, "p95": 78000}} ],
 "how_to_change": "기준표(safety/.../thresholds.toml) PR로 사람이 바꿉니다."}

// GET /api/finance/audit?days=7
{"from": "...", "to": "...",
 "stats": {"refund_count": 12, "refund_amount": 384000, "refund_rate": 0.052, "refund_rate_prev": 0.031,
           "coupon_count": 40, "coupon_amount": 210000, "gate_held": 3, "gate_rejected": 1},
 "findings": [ {"code": "split_under_limit"|"repeat_refund"|"duplicate_coupon"|"refund_rate_jump"|"amount_recheck_failed"|"gate_hold_trend",
                "severity": "high"|"medium"|"low", "title": "같은 고객에게 한도 바로 아래 쿠폰 3번",
                "detail": "buyer_0007에게 1시간 안에 9,900원 쿠폰 3장 (한도 10,000원)", "refs": ["cpn_...", "exe_..."]} ]}
```

## 12. 시나리오 A(결제 장애)와 재무 (2026-10-26 추가, 2026-10-27 개정)

**2026-10-27 개정 (멘토 피드백 2026-10-26):** 재무는 서버 장애 대응 흐름의 노드가 아니다. 이전 판의 ⑨ 쿠폰 장부·⑩ 손실 리포트·⑰ 사건 후 감사 단계는 시나리오에서 지웠다 (`docs/scenarios/scn-a-server-incident.md`, 공통 구성안 후속 결정 22).

| 이전 단계 | 지금 |
|---|---|
| ⑨ 쿠폰 장부 | 사건 단계가 아니라 **평상시 장부**. 지급된 쿠폰(`coupon.issued`)·환불(`payment.refunded`)이 이벤트로 장부에 쌓인다. 실제 모드는 `REDIS_URL`이 있어야 받고(agent-template #386), 못 받으면 안전 관문 실행 기록(`GET /api/safety/executions`)으로 채운다(읽기 API, 규칙 4) |
| ⑩ 손실 리포트 | **없앰.** 총괄이 사건 중에 `loss_aggregation`을 부르지 않는다. 장애 비용은 결산(`closing_report`)의 쿠폰 비용 줄에 들어간다 |
| ⑰ 사건 후 감사 | 사건 단계가 아니라 **정기 사후 감사**(`audit_report`). `context_id`로 사건만 골라 볼 수 있다. 총괄이 사건 끝에 요청하지 않는다 |

재무가 하지 않는 일(이 시나리오): 사건 단계 전부, 쿠폰 건별 승인(안전 관문 A/C), 보상 대상·단가 판정(고객지원 정책표), 쿠폰 지급(마케팅), 금액 기준 결정(사람, `thresholds.toml`).

코드 정리(후속, 이 문서 개정과 별도 PR): 재무 `incidents.py`(`/api/finance/incidents`의 `steps` 9·10·17, `loss`)·`loss_aggregation`·`/loss-reports`, 총괄 `plans/scenario_a.py`의 `loss`·`audit` 노드, 대시보드 재무 화면의 장애 사건 탭.

### 12-1. API (코드 정리 전까지 남아 있는 형식, 2026-10-26)

```jsonc
// GET /api/finance/incidents  → 재무가 아는 사건 목록 (장부·손실 리포트·안전 관문 기록에 나온 ctx_inc_)
{"data": [ {"context_id": "ctx_inc_…", "component": "payment"|null, "opened_at": "…"|null,
            "damage_amount": 95000|null, "compensation_total": 8000, "coupon_count": 4, "held_count": 0,
            "loss_total": 103000|null, "checks_ok": true|false|null} ],
 "meta": {"mock": bool, "ledger_source": "events"|"safety_executions"|"seed"}}

// GET /api/finance/incidents/{context_id}
{"data": {
  "context_id": "ctx_inc_…", "component": "payment"|null, "window": {"from": "…", "to": "…"}|null,
  "impact": {"failed_orders": 6, "affected_customers": 4, "damage_amount": 95000,
             "by_category": [{"category": "books", "orders": 4, "amount": 82500}, {"category": "stationery", "orders": 2, "amount": 12500}],
             "affected_customer_ids": ["buy_0001", "buy_0002", "buy_0003", "buy_0004"],
             "source": "data.impact_analysis", "collected_at": "…"} | null,
  "compensation": {
    "coupons": [ {"buyer_id": "buy_0001", "execution_id": "exe_…", "coupon_id": "cpn_…"|null, "count": 1, "unit_value": 2000, "amount": 2000,
                  "purpose_code": "compensation", "reason_code": "payment_failure"|null, "grade": "A"|"C",
                  "status": "executed"|"held"|"rejected"|"failed", "review_id": null, "at": "…", "in_impact": true|false|null} ],
    "total_executed": 8000, "total_held": 0, "count_executed": 4, "count_held": 0},
  "limits": {"per_customer": {"limit": 50000, "max_used": 2000, "buyer_id": "buy_0001"},
             "per_incident": {"limit": 1000000, "used": 8000, "remaining": 992000}},
  "loss": {"damage_amount": 95000, "compensation_total": 8000, "total": 103000, "status": "complete"|"partial", "missing": []},
  "checks": [ {"code": "recipients_in_impact"|"one_coupon_per_buyer"|"per_customer_limit"|"per_incident_limit"|"amount_recheck",
               "ok": true|false|null, "title": "…", "detail": "…", "refs": ["exe_…"]} ],
  "steps": [ {"no": "9", "title": "쿠폰 장부 기록", "status": "done"|"waiting"|"unavailable", "detail": "…"},
             {"no": "10", "title": "손실 리포트", "status": "…", "detail": "…"},
             {"no": "17", "title": "사건 후 감사", "status": "…", "detail": "…"} ],
  "ledger_source": "events"|"safety_executions"|"seed"},
 "meta": {"mock": bool, "unavailable": []}}
// 없는 사건: 404 {"error": {"code": "not_found", …}}
```

`audit_report`에 finding `compensation_outside_impact`(high)를 더한다: 사건 보상 쿠폰을 받은 구매자가 그 사건의 데이터 영향 고객 목록에 없으면.
목 시드: 사건 1건이 데이터 목과 같은 숫자(6건·4명 `buy_0001~0004`·95,000원, 도서 4건 82,500원·문구 2건 12,500원)이고 보상 쿠폰 4장 × 2,000원(`payment_failure`, A, executed). 감사 시연용으로 지난 사건 1건에 영향 고객이 아닌 구매자 쿠폰 1장을 둔다.
