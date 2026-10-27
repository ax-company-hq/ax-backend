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
| 금액 다시 계산 | `recheck.py` | 예전 `verify_amount` 계산. 사후 감사가 쓴다 |
| 사건 보상 장부 | `ledger.py` | 쿠폰 업무는 재무가 맡지 않는다(마케팅·고객지원 합의). 지급은 마케팅(`issue_coupon`), 판정은 고객지원이 하고 재무는 **빠져나간 돈**을 사건별로 적는다: 이벤트(`events`) 또는 safety 실행 기록(`safety_executions`, 이벤트를 못 받을 때)·시드(`seed`). 실행 기록이 있으면 그것이 원본이라 이벤트를 더하지 않는다. 둘 다 없으면 null |
| 사건별 화면 | `incidents.py` | 사건 목록·상세: data 영향 그대로 + 보상 장부 + 기준표 한도 + 점검 5개 (재설계 문서 12-1). 재무 단계는 #415로 없앴다 |
| 설정 | `finance_policy.toml` | 수수료율(기본 10%, 문구 8%), 주 정산, PG 3.1%, 예산, 한도 제안(한 번에 30% 안), 감사 기준 |

| skill | 등급 | 요청자 | 우선순위 | 하는 일 |
| --- | --- | --- | --- | --- |
| `seller_review` | B (검증자 data) | orchestrator | recommended | 서류 확인 + data 위험 점수로 판매자 승인 요청 (DEP-FIN-03) |
| `settlement_hold` | B (검증자 data) | orchestrator | recommended | 위험 판매자 정산 보류 요청 (DEP-FIN-04). 보류된 판매자는 정산 묶음에서 빠진다 |
| `closing_report` | read | orchestrator, data | optional | 기간 손익 요약 (`ClosingReportInput`·`ClosingReportOutput`) |
| `audit_report` | read | orchestrator, data | optional | 사후 감사 결과 (`AuditReportInput`·`AuditReportOutput`) |

없앤 것: `verify_amount`(B 쿠폰·환불 교차검증), `review_hold`(C 보류 건 AI 1차 판단), `/api/finance/verifications`, `/api/finance/ai-reviews`.
장애 흐름에서 빠지며 없앤 것(2026-10-27): `loss_aggregation`(손실 집계 skill), `/api/finance/loss-reports`, 사건 상세의 `loss`와 사건 단계(9·10·17) 계산. 재무는 장애 대응 흐름의 단계가 아니고(요구사항 DEP-FIN-01, 공통 구성안 결정 22) 장애 비용은 평상시 장부 → `closing_report`·`audit_report`에서 드러난다.
화면 호환으로 한 단계 남겨 뒀던 키(사건 상세 `steps`, 목록 `loss_total`)도 지웠다 — 화면 정리(ax-company-hq/ax-frontend#61)가 끝나 읽는 쪽이 없다.
재무 일이 아니라 없앤 것(2026-10-26): 캣북스 클레임 연동(`catbooks.py`, 환불·교환 신청을 건별로 다시 확인 → 건별 검사는 안전 관문의 일, `/api/finance/catbooks/*`, `FINANCE_CATBOOKS_*`),
`review_threshold_change`(AI가 기준 변경을 판단 → 재무는 제안만 하고 사람이 기준표 PR로 바꾼다, `/api/finance/threshold-reviews`).
`ai_review_policy.toml`(사람 전용)은 이제 코드가 읽지 않는다. 사람이 지우는 PR을 올린다.

## 2. 실행 방법
`docker compose -f infra/compose.yml up finance` / 목 모드: `FINANCE_MOCK=1` (장부 시드 `src/finance_agent/finance_books_seed.json`, 영향 분석·심사 시드 `mock_seed.json`)

혼자 띄우기(목 모드, 레포 루트): `FINANCE_MOCK=1 uv run uvicorn finance_agent.main:app --port 8105`
(정산 묶음을 실제 safety 보류 큐로 보내려면 `AX_SAFETY_URL=http://localhost:8001`. 없으면 템플릿 목 execute가 바로 `executed`를 돌려준다)

테스트: `uv run pytest agents/finance`

```bash
# 사후 감사 (목 모드, orchestrator로 요청)
echo '{"days":7}' > in.json
uv run ax-test-call --agent http://localhost:8105 --skill audit_report --input in.json \
  --context-id ctx_inc_0001
```

### 가짜 데이터로 돌려 보기 (목 모드)
safety·finance를 목 모드로 띄운 뒤 `uv run python agents/finance/demo/seed_demo.py`를 실행하면 마케팅·고객지원·통합 에이전트인 척 18건을 보낸다(공지·기록, 보상 쿠폰, 환불, 판매자 심사, 정산 보류). 결과는 대시보드 `http://localhost:3001/finance`에서 본다. 가짜 데이터는 `src/finance_agent/mock_seed.json`이다.

관리자 사용법(화면 보는 법, 결정 기준): `docs/notes/finance/admin-guide.md`

## 3. 입출력 계약
- 입출력 모델: `src/finance_agent/schemas.py` (Agent Card의 `input_schema`·`output_schema`). 금액 다시 계산 형식(`VerifyAmountInput`·`VerifyAmountOutput`)은 contracts 원본
- 재무 대시보드 API (`{data, meta}`, `meta.mock`·`collected_at`·`source`·`unavailable`, 형식은 재설계 문서 11장, 구현 `finance_api.py`):
  `GET /api/finance/overview`, `GET /api/finance/closing?period=day|month&date=`, `POST /api/finance/closing/confirm`(사람 토큰),
  `GET /api/finance/reconciliation?date=`, `POST /api/finance/reconciliation/{payment_id}/note`(사람 토큰),
  `GET /api/finance/settlements?period=YYYY-Www`, `POST /api/finance/settlements/batch`(토큰 선택, safety 없으면 503 `safety_unavailable`),
  `GET /api/finance/policy`, `GET /api/finance/audit?days=7&context_id=`(사건 선택)
- 사건별 API (시나리오 A, 재설계 문서 12-1, `incidents.py`): `GET /api/finance/incidents`(`meta.ledger_source`), `GET /api/finance/incidents/{context_id}`(없으면 404 `not_found`). 목 시드: `ctx_inc_scn_a_0001`(data 목과 같은 6건·4명·95,000원 + 보상 2,000원 × 4장), `ctx_inc_0100`(영향 밖 `buy_0999` 보상 → 감사 `compensation_outside_impact`)
- 장애 구간(`window`)은 총괄 사건 목록 `GET /api/incidents`(8000)의 `opened_at`·`closed_at`을 **읽기 전용으로** 읽어서 안다 (계약 `contracts/src/ax_contracts/data_access/orchestrator_incidents.py`, [협업] #417 제안 A). 사건 **중**에 총괄을 부르는 것이 아니라 평상시 사후 감사·사건 조회에서만 읽는다. 주소는 `AX_ORCHESTRATOR_URL`(기본 `http://orchestrator:8000`). 못 읽거나 계약과 다르거나 아직 열린 사건이면 구간·영향 모두 `null`이고 data에 묻지 않는다(fail-closed). 목 모드는 장부 시드로 구간을 안다
- 기존 조회 API: `/api/finance/ledger`(사건별 보상 지급·주문별 환불 누적), `/api/finance/market-ops`(판매자 심사·정산 보류 결과), `/api/finance/self-test`(재무 AI 테스트 버튼: 가짜 자료로 계산 규칙 점검 + 안전 장치·쇼핑몰·데이터 AI 연결 확인, 상태를 바꾸지 않음), `/api/finance/self-test/last`(마지막 결과, `data.trigger`=`auto`|`manual`, `meta.interval_seconds`·`meta.next_run_at`)
- 재무 AI 테스트 자동 실행: 서버가 켜지고 5초 뒤 한 번, 그다음 `FINANCE_SELF_TEST_SECONDS`(기본 600초, 0이면 끔)마다 서버 안에서 돌려 `/self-test/last`에 남긴다. 테스트(`tests/conftest.py`)는 0으로 끈다
- 매일 결산 자동 실행(①): 서버가 켜지고 10초 뒤 한 번, 그다음 `FINANCE_DAILY_CLOSING_SECONDS`(기본 86400초=하루, 0이면 끔)마다 **전날** 하루 결산을 계산해 `GET /api/finance/closing/last`에 남긴다(`data.date`·`data.report`·`data.trigger`=`auto`|`manual`·`data.ran_at`, `meta.interval_seconds`·`meta.next_run_at`). 조회만 한다 — 확정(`closing/confirm`)은 사람이 대시보드에서 누른다. 테스트는 0으로 끈다
- 사람 토큰: 안전 관문과 같은 형식 `Authorization: Bearer human:<이름>`(목 모드) 또는 `FINANCE_HUMAN_TOKENS`/`SAFETY_HUMAN_TOKENS`의 토큰 (`human.py`)
- CORS: `FINANCE_CORS_ORIGINS`(기본 `http://localhost:3001,http://127.0.0.1:3001`). 사람 결정 경로 3개만 POST·Authorization 허용, 나머지는 GET만
- 구독 이벤트: `coupon.issued`(보상 쿠폰 지급 누적), `payment.refunded`(환불 누적). 실제 모드는 `REDIS_URL`이 있어야 받는다. 받지 못하면 보상 쿠폰 장부를 safety `GET /api/safety/executions`(읽기)로 채운다
- 부르는 것: data `impact_analysis`(`failed_order_count`·`affected_customer_count`·`damage_amount` + 영향 고객 ID `affected_customer_ids`), data `fraud_score`(`risk_score` 0~100), safety `execute()`(판매자 승인·정산 보류·정산 묶음 `confirm_settlement`), shop-api·safety 조회 API(장부)

## 4. 의존
ax-contracts, ax-agent-template, data 에이전트(`impact_analysis`, `fraud_score`), safety

## 5. 하지 않는 것
- 영향 숫자(실패 주문·영향 고객·피해 금액)를 다시 계산하지 않는다 (원천은 data)
- 실제 송금·정산 확정을 하지 않는다 (정산 확정은 safety 레지스트리상 항상 C)
- 판매 사이트 DB에 직접 붙지 않는다 (shop-api·data를 거친다)
- 판매자 소개·구매자 메모 같은 `untrusted` 문장을 금액·심사 판단에 쓰지 않는다
- 기준표를 바꾸지 않는다 (사람이 safety 레포 설정을 PR로)
