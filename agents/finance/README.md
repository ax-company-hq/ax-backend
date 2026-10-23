# agents/finance
담당: 태윤 · 포트: 8105
## 1. 역할
**캣북스 재무팀** + 마켓운영 에이전트 (재무 재설계 2026-10-23, `docs/notes/finance/2026-10-23-finance-redesign.md`).
결산·리포트, 매출·PG 대사, 판매자 정산, 예산·한도 정책, 사후 감사를 한다. **건별 금액 승인은 하지 않는다**:
쿠폰·환불 건별 검사는 안전 관문 코드 규칙(금액 계산, `claimed_fields` 불일치, 기준표 한도)이 한다.
숫자는 전부 코드로 계산하고, LLM은 결산 설명 문장만 쓴다(목 모드·미설정·실패 때는 템플릿 문장).

| 모듈 | 파일 | 하는 일 |
| --- | --- | --- |
| 장부 자료 | `books.py` | 실제: shop-api 주문·상품·판매자·쿠폰 + safety 기준표·실행 기록. 목: `finance_books_seed.json`(오늘 기준 2주치). 토스 정산 형식 목 PG 정산(데모 불일치 포함) |
| ① 결산·리포트 | `closing.py` | 일·월 손익, 분개 미리보기, 설명 문장 |
| ② 매출·PG 대사 | `reconciliation.py` | `matched`·`mismatch`·`missing_in_pg`·`pending`·`fee_anomaly` |
| ③ 판매자 정산 | `settlement.py` | 정산금 = 판매액 − 수수료 − 판매자 부담 쿠폰 − 환불, 보류 사유. 묶음 지급은 `execute("confirm_settlement")`(항상 C) |
| ⑤ 예산·한도 | `policy.py` | 정책표·소진율·경보, 한도 *제안*(적용은 기준표 PR) |
| ⑦ 사후 감사 | `audit.py` | 쪼개기·반복 환불·쿠폰 중복·환불률 급변·재계산 실패·관문 보류 추이 |
| 금액 다시 계산 | `recheck.py` | 예전 `verify_amount` 계산. 캣북스 확인과 사후 감사가 쓴다 |
| 설정 | `finance_policy.toml` | 수수료율(기본 10%, 문구 8%), 주 정산, PG 3.1%, 예산, 감사 기준 |

| skill | 등급 | 요청자 | 우선순위 | 하는 일 |
| --- | --- | --- | --- | --- |
| `loss_aggregation` | read | orchestrator | required | data 영향 분석 + 같은 사건의 보상 쿠폰 지급 합계 = 손실 리포트 (DEP-FIN-01) |
| `seller_review` | B (검증자 data) | orchestrator | recommended | 서류 확인 + data 위험 점수로 판매자 승인 요청 (DEP-FIN-03) |
| `settlement_hold` | B (검증자 data) | orchestrator | recommended | 위험 판매자 정산 보류 요청 (DEP-FIN-04). 보류된 판매자는 정산 묶음에서 빠진다 |
| `closing_report` | read | orchestrator, data | optional | 기간 손익 요약 (`ClosingReportInput`·`ClosingReportOutput`) |
| `audit_report` | read | orchestrator, data | optional | 사후 감사 결과 (`AuditReportInput`·`AuditReportOutput`) |
| `review_threshold_change` | read | safety | recommended | 기준 바꾸기 판단. safety가 안전 장치(원래 값의 0.5~2배 등)를 먼저 검사한 요청만 온다 |

없앤 것: `verify_amount`(B 쿠폰·환불 교차검증), `review_hold`(C 보류 건 AI 1차 판단), `/api/finance/verifications`, `/api/finance/ai-reviews`.
`ai_review_policy.toml`(사람 전용)은 그대로이고 `[threshold_change]`만 쓴다.

`review_threshold_change` 판단 규칙 (`threshold_review.py`, 값은 `ai_review_policy.toml`의 `[threshold_change]`, 사람만 변경):
- 입력 `{change_id, key, current_value, new_value, baseline, stats}`. `stats`(`holds_24h`·`executed_today`·`ai_approved_24h`·`human_approved_24h`·`expired_24h`)는 safety가 코드로 센 값
- 변경 폭 `|new − current| / current > max_step_ratio(0.3)` → `escalate`(`big_step`)
- 낮추기 → `approve`(`tighten`), `review_timeout_seconds`는 폭 안이면 `approve`
- 올리기 → `holds_24h ≥ min_holds_to_raise(3)`이면 `approve`(`raise_with_holds`), 아니면 `escalate`(`insufficient_evidence`, "근거 부족")
- 값이 잘못됐거나 지금 값과 같으면 `reject`. 요청 이유는 `untrusted.reason_untrusted_text`로 오며 읽지 않는다. 같은 `change_id`는 같은 판단
- 출력 `{decision: approve|reject|escalate, reason_code, explanation}` (설명은 쉬운 한국어, 숫자는 코드 계산)

## 2. 실행 방법
`docker compose -f infra/compose.yml up finance` / 목 모드: `FINANCE_MOCK=1` (장부 시드 `src/finance_agent/finance_books_seed.json`, 손실 리포트·심사 시드 `mock_seed.json`)

혼자 띄우기(목 모드, 레포 루트): `FINANCE_MOCK=1 FINANCE_CATBOOKS_POLL_SECONDS=0 uv run uvicorn finance_agent.main:app --port 8105`
(정산 묶음을 실제 safety 보류 큐로 보내려면 `AX_SAFETY_URL=http://localhost:8001`. 없으면 템플릿 목 execute가 바로 `executed`를 돌려준다)

테스트: `uv run pytest agents/finance`

```bash
# 손실 리포트 (목 모드, orchestrator로 요청)
echo '{"window_from":"2026-10-22T05:00:00Z","window_to":"2026-10-22T05:40:00Z"}' > in.json
uv run ax-test-call --agent http://localhost:8105 --skill loss_aggregation --input in.json \
  --context-id ctx_inc_0001
```

### 가짜 데이터로 돌려 보기 (목 모드)
safety·finance를 목 모드로 띄운 뒤 `uv run python agents/finance/demo/seed_demo.py`를 실행하면 마케팅·고객지원·통합 에이전트인 척 18건을 보낸다(공지·기록, 보상 쿠폰, 환불, 손실 리포트, 판매자 심사, 정산 보류). 결과는 대시보드 `http://localhost:3001/finance`에서 본다. 가짜 데이터는 `src/finance_agent/mock_seed.json`이다.

관리자 사용법(화면 보는 법, 결정 기준): `docs/notes/finance/admin-guide.md`

## 3. 입출력 계약
- 입출력 모델: `src/finance_agent/schemas.py` (Agent Card의 `input_schema`·`output_schema`). 금액 다시 계산 형식(`VerifyAmountInput`·`VerifyAmountOutput`)은 contracts 원본
- 재무 대시보드 API (`{data, meta}`, `meta.mock`·`collected_at`·`source`·`unavailable`, 형식은 재설계 문서 11장, 구현 `finance_api.py`):
  `GET /api/finance/overview`, `GET /api/finance/closing?period=day|month&date=`, `POST /api/finance/closing/confirm`(사람 토큰),
  `GET /api/finance/reconciliation?date=`, `POST /api/finance/reconciliation/{payment_id}/note`(사람 토큰),
  `GET /api/finance/settlements?period=YYYY-Www`, `POST /api/finance/settlements/batch`(토큰 선택, safety 없으면 503 `safety_unavailable`),
  `GET /api/finance/policy`, `GET /api/finance/audit?days=7`
- 기존 조회 API: `/api/finance/ledger`(사건별 보상 지급·주문별 환불 누적), `/api/finance/market-ops`(판매자 심사·정산 보류 결과), `/api/finance/loss-reports`, `/api/finance/threshold-reviews`, `/api/finance/self-test`(재무 AI 테스트 버튼: 가짜 자료로 계산 규칙 점검 + 안전 장치·쇼핑몰·데이터 AI·캣북스 연결 확인, 상태를 바꾸지 않음), `/api/finance/self-test/last`, `/api/finance/catbooks/sync`, `/api/finance/catbooks/items`
- 사람 토큰: 안전 관문과 같은 형식 `Authorization: Bearer human:<이름>`(목 모드) 또는 `FINANCE_HUMAN_TOKENS`/`SAFETY_HUMAN_TOKENS`의 토큰 (`human.py`)
- CORS: `FINANCE_CORS_ORIGINS`(기본 `http://localhost:3001,http://127.0.0.1:3001`). 사람 결정 경로 3개만 POST·Authorization 허용, 나머지는 GET만
- 캣북스 연결(`src/finance_agent/catbooks.py`): 캣북스 관리자 API `GET /api/admin/claims`·`/api/admin/claims/{id}`·`/api/admin/queue`를 읽어 환불 금액을 확인한다. 서버가 도는 동안 `FINANCE_CATBOOKS_POLL_SECONDS`(기본 45)초마다 가져온다. 주소는 `FINANCE_CATBOOKS_URL`(기본 `http://host.docker.internal:8030`)
- 구독 이벤트: `coupon.issued`(보상 쿠폰 지급 누적), `payment.refunded`(환불 누적)
- 부르는 것: data `impact_analysis`(`failed_order_count`·`affected_customer_count`·`damage_amount` + 영향 고객 ID `affected_customer_ids`), data `fraud_score`(`risk_score` 0~100), safety `execute()`(판매자 승인·정산 보류·정산 묶음 `confirm_settlement`), shop-api·safety 조회 API(장부)

## 4. 의존
ax-contracts, ax-agent-template, data 에이전트(`impact_analysis`, `fraud_score`), safety

## 5. 하지 않는 것
- 영향 숫자(실패 주문·영향 고객·피해 금액)를 다시 계산하지 않는다 (원천은 data)
- 실제 송금·정산 확정을 하지 않는다 (정산 확정은 safety 레지스트리상 항상 C)
- 판매 사이트 DB에 직접 붙지 않는다 (shop-api·data를 거친다)
- 판매자 소개·구매자 메모 같은 `untrusted` 문장을 금액·심사 판단에 쓰지 않는다
- 기준표를 바꾸지 않는다 (사람이 safety 레포 설정을 PR로)
