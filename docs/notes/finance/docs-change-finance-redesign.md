# 기준 문서 개정안: 재무 AI 재설계 (2026-10-23)

담당: 태윤. 상태: **초안** (docs PR 전). 근거 설계: [2026-10-23-finance-redesign.md](2026-10-23-finance-redesign.md)

- 2026-10-26: A-5·A-6(SCN-A 인수 기준)만 먼저 docs PR #393으로 열었다 (A-6의 "한도 초과"는 "기준 이상"으로 맞춤). 나머지 항목은 아직 docs PR 전.
- 2026-10-27: 멘토 피드백으로 재무를 시나리오 A 흐름에서 뺐다 (docs PR `docs/scn-a-remove-finance`). 그래서 아래 B-7의 "시나리오 A 필수 = 손실 리포트"와 A-x의 "DEP-FIN-01 유지(`loss_aggregation`)", B-10 결정 24의 "`loss_aggregation` 포함"은 더 이상 맞지 않는다. 공통 구성안 결정 22는 이 개정이 썼으므로 B-10의 결정 번호는 하나씩 밀린다.

## 목적

재무 AI 재설계(쿠폰·환불 건별 재무 교차검증 `verify_amount` 삭제 → 두 액션 A 등급, 재무는 결산·대사·판매자 정산·예산·한도 정책·사후 감사 담당)는 기준 문서 `docs/requirements.md`, `docs/agent-common.md`와 다르게 동작한다.
CLAUDE.md 11번에 따라 코드 PR보다 docs PR이 먼저다. `docs/`는 사람만 고치는 경로라, 태윤이 아래 "현재 문구 → 바꿀 문구"를 그대로 옮겨 docs PR을 연다.

- 기준 시점: 브랜치 `finance/redesign`, 커밋 `7f8fcf8`의 `docs/`. 줄 번호는 이 커밋 기준이며, docs PR을 열 때 dev 최신과 다르면 문구로 찾는다.
- 현재 문구 블록은 파일 원문 그대로다(표 안의 `\_`, `\~` 이스케이프 포함). 블록 안 내용을 그대로 찾아 바꾸면 된다.
- 표기: **[필수]** 안 고치면 기준 문서와 코드가 충돌함, **[선택]** 고치면 정확해지지만 충돌은 아님, **[확인 필요]** 설계 문서만으로 정할 수 없어 태윤 결정이 필요함.

---

## A. `docs/requirements.md`

### A-1. [필수] 6장 DEP-MKT-02 (124줄) — 쿠폰 지급 B → A

현재 문구:

```markdown
| DEP-MKT-02 | 마케팅 | 쿠폰 지급 | issue\_coupon(purpose\_code campaign·compensation): 사건 보상과 고객지원 보상 요청 처리, 보상은 직렬 + sequence, 검증자 재무(안전 레이어 라우팅), 기준 이상 C | B | 재헌 | 필수 |
```

바꿀 문구:

```markdown
| DEP-MKT-02 | 마케팅 | 쿠폰 지급 | issue\_coupon(purpose\_code campaign·compensation): 사건 보상과 고객지원 보상 요청 처리, 보상은 직렬 + sequence. 검증자 없음, 안전 레이어 코드 규칙(금액 재계산·기준표 한도)으로 판정해 한도 안이면 바로 실행, 기준 이상·금액 불일치 C | A | 재헌 | 필수 |
```

이유: 설계 4장 — `issue_coupon`은 A·verifier 없음, 한도 초과·계산 불일치는 지금처럼 C.

### A-2. [필수] 6장 DEP-FIN-02 (129줄) — 금액 교차검증 삭제

현재 문구:

```markdown
| DEP-FIN-02 | 재무/정산 | 금액 교차검증 | 안전 레이어가 라우팅한 B 액션을 원천 데이터로 재계산해 승인·거절 (verify\_amount) | 조회 | 태윤 | 필수 |
```

바꿀 문구 (안 1, 권장: 행 삭제. ID DEP-FIN-02는 다시 쓰지 않는다):

```markdown
(행 삭제)
```

바꿀 문구 (안 2: ID를 남겨 이력을 보이게 할 때):

```markdown
| DEP-FIN-02 | 재무/정산 | (삭제) 금액 교차검증 | 2026-10-23 재무 재설계로 삭제. 쿠폰·환불 건별 금액 검사는 안전 레이어 코드 규칙, 재무는 사후 감사(DEP-FIN-09)로 대신 | — | 태윤 | — |
```

이유: 설계 4장 — `verify_amount` 삭제.

### A-3. [필수] 6장 DEP-FIN-04 다음 (131줄 뒤) — 새 요구사항 DEP-FIN-05~09 추가

현재 문구 (이 행 바로 뒤에 넣는다, 이 행 자체는 그대로):

```markdown
| DEP-FIN-04 | 재무/정산 | 정산 보류 | 위험 판매자 정산 보류 처리 | B | 태윤 | 권장 |
```

바꿀 문구 (위 행 아래에 5행 추가):

```markdown
| DEP-FIN-04 | 재무/정산 | 정산 보류 | 위험 판매자 정산 보류 처리 | B | 태윤 | 권장 |
| DEP-FIN-05 | 재무/정산 | 결산·리포트 | 일·월 손익 요약(총거래액, 판매 수수료 매출, 쿠폰 비용, 환불, PG 수수료, 영업 결과)과 분개 미리보기 (closing\_report). 숫자는 코드가 계산하고 LLM은 설명 문장만, 결산 확정은 사람 | 조회 | 태윤 | 권장 |
| DEP-FIN-06 | 재무/정산 | 매출·PG 대사 | 주문 ↔ 결제·환불 ↔ PG 정산을 건별로 맞춰 matched·mismatch·missing\_in\_pg·pending·fee\_anomaly로 분류. 불일치 처리 메모는 사람 | 조회 | 태윤 | 권장 |
| DEP-FIN-07 | 재무/정산 | 판매자 정산 | 판매자별 정산금(판매액 − 판매 수수료 − 판매자 부담 쿠폰 − 환불)과 보류 사유 계산, 보류 아닌 판매자를 주 단위로 묶어 confirm\_settlement 요청(사람 승인, 데모는 기록만). 정산 보류(DEP-FIN-04) 판매자는 묶음에서 제외 | C | 태윤 | 권장 |
| DEP-FIN-08 | 재무/정산 | 예산·한도 정책 | 기준표 한도와 오늘·이번 달 소진율·경보 표시, 데이터 근거로 한도 변경 제안 기록. 적용은 사람이 기준표 PR로 | 조회 | 태윤 | 권장 |
| DEP-FIN-09 | 재무/정산 | 내부통제·감사 | 쿠폰·환불·실행 관문 기록을 사후 전수 점검: 한도 바로 아래 쪼개기, 같은 고객 반복 환불·쿠폰 중복, 환불률 급변, 관문 보류·거절 추이 (audit\_report) | 조회 | 태윤 | 권장 |
```

이유: 설계 3장(모듈 5개)·7장(skill `closing_report`·`audit_report` read, `confirm_settlement` C).
형식 메모: 이 표의 열은 `ID | 부서 | skill | 상세 | 등급 | 담당 | 우선순위`이고, 인수 기준 열은 없다(인수 기준은 8장 SCN 체크박스로만 쓴다). 등급 칸은 `조회`/`A`/`B`/`C`/`B\~C`를 쓴다. **우선순위 5개 모두 `권장`은 제안값이다 [확인 필요]** — 설계 문서에 우선순위가 없다.

### A-4. [필수] 6장 DEP-CS-04 (136줄) — 현금 환불 B → A

현재 문구:

```markdown
| DEP-CS-04 | 고객지원 | 현금 환불 | refund\_request: 고객지원 관문 경유로 판매 사이트 환불 API 호출, 검증자 재무, 기준 이상 C | B | 병환 | 권장 |
```

바꿀 문구:

```markdown
| DEP-CS-04 | 고객지원 | 현금 환불 | refund\_request: 고객지원 관문 경유로 판매 사이트 환불 API 호출. 검증자 없음, 안전 레이어 코드 규칙(금액 재계산·기준표 한도)으로 판정해 한도 안이면 바로 실행, 기준 이상·금액 불일치 C | A | 병환 | 권장 |
```

이유: 설계 4장 — `refund_request`는 A·verifier 없음.

### A-5. [필수] 8장 SCN-A 셋째 항목 (168줄)

현재 문구:

```markdown
- [ ] 고객지원 보상 요청 → 마케팅 issue\_coupon → 재무 교차검증(B) → 지급 흐름이 최소 1회 발생하고, 고객지원이 get\_notice로 공지 사실을 읽어 응대
```

바꿀 문구:

```markdown
- [ ] 고객지원 보상 요청 → 마케팅 issue\_coupon → 안전 레이어 금액 검사(한도 안 A 바로 지급, 기준 이상·금액 불일치 C 보류) 흐름이 최소 1회 발생하고, 고객지원이 get\_notice로 공지 사실을 읽어 응대
```

이유: 재무 교차검증 단계가 없어진다.

### A-6. [필수] 8장 SCN-A 넷째 항목 (169줄)

현재 문구:

```markdown
- [ ] 롤백·쿠폰 지급이 B 등급 교차검증을 거쳐 실행
```

바꿀 문구:

```markdown
- [ ] 롤백이 B 등급 교차검증을 거쳐 실행되고, 쿠폰 지급은 교차검증 없이 안전 레이어 코드 규칙으로 판정(한도 안 A 실행, 한도 초과·금액 불일치 C, finance 호출 없음)
```

이유: 설계 10장 안전 관문 테스트(쿠폰·환불 A·executed / C / finance 교차검증 호출 없음)를 인수 기준으로 옮김.

### A-7. [필수] 10장 Agent Card 필수 필드 예시 (236줄)

현재 문구:

```markdown
  "description": "재무/정산 — 손실 집계, 보상 승인, 판매자 심사, 정산 보류",
```

바꿀 문구:

```markdown
  "description": "재무/정산 — 결산·리포트(손실 집계 포함), 매출·PG 대사, 판매자 정산·심사·정산 보류, 예산·한도 정책, 내부통제·감사",
```

이유: "보상 승인"은 재무 일이 아니게 됨(설계 1장).

### A-8. [필수] 10장 등급 판정 응답 (274줄)

위 예시 요청이 `issue_coupon`이라, 응답의 `"verifier": "finance"`가 바뀐 규칙과 충돌한다.

현재 문구:

```markdown
응답: `{ "execution_id", "grade": "A|B|C", "status": "executed|verifying|held|rejected|failed", "computed_amount", "threshold_version", "verifier": "finance", "hold_reason", "review_id" } — claimed_grade는 기록만 하고 판정에 쓰지 않는다`
```

바꿀 문구:

```markdown
응답: `{ "execution_id", "grade": "A|B|C", "status": "executed|verifying|held|rejected|failed", "computed_amount", "threshold_version", "verifier": null, "hold_reason", "review_id" } — claimed_grade는 기록만 하고 판정에 쓰지 않는다. verifier·verifying은 B 액션(예: rollback_deployment → planning)에서만 채워진다`
```

이유: 쿠폰은 A라 verifier가 없고 `verifying` 상태가 오지 않음(설계 9장).

### A-9. [필수] 11장 체크리스트 "4. 시나리오 A 완주" (316줄)

현재 문구:

```markdown
- [ ] 손실 리포트·금액 교차검증 (DEP-FIN-01\~02), 쿠폰 지급·get\_notice (DEP-MKT-02, DEP-MKT-04, 재헌) — 태윤
```

바꿀 문구:

```markdown
- [ ] 손실 리포트 (DEP-FIN-01), 쿠폰 지급·get\_notice (DEP-MKT-02, DEP-MKT-04, 재헌) — 태윤
```

이유: DEP-FIN-02 삭제.

### A-10. [필수] 11장 체크리스트 "6. 시나리오 B" (328줄 뒤에 한 줄 추가)

현재 문구 (이 줄 바로 뒤에 넣는다):

```markdown
- [ ] 판매자 심사·정산 보류, C 등급 보류 (DEP-FIN-03\~04, SAF-05) — 태윤
```

바꿀 문구:

```markdown
- [ ] 판매자 심사·정산 보류, C 등급 보류 (DEP-FIN-03\~04, SAF-05) — 태윤
- [ ] 결산·리포트, 매출·PG 대사, 판매자 정산, 예산·한도 정책, 내부통제·감사 (DEP-FIN-05\~09) — 태윤
```

이유: 새 요구사항을 실행 목록에 넣음. 판매자 정산이 정산 보류와 묶이므로 6단계에 둔다 **[확인 필요: 단계 위치는 제안]**.

### A-11. [필수] 12장 "해소된 질문" (354줄)

현재 문구:

```markdown
해소된 질문: 기준 금액·누적 한도(재무 기준표 v1), 교차검증 담당(돈은 재무, 롤백은 기획개발), 롤백 대상(데모에서 롤백 = 장애 주입 해제), 부서 간 안건(공통 구성안 13장 결정 1\~16).
```

바꿀 문구:

```markdown
해소된 질문: 기준 금액·누적 한도(재무 기준표 v1), 교차검증 담당(롤백은 기획개발. 쿠폰·환불 금액은 2026-10-23 재무 재설계로 교차검증 없이 안전 레이어 코드 규칙이 검사하고, 재무는 한도 정책·사후 감사), 롤백 대상(데모에서 롤백 = 장애 주입 해제), 부서 간 안건(공통 구성안 13장 결정 1\~24).
```

이유: "돈은 재무"가 더 이상 맞지 않음. 끝의 `1\~16`은 지금도 공통 구성안 결정이 21번까지 있어 낡았다. 아래 C-10에서 22~24를 추가하므로 `1\~24`로 같이 고친다(이 숫자 수정은 재설계와 별개, 빼도 됨).

### A-12. [확인 필요] 5장 SAF-09 사후 검증 (106줄)

현재 문구:

```markdown
| SAF-09 | 사후 검증 | B 등급 액션 실행 후 결과 확인(쿠폰 발급 수, 롤백 후 지표 등), 불일치 시 경보 | 태윤 | 필수 |
```

바꿀 문구 (제안):

```markdown
| SAF-09 | 사후 검증 | B 등급 액션 실행 후 결과 확인(롤백 후 지표 등), 불일치 시 경보. 쿠폰·환불(A)은 실행 결과(쿠폰 발급 수·환불 금액)를 재무 사후 감사(DEP-FIN-09)가 전수 점검 | 태윤 | 필수 |
```

이유: 쿠폰이 A가 되면 "B 등급 … 쿠폰 발급 수" 예시가 맞지 않음. **설계 문서는 쿠폰·환불의 실행 직후 결과 확인을 safety가 계속할지 정하지 않았다.** safety가 A 쿠폰도 계속 사후 확인한다면 "B 등급 액션과 쿠폰·환불 실행 후 결과 확인(쿠폰 발급 수, 롤백 후 지표 등)"으로 쓰는 쪽이 맞다. 태윤이 고른다.

### A-13. [선택] 5장 SAF-11 규칙 기반 검증 (108줄)

현재 문구:

```markdown
| SAF-11 | 규칙 기반 검증 | B 등급 중 금액·대상 수 검증은 LLM이 아닌 코드 규칙으로 판정 | 태윤 | 권장 |
```

바꿀 문구:

```markdown
| SAF-11 | 규칙 기반 검증 | 금액·대상 수 검증(쿠폰·환불 건별 금액 재계산·기준표 한도 포함)은 LLM이 아닌 코드 규칙으로 판정 | 태윤 | 권장 |
```

이유: 건별 금액 검사가 이제 A 액션(쿠폰·환불)에도 코드 규칙으로만 일어남. 지금 문구도 틀리지는 않아 선택.

### A-x. 확인했지만 바꾸지 않는 곳 (`requirements.md`)

| 위치 | 판단 |
| --- | --- |
| 5장 SAF-02 (99줄) 등급 기준표 | 기준표 한도는 그대로 C 승격 기준. 변경 없음 |
| 5장 SAF-04 (101줄) B 등급 교차검증 | 롤백 등 다른 B 액션에 계속 필요. 변경 없음 |
| 6장 DEP-FIN-01·03·04 (128·130·131줄) | 설계 4장에서 유지(`loss_aggregation`·`seller_review`·`settlement_hold`) |
| 7장 DSH-03 (153줄) "B 교차검증" 표시 | 롤백 B가 남아 문구는 유효. 보류 큐에 `confirm_settlement`가 새로 나타나는 것은 기존 "C 보류" 표시 범위 안 |
| 8장 SCN-B (174~175줄) | 판매자 심사·정산 보류 유지라 변경 없음 |
| 8장 SCN-S (187~188줄) | 안전 레이어가 내려가면 거부(fail-closed, SAF-08은 A에도 적용), 소액 반복 쿠폰은 누적 한도로 A → C 승격. 문구 그대로 유효 |
| 10장 이벤트 목록 | 재무가 구독하는 `coupon.issued`·`payment.refunded`는 유지(설계 4장). 변경 없음 |

---

## B. `docs/agent-common.md`

장 번호 확인: 2장 에이전트 목록, 6장 이벤트 카탈로그, 7장 상태 변경과 안전 레이어, 8장 부서 간 책임 경계, 12장 시나리오 A 필수 범위, 13장 결정 사항. (요청에 적힌 7·8·12장 번호가 실제와 같다.)

### B-1. [필수] 2장 에이전트 표 finance 행 (26줄)

현재 문구:

```markdown
| `finance` | 재무/정산 | 태윤 | 8105 | 금액 교차검증, 대사·정산, 손실 리포트 |
```

바꿀 문구:

```markdown
| `finance` | 재무/정산 | 태윤 | 8105 | 결산·리포트(손실 리포트 포함), 매출·PG 대사, 판매자 정산, 예산·한도 정책, 사후 감사 |
```

이유: 금액 교차검증 삭제, 설계 3장 모듈 5개.

### B-2. [필수] 8장 책임 경계 "쿠폰 지급" 행 (211줄)

현재 문구:

```markdown
| 쿠폰 지급 (캠페인·보상 모두) | marketing `issue_coupon` (B, 기준 이상 C), `purpose_code`로 구분 | 검증자 finance. 보상 쿠폰은 support 요청을 직렬 처리 + `sequence` | 판매 사이트 쿠폰 API는 마케팅 관문만 호출 |
```

바꿀 문구:

```markdown
| 쿠폰 지급 (캠페인·보상 모두) | marketing `issue_coupon` (A, 기준 이상·금액 불일치 C), `purpose_code`로 구분 | 검증자 없음(안전 레이어 코드 규칙이 금액 검사). 보상 쿠폰은 support 요청을 직렬 처리 + `sequence` | 판매 사이트 쿠폰 API는 마케팅 관문만 호출 |
```

이유: 설계 4장.

### B-3. [필수] 8장 책임 경계 "현금 환불" 행 (213줄)

현재 문구:

```markdown
| 현금 환불 | support `refund_request` (B, 기준 이상 C) | 검증자 finance, 판매 사이트 환불 API는 고객지원 관문만 호출 | 마케팅은 관여하지 않음 |
```

바꿀 문구:

```markdown
| 현금 환불 | support `refund_request` (A, 기준 이상·금액 불일치 C) | 검증자 없음(안전 레이어 코드 규칙이 금액 검사), 판매 사이트 환불 API는 고객지원 관문만 호출 | 마케팅은 관여하지 않음 |
```

이유: 설계 4장.

### B-4. [필수] 8장 책임 경계 "금액 교차검증" 행 (215줄)

현재 문구:

```markdown
| 금액 교차검증 | finance `verify_amount` | 안전 레이어가 라우팅. 요청 부서는 `execute()`만 부름 | 요청 부서가 재무를 직접 부르지 않음 |
```

바꿀 문구:

```markdown
| 쿠폰·환불 건별 금액 검사 | safety 코드 규칙 (금액 재계산·`claimed_fields` 불일치·기준표 한도) | 요청 부서는 `execute()`만 부름. finance는 건별로 관여하지 않고 한도 제안·사후 감사만 | 2026-10-23 재무 재설계로 finance `verify_amount` 삭제 |
```

이유: 설계 1장·4장 — 건별 검사는 안전 관문 코드 규칙.

### B-5. [선택] 8장 책임 경계 "기준 금액 등급표" 행 (216줄)

현재 문구:

```markdown
| 기준 금액 등급표 | finance (사람만 변경) | 모든 부서는 조회. 캠페인·보상·환불 항목 분리 |  |
```

바꿀 문구:

```markdown
| 기준 금액 등급표 | finance (사람만 변경) | 모든 부서는 조회. 캠페인·보상·환불 항목 분리 | 재무 AI는 바꿀 값을 제안만 하고, 적용은 사람이 기준표 PR로 |
```

이유: 설계 2장 2번(정책은 재무가 정하고 집행은 시스템).

### B-6. [필수] 8장 책임 경계 — "판매자 심사 판단" 행(218줄) 뒤에 3행 추가

현재 문구 (이 행 바로 뒤에 넣는다, 이 행 자체는 그대로):

```markdown
| 판매자 심사 판단 | finance | data는 사기 위험 점수만 |  |
```

바꿀 문구:

```markdown
| 판매자 심사 판단 | finance | data는 사기 위험 점수만 |  |
| 결산·매출·PG 대사 | finance `closing_report` (read), 대사 | 결산 확정·불일치 처리 메모는 사람 | 숫자는 코드가 계산, LLM은 설명 문장만 |
| 판매자 정산 지급 | finance `confirm_settlement` (C) | 사람이 safety 보류 큐에서 승인. 정산 보류 판매자는 묶음에서 제외 | 데모에서는 지급을 기록만 |
| 쿠폰·환불 사후 감사 | finance `audit_report` (read) | 조사 결론 메모는 사람 | 건별 승인 대신 전수 점검 |
```

이유: "한 가지 일 = 한 주인" 표에 새 재무 일의 주인을 적음(설계 3장·7장).

### B-7. [필수] 12장 시나리오 A 필수 범위 finance 행 (325줄)

현재 문구:

```markdown
| finance (태윤) | `verify_amount`, 손실 리포트(data 결과 기반) | 일일 대사, 판매자 정산 |
```

바꿀 문구:

```markdown
| finance (태윤) | 손실 리포트(data 결과 기반) | 결산·리포트(`closing_report`), 매출·PG 대사, 판매자 정산(`confirm_settlement` C), 예산·한도 정책, 내부통제·감사(`audit_report`) |
```

이유: `verify_amount` 삭제로 시나리오 A에서 재무 필수는 손실 리포트만 남음.

### B-8. [확인 필요] 13장 후속 결정 12 (355줄)

현재 문구:

```markdown
| 12 | 교차검증 결과 상태 | 재무 거절 = completed + `outcome: rejected_by_review`, 마감 초과 = canceled, 실행 오류 = failed | 재헌·병환·준하 |
```

바꿀 문구 (제안):

```markdown
| 12 | 교차검증 결과 상태 | 검증자 거절 = completed + `outcome: rejected_by_review`, 마감 초과 = canceled, 실행 오류 = failed (2026-10-23: 쿠폰·환불은 교차검증이 없어져 해당 없음, 결정 22) | 재헌·병환·준하 |
```

이유: 상태 규칙은 다른 B 검증자(롤백 planning 등)에 계속 쓰이지만 "재무 거절" 주어가 사라짐. **결정 기록을 덮어쓸지, 원문을 두고 주석만 달지는 문서 관례가 없어 정하지 않았다**(결정 1은 "관련" 칸에 이동을 적는 방식을 썼다). 태윤·재현이 고른다.

### B-9. [필수] 13장 후속 결정 13 (356줄)

현재 문구:

```markdown
| 13 | 교차검증 호출 | 마케팅·고객지원은 `execute()`만 부르고, 안전 레이어가 finance `verify_amount`로 라우팅한다. 마케팅 v3의 "마케팅 → 재무 직접 요청" 표현은 이 방식으로 읽는다 | 재헌·태윤 |
```

바꿀 문구:

```markdown
| 13 | 교차검증 호출 | (2026-10-23 결정 22로 폐지) 마케팅·고객지원은 `execute()`만 부르고, 안전 레이어가 finance `verify_amount`로 라우팅한다. 마케팅 v3의 "마케팅 → 재무 직접 요청" 표현은 이 방식으로 읽는다 | 재헌·태윤 |
```

이유: 결정 이력은 남기고 폐지만 표시.

### B-10. [필수] 13장 — 결정 21 표 뒤(369줄 뒤, "### 구성안별 수정 목록" 앞)에 새 표 추가

현재 문구 (이 표 끝 행 뒤, 다음 제목 앞에 넣는다):

```markdown
| 21 | 원인 추적 루프 | 결정하는 쪽은 infra(원인 확정·롤백), 판정·기록은 data(업무 지표)와 planning(배포·PR 근거) (8·9장) | 예진 |

### 구성안별 수정 목록
```

바꿀 문구:

```markdown
| 21 | 원인 추적 루프 | 결정하는 쪽은 infra(원인 확정·롤백), 판정·기록은 data(업무 지표)와 planning(배포·PR 근거) (8·9장) | 예진 |

### 후속 결정 (재무 재설계, 2026-10-23)

| # | 안건 | 결정 | 관련 |
| --- | --- | --- | --- |
| 22 | 쿠폰·환불 등급 | marketing `issue_coupon`, support `refund_request`는 A, 검증자 없음. 건별 금액 검사는 안전 레이어 코드 규칙(금액 재계산·`claimed_fields` 불일치·기준표 한도)이 하고, 기준 이상·금액 불일치는 지금처럼 C. finance `verify_amount` 삭제, 결정 13 폐지. 응답 상태 `verifying`은 쿠폰·환불에 오지 않는다 (8·12장) | 태윤, 재헌·병환 |
| 23 | 재무 AI 1차 판단 | finance `review_hold`(C 보류 건 AI 1차 판단) 삭제. 재무 사후 감사(`audit_report`)로 대신 | 태윤 |
| 24 | 재무 범위 | 재무는 결산·리포트(`closing_report`, `loss_aggregation` 포함), 매출·PG 대사, 판매자 정산(주 단위 묶음 `confirm_settlement` C), 예산·한도 정책(한도 변경은 제안만, 적용은 기준표 PR), 내부통제·감사(`audit_report`). `seller_review`·`settlement_hold` 유지 (요구사항 DEP-FIN-05\~09) | 태윤 |

### 구성안별 수정 목록
```

이유: 13장은 결정 기록이 기준이라, 재설계를 결정 번호로 남겨 다른 문서가 "결정 22"로 참조하게 함.

### B-11. [필수] 13장 구성안별 수정 목록 "마케팅·판매 사이트" 행 (378줄)

현재 문구:

```markdown
| 마케팅·판매 사이트 (재헌) | (v3 반영 후 남은 것) 교차검증은 execute()만 부르고 안전 레이어가 라우팅(후속 13), 이벤트 전달은 Redis Streams(후속 14), 롤백 시 장애 주입 해제 API 제공(후속 15), 배송·반품·환불 이벤트는 2차(후속 16), 결정 대기 4번(C 해제 주체)은 결정안 2번으로 닫기, 레포·tenant\_id 확정 반영 |
```

바꿀 문구:

```markdown
| 마케팅·판매 사이트 (재헌) | (v3 반영 후 남은 것) `issue_coupon`은 A·검증자 없음, 한도 초과·금액 불일치만 C(후속 22, 후속 13 폐지), 이벤트 전달은 Redis Streams(후속 14), 롤백 시 장애 주입 해제 API 제공(후속 15), 배송·반품·환불 이벤트는 2차(후속 16), 결정 대기 4번(C 해제 주체)은 결정안 2번으로 닫기, 레포·tenant\_id 확정 반영 |
```

이유: 후속 13 폐지.

### B-12. [선택] 13장 구성안별 수정 목록 "재무/정산·안전 레이어" 행 (380줄)

현재 문구:

```markdown
| 재무/정산·안전 레이어 (태윤) | 보류 큐에 자동 거절 추가, `incident_impact_report`는 data 영향 분석 결과 기반으로, memory 카드 쓰기(A) 레지스트리 등록 |
```

바꿀 문구:

```markdown
| 재무/정산·안전 레이어 (태윤) | 보류 큐에 자동 거절 추가, `incident_impact_report`는 data 영향 분석 결과 기반으로, memory 카드 쓰기(A) 레지스트리 등록. 재무 재설계(후속 22\~24): 레지스트리 `issue_coupon`·`refund_request` A·검증자 없음, `verify_amount`·`review_hold` 삭제, `confirm_settlement`(C) 등록, 구성안을 결산·대사·판매자 정산·예산·한도 정책·내부통제 5개 모듈로 고침 |
```

이유: 재무 구성안 쪽 할 일을 목록에 남김.

### B-x. 확인했지만 바꾸지 않는 곳 (`agent-common.md`)

| 위치 | 판단 |
| --- | --- |
| 4장 `verifier` 필드 규칙 (94줄) | B 일반 규칙. 변경 없음 |
| 6장 카탈로그 `payment.*`·`order.*`·`payment.refunded`·`coupon.issued` 구독자 finance (148~151·159줄) | 장부·결산·대사 자료로 계속 구독(설계 4장 "장부 구독 유지"). 변경 없음 |
| 6장 카탈로그 `reconciliation.*`, `settlement.*` (161줄) | 설계 문서에 발행 이벤트 변경이 없다. **[확인 필요]** 판매자 정산 묶음(`confirm_settlement`) 결과를 `settlement.*`로 발행할지 설계가 정하지 않음. 정하면 이 행의 data를 "대사 결과, 정산 묶음 요청·승인"처럼 구체화 |
| 6장 카탈로그 `safety.action_*` (160줄) | 재무 감사는 이벤트 구독이 아니라 `GET /api/safety/executions`로 읽음(설계 5장). 구독자에 finance 추가 불필요 |
| 7장 공통 규칙 fail-closed·사후 검증 (178·180줄) | "교차검증 거절·무응답", "B 액션 실행 뒤"는 남은 B 액션 기준으로 유효 |
| 7장 C 보류 큐 (184줄) | 이미 "판매자 정산 확정"을 C 예로 듦. 변경 없음 |
| 11장 단독 테스트 8번 (308줄) | B 일반 규칙. 변경 없음 |
| 12장 marketing·support·safety 행 (323·324·327줄) | 등급 표기가 없거나(`issue_coupon`, `refund_request`) B 교차검증이 롤백에 남아 유효 |

---

## C. 같은 내용을 참조하는 다른 문서 (선택)

기준 문서는 아니지만 바뀐 규칙과 어긋나는 문구다. 같은 docs PR에 넣거나 각 부서에 알린다. (`docs/`는 사람 경로, `docs/notes/<부서>/`는 부서 노트)

| 파일 | 줄 | 내용 |
| --- | --- | --- |
| `docs/team-rules.md` | 125 | 자율성 등급 표 B 예시에 "쿠폰 지급" |
| `docs/team-rules.md` | 180 | 결정 기록 "교차검증은 … 안전 레이어가 finance verify\_amount로 라우팅" |
| `docs/team-rules.md` | 181 | 결정 기록 "재무 거절 completed + rejected\_by\_review" |
| `docs/team-rules.md` | 184 | 결정 기록 "현금 환불은 고객지원 refund\_request(B)" |
| `docs/agent-card-guide.md` | 24 | grade 기준 표 B 예시에 "쿠폰 지급" |
| `docs/agent-card-guide.md` | 72 | finance `compensation_approval`(보상 승인) — 이미 기준 문서에도 없는 옛 skill |
| `docs/planning.md` | 30, 92, 107, 108, 129 | 재무 역할 "금액 교차검증", "보상은 재무 승인 연동", "재무/정산 교차검증(B)", B 예시 "쿠폰 지급" |
| `docs/notes/orchestrator/interface.md` | 50 | "`verify_amount`는 총괄이 부르지 않는다 … 마케팅 `execute()` → safety → finance 경로" |
| `docs/notes/orchestrator/department-guide.md` | 90 | "`verify_amount`는 총괄이 부르지 않는다" |
| `docs/notes/hr/morning-report-1023.md` | 61 | finance skill 수 "4개 (`verify_amount` 등)" — 날짜 붙은 보고서라 고치지 않아도 됨 |
| `docs/notes/planning/requirements.md` | 65, 639, 747 | "재무 `review_hold`는 검토자 불가" — `review_hold` 삭제로 문구 정리 대상 |
| `docs/notes/safety/issue-166-planning-grades.md` | 17 | "재무 `review_hold` 재사용" 검토 메모 |

---

## D. 영향받는 부서

- **마케팅(재헌)**: `issue_coupon` B → A. 한도 안이면 바로 `executed`, 넘거나 계산이 안 맞으면 지금처럼 C `held`. 응답 `status: verifying`이 더 이상 오지 않으니 그 분기를 기다리는 코드가 있으면 정리. DEP-MKT-02 문구가 바뀌므로 docs PR 리뷰 요청.
- **고객지원(병환)**: `refund_request` B → A(동일 규칙), `verifying` 없음. DEP-CS-04 문구 변경. 보상 요청(`compensation_request`) 흐름은 그대로.
- **총괄(준하)**: `loss_aggregation` 그대로. 새 `closing_report`·`audit_report`(read, 요청 가능 orchestrator·data)는 선택. orchestrator 노트 두 곳의 `verify_amount` 문구 정리.
- **대시보드(병환)**: 재무 화면(`/finance`)은 재무가 고친다. safety 보류 큐에 `confirm_settlement`(판매자 정산 묶음) 건이 새로 나타난다. "B 교차검증" 표시에 쿠폰·환불 건이 더 이상 오지 않는다.
- **안전 레이어(태윤)**: 레지스트리 등급 변경, finance 라우팅 제거, `confirm_settlement` 등록 — 코드 PR은 이 docs PR 머지 뒤.

---

## E. docs PR 제안

- 브랜치: `docs/finance-redesign` (dev에서)
- 제목: `docs: 재무 AI 재설계 — 쿠폰·환불 A 등급, verify_amount 삭제, 재무 모듈 DEP-FIN-05~09 추가`
- 본문:

```markdown
## 왜
재무 AI를 캣북스 재무팀(결산·리포트, 매출·PG 대사, 판매자 정산, 예산·한도 정책, 내부통제·감사)으로 재설계했다(2026-10-23 태윤 확정).
건별 금액 승인은 재무 일이 아니라 안전 레이어 코드 규칙이 하고, 재무는 한도 정책을 정하고 사후에 전수 점검한다.
CLAUDE.md 11번에 따라 코드 PR 전에 기준 문서를 먼저 고친다.

설계: docs/notes/finance/2026-10-23-finance-redesign.md
개정안 원본: docs/notes/finance/docs-change-finance-redesign.md

## 바뀌는 것
- requirements.md
  - DEP-MKT-02 `issue_coupon`, DEP-CS-04 `refund_request`: B → A, 검증자 없음, 기준 이상·금액 불일치 C
  - DEP-FIN-02 금액 교차검증(`verify_amount`) 삭제
  - DEP-FIN-05~09 추가: 결산·리포트, 매출·PG 대사, 판매자 정산(`confirm_settlement` C), 예산·한도 정책, 내부통제·감사
  - SCN-A 인수 기준, Agent Card·등급 판정 응답 예시, 11장 체크리스트, 12장 해소된 질문
  - (태윤 결정에 따라) SAF-09 사후 검증, SAF-11 규칙 기반 검증
- agent-common.md
  - 2장 finance 역할, 8장 쿠폰 지급·현금 환불·금액 검사 행 + 재무 새 일 3행, 12장 finance 행
  - 13장 결정 13 폐지 표시, 후속 결정 22~24 추가, 구성안별 수정 목록

## 영향
- 마케팅·고객지원: 쿠폰·환불이 한도 안이면 바로 실행, `verifying` 응답 없음
- 총괄: `loss_aggregation` 그대로, `closing_report`·`audit_report`는 선택
- 대시보드: 보류 큐에 `confirm_settlement` 등장

## 리뷰 요청
재헌(DEP-MKT-02), 병환(DEP-CS-04·대시보드), 준하(총괄), 재현(문서 관리)
```

---

## F. 확인 필요 요약

1. A-3 새 요구사항 5개의 우선순위(모두 `권장`으로 제안)와 A-10 체크리스트 단계 위치.
2. A-12 SAF-09: 쿠폰·환불(A)의 실행 직후 결과 확인을 safety가 계속할지, 재무 사후 감사로 넘길지.
3. A-2 DEP-FIN-02: 행 삭제(안 1)와 삭제 표시로 남기기(안 2) 중 선택.
4. B-8 결정 12: 결정 기록을 고쳐 쓸지, 주석만 달지.
5. B-x 6장 `settlement.*`: 정산 묶음 결과를 이벤트로 발행할지(설계에 없음).

### F의 기본안 (태윤, 2026-10-23. docs PR에서 바꿀 수 있음)

1. 우선순위는 모두 `권장`, 체크리스트는 6단계.
2. SAF-09: 실행 직후 결과 확인은 safety가 계속한다(등급과 무관한 안전 레이어 일). 재무 사후 감사는 그 기록을 읽어 추가로 점검한다.
3. DEP-FIN-02: 안 2(삭제 표시로 남기기). 이력 추적을 위해.
4. 결정 12: 원문은 두고 "결정 22로 대체" 주석만 단다.
5. `settlement.*` 이벤트: 이번 범위에서는 발행하지 않는다. 정산 결과는 safety 실행 기록(`confirm_settlement`)으로 남는다.
