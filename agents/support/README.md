# agents/support
담당: 병환 · 포트: 8102
## 1. 역할
고객 문의를 접수하고, 고객 메시지마다 분류·정책 근거 검색·답변 초안 3가지(empathetic, concise, step_by_step)·가드 검사를 만들어 상담원 화면(dashboard-api 경유)에 제공한다.
- 분류와 문장은 LLM, 근거 점수·가드 검사·집계 숫자는 코드가 계산한다.
- 고객 원문은 `untrusted`로만 받고 PII(전화번호·이메일)를 가린 뒤 데이터 블록으로만 LLM에 넣는다.
- LLM 실패(미설정·스키마 검증 실패)는 고정 템플릿으로 대체하고 `fallback: true`로 알린다.
## 2. 실행 방법
`docker compose -f infra/compose.yml up support` / 목 모드: `SUPPORT_MOCK=1`
모델은 `.env`의 `SUPPORT_LLM_MODEL` 또는 `LLM_MODEL`로 정한다 (코드에 모델 이름을 쓰지 않는다).
## 3. 입출력 계약
`agent-card.json`의 skill 21개. 문의 skill의 입력·출력 모델은 `src/support_agent/schemas.py`, 클레임 skill은 `src/support_agent/claim_schemas.py`.

| skill | 등급 | 요청 가능 | 내용 |
| --- | --- | --- | --- |
| `list_inquiries` | read | dashboard-api, orchestrator | 문의 목록 (상태 필터) |
| `get_inquiry` | read | dashboard-api, orchestrator | 메시지·분석 포함 상세 |
| `check_reply` | read | dashboard-api, orchestrator | 답변 가드 검사 (`untrusted.text`) |
| `summarize_inquiry` | read | dashboard-api, orchestrator | 대화 요약 (저장 안 함) |
| `create_inquiry` | A | dashboard-api, shop-api, orchestrator | 문의 접수 + 첫 메시지 분석 |
| `add_customer_message` | A | dashboard-api, shop-api, orchestrator | 고객 메시지 추가 + 분석 |
| `send_reply` | A | dashboard-api, orchestrator | 답변 발송 (가드 통과 필수) |
| `reject_draft` | A | dashboard-api | 거절한 초안 기록 |
| `set_inquiry_status` | A | dashboard-api, orchestrator | 상태 변경 |
| `request_compensation` | A | dashboard-api, orchestrator | 보상 단가 판정 + 마케팅 `issue_coupon(compensation)` 직렬 요청 (DEP-CS-02) |
| `diagnose_payment` | read | dashboard-api, orchestrator | 문의의 주문에서 결제가 막힌 곳(실패 원인·중복 결제·기록 불일치)과 다음 할 일을 주문·결제 기록으로 판단 (재결제·환불은 안 함) |
| `suggest_compensation` | read | dashboard-api, orchestrator | 문의의 주문 사실로 지연 보상 후보를 제안 (쿠폰은 안 냄, 승인은 사람이) |
| `request_incident_compensation` | A | orchestrator | 사건(`ctx_inc_`) 영향 고객 전원에게 보상 쿠폰 요청 (문의를 안 낸 고객 포함, SCN-A 8단계) |
| `customer_signals` | read | dashboard-api, orchestrator | 구매자 한 명의 이탈·재문의 위험 신호(미답 문의·감정·잦은 문의·거절/잦은 클레임)를 점수 기준표로 low·medium·high와 이유로 알려 줌 |
| `list_at_risk_customers` | read | dashboard-api, orchestrator | 위험이 medium/high 이상인 구매자를 점수 높은 순으로 (리텐션·후속 연락 대상 목록) |
| `request_change` | A | dashboard-api, orchestrator | 변경 요청을 기획개발 `create_ticket`으로 보내고 `ctx_req_`를 문의와 연결 |
| `list_change_requests` | read | dashboard-api, orchestrator | 문의·변경 요청 연결 목록 |
| `list_claims`, `get_claim`, `claim_stats`, `claim_queue` | read | dashboard-api, orchestrator | 클레임 목록·상세(판정 근거·감사 기록)·건수·보상 큐 |
| `inquiry_overview` | read | dashboard-api, orchestrator, data | KPI(초안 채택률·수정 정도·가드 적중·분류별 건수)와 답변 대기 문의 (긴급도·대기 시간 순) |
| `detect_inquiry_surge` | read | dashboard-api, orchestrator, data | 분류별 문의 급증 감지 (DEP-CS-05): 최근 창 건수를 평소 창 평균(`baseline_method=median`이면 창별 건수의 중앙값)과 견줌 |
| `create_claim` | A | dashboard-api, shop-api, orchestrator | 취소·반품·교환 접수 + 규칙 판정 |
| `decide_claim` | A | dashboard-api | 사람 확인이 필요한 클레임 승인·거절 |
| `receive_return`, `inspect_return` | A | dashboard-api (입고는 shop-api도) | 반품 입고, 검수 결과 기록 |
| `process_claim_queue` | B | dashboard-api | 준비된 큐 항목을 `refund_request`(재무 교차검증, 기준 이상 C)로 지급 처리 |

- 상태를 바꾸는 skill은 모두 `execute()`를 거치고, 실행되지 않으면(보류·거부·safety 무응답) 저장소를 바꾸지 않는다.
- 액션 이름은 skill id와 같다. safety 레지스트리에 등록이 필요하다 (`to:safety`).
- 같은 `idempotency_key`의 두 번째 요청은 이전 결과를 돌려준다.
- `create_inquiry`·`add_customer_message`는 선택 입력 `notice_id`가 있으면 마케팅 `get_notice`로 공지 사실(본문·약속·기간)을 읽어 답변 근거(`notice:<id>`)의 맨 앞에 넣는다. 공지를 찾지 못하거나 마케팅이 응답하지 않으면 근거 없이 진행한다 ("확인할 수 없음").
  - 공지 번호를 모르면 마케팅 `list_active_notices`(지금 게시 중인 공지 찾기)로 읽는다. 사건(`ctx_inc_`) 요청이거나 선택 입력 `notice_component`(`api`·`db`·`payment`)가 있을 때만 부르고, 평소 문의는 마케팅을 부르지 않는다 (오래된 공지를 인용하지 않으려고). 최근 게시 순으로 최대 3건을 답변 근거로 쓰고, 공지가 없거나 마케팅에 닿지 않으면 근거 없이 진행해 복구 시각 같은 값은 "확인할 수 없음"으로 둔다. `notice_id`가 있으면 그것만 읽는다.
- `request_compensation`은 단가를 코드 정책표(`compensation.py`)로 판정하고, 사건(`context_id`)별로 `sequence` 1부터 한 건씩 마케팅에 보낸다. 지급·`execute()`·재무 교차검증은 마케팅 쪽이라 support는 자기 상태를 바꾸지 않고 safety 등록도 필요 없다. 표에 없는 사유는 `not_eligible`. 번호는 마케팅이 소비한 경우(지급·재무 거절·보류·실행 실패)에만 올리고, 재무 검증 중(`working`)·번호/입력 오류·타임아웃·연결 실패는 같은 번호로 다시 보낸다. 같은 사건의 요청은 락으로 한 건씩 처리한다.
- 문의는 접수 때 진행 중인 사건의 번호(`ctx_inc_`)를 한 번 이어받는다 (`incident_context.py`, #274). 고객 문장은 읽지 않고 **주문 사실로만** 판정한다: 문의의 주문(shop-api, 본인 주문, `ord_` 형식)이 결제 서버 문제 코드(`PG_UNAVAILABLE`)로 실패한 시도를 갖고 있을 때만, 가장 이른 실패 시각을 `occurred_at`으로 총괄 `find_active_incident(component=payment)`에 묻는다. 사건이 있으면 문의의 `context_id`가 그 번호가 되고, 접수 이벤트·후속 메시지·답변·상태 변경·보상 쿠폰 요청이 같은 번호로 기록되며 보상 `sequence`도 사건 하나로 이어진다 (재무 손실 리포트가 사건 번호로 보상 쿠폰을 센다). 응답의 `meta`는 호출한 쪽이 보낸 그대로다. 조회가 실패하거나(3초 제한) 응답이 이상하거나 사건이 없으면, 카드 거절·정상 결제·주문 없음·시드 주문일 때와 같이 기존 방식(`ctx_ui_`·`ctx_shop_`)으로 접수하고 문의 응대는 멈추지 않는다. 이미 사건 번호로 온 요청(총괄)은 다시 묻지 않고, 같은 요청의 재전송은 한 번만 묻는다. 클레임 접수(`create_claim`)는 이 PR의 범위가 아니다.
  - 사유가 `payment_failure`(결제 장애)면 사유 코드만 믿지 않고 **본인 주문의 결제 기록**으로 확인한다 (`handlers._unverified_payment_failure`). 문의의 주문(shop-api)이 본인 주문이고 결제 서버 문제 코드(`PG_UNAVAILABLE`)로 실패한 시도가 있을 때만 쿠폰을 요청한다. 카드 거절·정상 결제·시도 없음, 주문 없음, 남의 주문·다른 구매자, 시드 주문(확인할 수 없음)이면 `not_eligible`이고 쿠폰을 요청하지 않는다 (재무 감사 `compensation_outside_impact` 예방). 결제 실패 후 다시 결제해 성공한 고객도 서버 문제로 실패한 시도가 있으면 대상이다. shop-api를 읽지 못하면 `shop_unavailable`로 알리고 저장하지 않아 같은 요청을 다시 보낼 수 있다. 다른 사유와 총괄이 영향 고객 목록으로 부르는 `request_incident_compensation`은 영향받지 않는다.
- `suggest_compensation`(읽기)은 보상 후보만 제안한다 (`suggest_handlers.py`). 고객 문장은 읽지 않고, 문의의 주문(shop-api, 목 모드는 시드)의 약속 배송일·배송 완료 시각으로 코드가 지연 일수(채운 일수, 배송 전이면 지금까지)를 세어 `LATE_MIN_DAYS`(1일) 이상이면 `late_delivery` 후보를 돌려준다. 약속일이 없으면 `확인할 수 없음`으로 돌려주고 지어내지 않는다. 문의의 구매자(`buy_<번호>`)와 주문의 구매자가 다르면 not_found다. 응답의 `request`를 담당자가 승인하면 그대로 `request_compensation`에 보낸다 (자동 발급 없음: 마케팅 `issue_coupon`은 B 등급이라 재무 검증을 거치고 기준 금액 이상은 안전 레이어가 보류한다).
- `request_incident_compensation`(A)은 장애로 피해를 본 고객 전원의 보상을 요청한다 (`incident_handlers.py`). 영향 고객은 데이터분석 영향 분석 결과를 총괄이 `buyer_ids`로 넘기며 문의가 없는 고객도 대상이다. 총괄만, 사건(`ctx_inc_`) 안에서만 부를 수 있고, 구매자 ID가 `buy_<4자리>`가 아니거나 50명을 넘으면 전체를 거부한다. 사유는 `payment_failure`만 받고 단가(시드 2,000원)와 건수 상한(3건)은 정책표가 정한다. 고객마다 마케팅 `issue_coupon`에 한 건씩 직렬로 요청하며(재무 검증, 한도 초과는 C 보류 → 결과 `held`) 지급은 마케팅이 한다. 같은 사건·구매자·사유는 `request_compensation`과 같은 키라 한 번만 요청되고, 마케팅은 같은 사건·구매자에게 번호만 달라도 새 쿠폰을 내지 않고 이전 결과에 잇는다(`linked_duplicate`, 결과 `duplicate`, 합계 0). 마케팅에 닿지 않거나 요청을 거부(`SEQUENCE_GAP` 등)하면 남은 고객은 `skipped`로 멈춘다 (소비되지 않은 `sequence`로 다시 요청할 수 있다). `request_compensation`도 같은 정책표라 문의를 낸 고객은 `payment_failure` 사유로 보상할 수 있다.
  - 사건당 상한: 같은 사건에서 여러 번 불러도 인원 100명·합계 60만 원(시드 값, 고객지원 정책표 값이라 팀이 확정한다, `compensation.MAX_INCIDENT_*`. 안전 관문의 금액 한도는 사람이 바꾸는 기준표가 따로 정한다)을 넘지 못한다. 넘는 고객은 `skipped`이고 응답에 `limit_reached`가 켜진다. 이미 요청한 고객의 재전송은 막지 않는다. 마케팅이 재무 검증 중(`working`)이면 결과는 `pending`이고 번호가 소비되지 않아 남은 고객은 멈춘 뒤 같은 번호로 다시 요청한다. 구매자 ID는 ASCII 숫자만 받는다(`[0-9]`, 유니코드 숫자 거부).
- `diagnose_payment`(읽기)는 결제가 막힌 곳을 알려 준다 (`payment_handlers.py`). 고객 문장은 읽지 않고, 문의의 주문(shop-api, 결제 시도 기록 포함)으로 코드가 상황(`paid`, `payment_failed`, `not_attempted`, `duplicate_payment`, `inconsistent`, `cancelled`, `other`)과 다음 할 일(`retry_payment`, `wait_and_retry`, `complete_payment`, `review_refund`, `handoff_review`, `none`)을 정한다. 실패 코드가 `PG_UNAVAILABLE`이면 결제 서버 문제로, `PG_DECLINED`면 거절로 본다. 성공한 결제가 둘 이상이면 중복 금액을 계산해 환불 검토로 넘기며 자동 환불은 없다. 문의의 구매자와 주문의 구매자가 다르면 not_found이고, 주문 출처가 시드(목)이면 결제 기록이 없어 담당자 확인으로 돌려준다.
- `customer_signals`·`list_at_risk_customers`(읽기)는 데이터분석팀 정리표의 이탈 예측·고객 행동 분석을 고객지원 데이터로 좁혀 적용한 것이다 (`customer_signals.py`). 문의·클레임 기록에서 코드가 점수를 센다: 하루 넘게 답 못 받은 문의 3점, 화난 메시지 건당 2점·답답한 메시지 건당 1점(각 2건까지), 30일 3건 이상 문의 2점, 이관 1점, 90일 안 거절된 클레임 2점, 클레임 2건 이상 2점. 합이 3점 이상 medium, 6점 이상 high이고 `recommended_action`은 none·proactive_check·personal_followup이다. 클레임이 90일에 3건 이상이면 이탈과 별개로 `claim_pattern_review`를 켠다. 구매자 가명 ID와 건수·이유 코드만 돌려주고 이름과 문의 원문은 읽지도 담지도 않는다. 구매 이력이 필요한 RFM·LTV·코호트는 data 소관이라 여기서 하지 않는다. 위험 단계는 사실에서 코드가 낸 값이라 쓰는 사람이 확인한다.

## 다국어와 키워드 (`languages.py`, `drafting.py`, `guard.py`)
- 고객 메시지의 언어는 LLM이 아니라 **코드가 문자 종류로 판별**한다 (`languages.detect`): 한글 `ko`, 가나(한자 포함) `ja`, 한자만 `zh`, 라틴 문자 `en`, 그 밖(키릴·아랍·태국어 등) `other`. 분류 결과 `classification.language`에 담긴다.
- 답변은 **한국어·영어·일본어·중국어**로만 쓴다. 가드가 이 네 언어의 약속 표현을 검사할 수 있기 때문이다. 그 밖의 언어로 온 문의에는 영어로 답한다. 한국어가 아닌 초안에는 상담원이 읽을 한국어 번역 `reply_ko`와 초안 언어 `language`가 붙는다. LLM을 쓸 수 없을 때는 언어별 대체 템플릿(환불·보상을 약속하지 않는 문장)을 쓴다.
- 정책 문서는 한국어라서 근거 검색 질의(`search_query`)는 고객 언어와 관계없이 한국어로 만든다 (LLM 대체 경로는 분류별 한국어 검색어).
- 키워드(`classification.keywords`)는 LLM이 고른 핵심 단어를 **코드가 걸러서** 최대 5개만 남긴다: 개인정보가 든 것(전화번호 등)은 버리고, 빈 값·긴 문장·중복(대소문자 무시)을 뺀다. LLM이 못 주면 분류 규칙 단어(환불·refund·返金 등)로 채운다.
- 가드는 영어·일본어·중국어 약속 표현(`We will definitely refund…`, `返金を必ずお約束…`, `我们保证退款` 등)을 한국어와 같이 막는다. 가드가 검사하지 못하는 문자(키릴·아랍·태국어 등)는 `unsupported_language` 오류로 막는다. 악센트가 있는 라틴 글자(스페인어 등)는 영어와 구분되지 않아 `language_not_verified` 경고만 하고 통과시키니 직접 확인해야 한다.
- 예전에 저장된 분석·초안 기록에는 새 필드가 없어도 읽힌다 (`language`는 `ko`, `keywords`는 빈 목록).

## 영수증·환불·교환 요청 분석 (`receipts.py`, `claim_analysis.py`)
둘 다 **읽기 skill**(요청 가능: dashboard-api, orchestrator)이라 상태를 바꾸지 않고, 접수·지급은 하지 않는다.
- `analyze_receipt`: 영수증 글(`untrusted.receipt_text`)과 사진(`untrusted.images`, 최대 4장)에서 금액·날짜·주문번호·배송비·품목을 **코드(정규식)가 뽑아** 문의에 붙은 주문(shop-api)과 대조한다. 총 결제금액은 "총 결제·결제금액" 줄을 우선하고, 후보가 서로 다르면 고르지 않는다(`ambiguous_total`). 금액은 실결제 현금 또는 포인트 전 총액과 같으면 맞다고 본다. 날짜는 하루 차이까지 허용한다. 결과는 `consistent`·`mismatch`·`insufficient`와 불일치 코드(`RECEIPT_AMOUNT_MISMATCH`·`RECEIPT_DATE_MISMATCH`·`RECEIPT_ORDER_NO_MISMATCH`·`RECEIPT_SHIPPING_MISMATCH`·`RECEIPT_ITEM_UNKNOWN`·`RECEIPT_ITEM_AMOUNT_MISMATCH`)뿐이다. **환불 판정은 하지 않는다.**
  - 개인정보(카드번호·전화번호·계좌 등)는 가린 뒤에 읽고, 결과에는 뽑은 사실과 가린 종류만 담는다. 영수증 원문과 사진은 어디에도 저장하지 않는다.
  - 사진은 `receipts.set_vision_extractor(함수)`로 이미지 분석기를 꽂았을 때만 읽는다. 꽂지 않았으면 `images_status: unavailable`로 읽지 못했다고 알리고 아무것도 지어내지 않는다. 글과 사진 값이 다르면 글 값을 쓰고 알린다. 개수·크기·형식(jpeg·png·webp)을 넘으면 읽지 않는다(`skipped`).
  - **이미지 분석기는 아직 연결되지 않았다.** agent-template의 LLM 래퍼가 이미지 입력을 지원하면 `VisionExtractor`(이미지 목록 → `ReceiptFacts`)를 구현해 꽂는다. 사진 저장 위치와 보관 정책은 사람이 정한다.
- `analyze_claim_request`: 문의의 마지막 고객 메시지(개인정보를 가린 뒤)를 환불·교환·취소·이벤트 문의로 분류한다. **LLM은 분류만** 하고(요청 종류, 사유, 말한 품목, 이벤트 단어), LLM을 쓸 수 없으면 단어 규칙으로 대신하며 `source: rules`로 표시한다. 그다음은 코드가 한다: 말한 품목을 주문 품목과 맞추고(말하지 않았으면 전체), 클레임 유형을 정하고(환불은 배송 전이면 `cancel`, 배송 뒤면 `return`), 접수했다면 어떻게 판정될지 `claim_rules.decide`로 **환불 범위를 미리 계산**(`preview`)하며, 담당자가 확인해 `create_claim`에 보낼 입력(`suggested_claim`)을 돌려준다. 이벤트·쿠폰 문의는 `event_related`와 키워드, 근거 문서(`policy_sources`)만 돌려주고 미리 계산은 없다.
  - 고객 문장은 금액에 영향을 줄 수 없다 (금액은 주문 사실과 규칙에서만 나온다). 사유를 모르면 `other`로 계산하고 알린다.

## 고객 화면 대리 호출 (shop-api)과 집계
- 사이트의 문의 위젯은 shop-api가 로그인한 고객을 대신해 `create_inquiry`·`add_customer_message`·`list_inquiries`·`get_inquiry`를 부른다 (요청자는 토큰에서 꺼낸 `shop-api`만 해당). 이때는 **`user_id`가 필수**이고 그 고객의 문의만 다룬다. 남의 문의는 있는지도 알려 주지 않고 `not_found`로 답하며, 남의 문의에는 메시지를 덧붙일 수 없다.
- 응답은 **고객용 모양**으로 줄인다: 문의는 `id`·`subject`·`status`·`order_id`·`updated_at`(목록은 `preview`, 상세는 `messages`의 `id`·`role`·`text`·`created_at`)뿐이고, 분석·초안·분류·감정·긴급도·고객 이름·상담원 메모는 싣지 않는다. 같은 요청의 재시도도 같은 모양이다. 상담원 화면(dashboard-api, orchestrator)의 응답은 그대로다. 상담원은 `user_id`로 한 고객의 문의만 걸러 볼 수도 있다.
- shop-api는 답변 발송(`send_reply`)·초안 처리·상태 변경은 못 한다 (카드 허용 목록에 없다).
- KPI(`inquiry_overview`·`kpis`)에 코드가 센 추세가 늘었다: `by_sentiment`(감정별 문의 수), `by_language`(문의별 마지막 고객 메시지의 언어), `top_keywords`(많이 나온 키워드 최대 10개, 한 메시지에서 같은 키워드는 한 번만, 대소문자 무시).

## DejaFix로 보내는 고객 개인정보 (`secure_transfer.py`)
고객 개인정보는 캣북스 쪽에만 두고, DejaFix(에이전트 이름 `infra`, 팀 이름 `dejafix`·`infra-dejafix`)로 나가는 요청은 **필드 단위로 암호화**한다. 다른 에이전트로 나가는 요청도 모두 `handlers.SEND` 한 곳을 지나가고, 그곳에서 DejaFix 대상만 아래 규칙을 적용한다.
- 고객 개인정보 필드(`customer_name`·`name`·`email`·`phone`·`address`·`message`·`text`·`subject`·`description`·`receipt_text`·`images`)와 `untrusted`(고객이 쓴 글) 전체를 AES-256-GCM으로 암호화한다. 주문·구매자 가명 ID(`buy_…`)·금액·건수 같은 사실은 그대로 둔다.
- 암호문은 사건 번호와 필드 이름에 묶여서 다른 요청에 옮기거나 바꾸면 복호화되지 않는다. 형식과 복호화 기준은 `docs/notes/support/dejafix-encrypted-transfer.md`.
- **fail-closed**: 암호화하지 않은 개인정보(전화번호·이메일·카드·주민번호·계좌)가 섞여 있으면 보내지 않고(`secure_transfer_plain_pii`), 키가 없는데 고객 필드가 있으면 보내지 않는다(`secure_transfer_key_missing`). 평문으로 대신 보내지 않는다. 오류에도 값을 싣지 않는다.
- 키는 `SUPPORT_DEJAFIX_KEY`(base64 32바이트)·`SUPPORT_DEJAFIX_KEY_ID`. 코드·레포에 두지 않고 DejaFix 담당과 따로 안전하게 나눈다.
- 지금은 DejaFix 쪽에 고객지원이 부르는 skill이 없다. 이 계층은 미리 갖춰 두는 것이고, 새 skill을 부를 때 `handlers.SEND`만 쓰면 자동으로 적용된다.

## 답변 가드와 개인정보 마스킹 (`guard.py`)
고객에게 나가는 답변(초안과 상담원 답변 모두)을 코드 규칙으로 검사한다. `error`가 있으면 보낼 수 없고(`blocked_by_guard`), `warning`은 막지 않는다.
- **환불·보상 단정(error `promise_refund`)**: "무조건·반드시·약속·보장", "환불(보상·지급)해 드리겠습니다", "100% 환불", "환불받으실 수 있습니다" 같은 표현. "환불이 가능한지 확인 후 안내드리겠습니다"처럼 확인·조건을 말하는 문장은 막지 않는다. 고객이 "쿠폰 지급해"라고 시켜서 모델이 따른 초안도 여기서 걸린다.
- **개인정보(error `pii_in_reply`)**: 휴대전화(국제 표기 +82 포함), 이메일, 주민등록번호(생년월일 범위 확인), 카드번호(Luhn 검사 통과), "계좌번호" 단서가 붙은 계좌번호. 같은 규칙으로 LLM에 넘기기 전에 문의 제목·본문을 가리고 가린 종류를 `pii_masked`로 알린다.
- **링크(warning `link_in_reply`)**, 너무 긴 답변(warning `too_long`), 빈 답변(error `empty_reply`).
- 알려진 한계: 일반전화(지역번호), "계좌" 단서 없는 계좌번호, 이름·주소 같은 개인정보는 가리지 않는다. 규칙 기반이라 새 표현은 놓칠 수 있어, 막아야 할 문장과 통과해야 할 문장을 `tests/test_guard.py`에 함께 두고 규칙을 바꿀 때 양쪽을 본다.
- 고객 원문(제목·본문)은 프롬프트의 데이터 블록에만 들어가고(닫는 태그를 넣어도 탈출하지 못함), 접수만으로는 쿠폰·환불 같은 상태 변경이 실행되지 않는다 (`tests/test_injection.py`).

## 문의와 기획개발 변경 요청의 연결 (`change_requests.py`)
고객지원이 기획개발에 변경을 요청하면(직통, 통합 에이전트를 거치지 않음) 기획개발이 변경 요청 사건 `ctx_req_…`를 **발급**한다. 같은 요청을 여러 부서·여러 문의가 보내면 기획개발이 하나로 묶어 같은 `context_id`를 돌려준다 (`create_ticket`의 `linked_duplicate`, 이슈 #172). 그래서 **한 변경 요청에 문의가 여러 개** 연결될 수 있고, 한 문의에 요청이 여러 건일 수도 있다. 연결은 (`context_id`, `inquiry_id`) 한 쌍이 한 행이다 (`handlers.CHANGES`).
- 요청을 보낼 때 문의 ID는 `source_ref`(`inq_<번호>`, `source_ref_for`)로 보내고, 돌려받은 `context_id`로 이 저장소에 이어 둔다. `source_ref`는 기획개발의 판정에 쓰이지 않는다.
- 결과 통지는 기획개발이 **같은 `context_id`에 `path`를 비운 새 요청**으로 보낸다 (원래 요청의 `meta`를 재사용하면 `path`에 support가 남아 `LOOP_DETECTED`). 그 `context_id`에 연결된 **모든 문의**에 같은 결과를 적용한다 (`record_result`).
- 상태: `requested`(결과 대기) → `completed` 또는 `declined`. 같은 결과를 다시 받으면 재전송으로 보고 그대로 두며, 이미 끝난 요청에 다른 결과가 오면 충돌이다. 이미 끝난 요청에 새 문의가 묶이면 그 결과(와 공지)를 이어받는다. 상태 이름은 계약에 맞춰 `CHANGE_STATUSES` 한 곳에서 바꾼다.
- 처리가 끝난(`completed`) 요청에는 고객 공지가 필요했을 때 마케팅 `notice_id`를 한 번 붙일 수 있다 (연결된 모든 문의에). 공지 조회 후 답변은 이미 `notice_id` 입력으로 동작한다.
- DB가 있으면 `change_requests` 테이블에 저장되어 재시작해도 이어진다.
- skill `request_change`(A)가 기획개발 `create_ticket`을 직통으로 부른다 (`change_handlers.py`). 입력은 `inquiry_id`, `title`(바꿀 내용 한 줄), `evidence`(건수·ID 같은 사실 요약), `evidence_ids`이고, 제목·근거에 전화번호 등 개인정보가 있으면 보내기 전에 거부한다. 고객 원문은 보내지 않는다(`untrusted`도 비움). 기획개발이 자기 `execute()`로 요청을 만들어 `REQ-n`과 `ctx_req_`를 돌려주면(`created` 또는 `linked_duplicate`) 그 사건을 문의와 연결해 저장한다. 같은 멱등 키의 재시도는 기획개발에 다시 보내지 않는다. 기획개발이 실패·시간 초과면 `planning_unavailable`로 실패하고 아무것도 연결하지 않으며, 보류 같은 정책 거절은 `planning_<outcome>`으로 그대로 알린다.
- `list_change_requests`(읽기)는 연결 목록을 문의·사건·상태로 걸러 돌려준다.
- `receive_change_result`(A, 요청 가능: planning만)가 기획개발이 요청을 닫을 때(완료·거절) 보내는 결과(계약 `ChangeResultInput`)를 받는다. 통지는 `ctx_req_` 사건 하나당 한 번 오고(중복 연결된 요청은 원래 요청이 닫힐 때 같은 사건으로 한 번에), 그 사건에 연결된 **모든 문의**에 같은 결과를 적용해 `applied`(적용한 문의 수)로 답한다. 같은 결과가 다시 오면 한 번만 적용한 것으로 보고(`duplicate`), 이미 닫힌 요청에 다른 결과가 오면 거절하며(`invalid_state`), 이 사건에 연결된 문의가 없으면 다른 부서 몫이라 `applied=0`(`not_linked`)이다. `ctx_req_`가 아닌 사건이나 계약을 어긴 입력(거절인데 PR 번호, 완료인데 사유 코드 등)은 `invalid_input`이다. 요약(`summary`)은 기획개발이 코드로 만든 한 줄이라 300자까지만 보관한다. 결과는 문의 목록 조회(`list_change_requests`)에 보이고 재시작해도 이어진다. 저장소 기록만 하는 내부 처리라 안전 관문(`execute()`)은 거치지 않는다(등록된 액션 없음). 고객 공지(`notice_id`)는 따로 붙인다.

## 영속 저장소 (PostgreSQL)
`SUPPORT_PG_DSN`을 설정하면 문의·클레임·멱등 결과·보상 번호가 DB에 저장되어 재시작해도 이어진다. 비우면(CI·목 모드) 예전처럼 메모리만 쓴다. compose는 `support-db`(postgres:16, 호스트 포트 없음)를 함께 띄우고 DSN을 서비스 이름으로 덮어쓴다 (비밀번호 `SUPPORT_DB_PASSWORD`).
- 메모리 저장소(`InquiryStore`, `ClaimStore`, `SequenceLedger`)를 상속한 영속 저장소가 읽기는 메모리 캐시, 쓰기는 같은 호출 안에서 DB에 반영한다. 시작할 때 DB에서 모두 읽어 와 ID 번호(문의·메시지·클레임)와 보상 `sequence`도 이어 간다.
- 테이블(모두 `tenant_id`·`context_id` 포함, 본문은 `doc` jsonb): `inquiries`, `inquiry_results`(멱등 결과), `draft_events`(초안 채택·수정·거절 기록), `claims`, `change_requests`(문의·변경 요청 연결), `compensation_sequences`, `compensation_results`. 스키마는 시작할 때 `CREATE TABLE IF NOT EXISTS`로 만든다 (별도 마이그레이션 도구 없음).
- 보상 `sequence`를 저장하는 이유: 재시작하면 번호가 1로 돌아가 마케팅이 `SEQUENCE_GAP`으로 거절하고 이후가 계속 막힌다.
- 한계: 단일 인스턴스를 가정한다 (여러 프로세스가 같은 DB를 쓰면 캐시가 어긋난다). 쓰기 실패는 예외로 올린다 — 안전 레이어가 이미 허가한 뒤라면 같은 `idempotency_key`로 다시 요청해야 하고, 메모리에는 이미 반영돼 있을 수 있다. 연결이 끊기면 한 번 다시 연결해 재시도한다.
- 테스트: DB 없이 도는 테스트(메모리 구현으로 "저장 → 재시작 → 이어짐")와, `SUPPORT_PG_DSN`이 있을 때만 도는 PostgreSQL 테스트가 `tests/test_persistence.py`에 있다.

## 4. 의존
ax-contracts, ax-agent-template
## 5. 하지 않는 것
- 클레임 판정(`claim_rules.py`)은 취소·반품·교환만 한다. 주문 사실로 코드가 판정하고(`orders.py` 시드 주문, 7일 환불 기간·불량 증빙·잦은 요청·고액 확인), 금액은 입력으로 받지 않는다. 규칙 값은 시드이며 memory 정책 문서 API가 생기면 교체한다. 보상 쿠폰은 클레임이 아니라 `request_compensation`으로 요청하고 지급은 마케팅이 한다. 클레임은 판정에 쓴 주문 사실의 스냅샷(`receipt`), 상담원·검수 메모(`untrusted.note`), 감사 기록(판정 시 `abuse_score`, 승인·거절 시 `system_outcome`·`agent_action`, 지급 시 `refund_cash`)을 담고, 보상 큐 항목은 입고 시각·품목·화면용 차단 코드(`NOT_INSPECTED`, `ON_HOLD`)를 담는다.
- 집계(`insights.py`)는 문의 기록에서 코드가 센다. 비율은 분모가 0이면 `null`이다(0으로 채우지 않음). 초안 수정 정도는 `send_reply`가 초안과 보낸 답변의 차이로 기록한다. 급증 감지는 평소가 0이면 건수 기준만 쓰고 `ratio`는 `null`이며, 샘플은 문의 ID만 담는다. `support.inquiry_surge` 이벤트 발행은 contracts에 data 모델이 생긴 뒤에 한다.
- 급증 감지의 평소 기준은 `baseline_method`로 고른다. 기본 `mean`은 기준 기간의 창당 평균(기존 동작)이고, `median`은 기준 기간을 창으로 나눠 창별 건수(0건인 창 포함)의 중앙값을 쓴다. 지난 어느 한 시간에 문의가 몰렸어도 평소가 부풀지 않아 급증을 놓치지 않는다. 응답의 `baseline_method`에 쓴 방식이 나온다.
- 클레임의 주문·구매자·품목 ID는 모두 **문자열**이다 (shop-api 형식에 맞춤): 주문 `Order.id`(시드 `"101"`, shop-api 연결 뒤 `ord_…`), 구매자 `buyer_id`(shop-api 형식 `buy_<4자리>`, 사용자 번호 N ↔ `buy_`+0 채운 4자리, `buyer_ids.py`), 품목 `itm_…`. 예전 기록은 읽을 때 지금 형식으로 바꾸고(`user_id: 1`·`usr_1`은 `buyer_id: buy_0001`), PostgreSQL `claims` 테이블은 시작할 때 자동으로 옮긴다(`user_id`→`buyer_id`, 값은 `buy_…`, `order_id` bigint→text). 문의 ID(`inq_<번호>`)는 그대로다. 재무 목(`mock_seed.json`)은 주문 키를 `"101"`·`"102"`로 찾으므로 시드 주문과 맞는다.
- 환불 지급 요청(`refund_request`)의 `params.summary`는 코드가 만든 사실(금액·클레임·주문 ID·유형/사유 코드)만 담는다. 10만 원 이상 C 보류가 되면 safety가 앞 200자를 보류 기록에 담아 담당자가 대시보드에서 읽는다. 고객 이름·메모 같은 외부 입력은 넣지 않는다 (테스트로 확인).
- 이벤트 `support.inquiry_received`(SHOP-09)는 `create_inquiry`·`add_customer_message`가 `execute()`로 실행되고 저장이 끝난 뒤 한 번씩 발행한다(agent-template `events.publish`). `data`는 계약 `SupportInquiryReceivedData`의 사실 필드(문의 ID, 분류, 긴급도, 고객 메시지 수)만 담고, 제목·본문은 전화번호·이메일을 가린 채 봉투 `untrusted`에만 담는다. 멱등 키 `<context_id>:support.inquiry_received:<inquiry_id>:<메시지 수>`가 `event_id`가 되어 재전송해도 한 번만 전달된다. 실행되지 않은 요청과 재시도(같은 `idempotency_key`)는 발행하지 않고, 발행이 실패해도 접수는 막지 않는다. 문의의 `order_id`도 클레임처럼 문자열이다(shop-api `ord_…`, 옛 숫자 기록·입력은 읽을 때 문자열로 바꾼다). 이벤트 `order_id`는 `ord_`로 시작할 때만 싣고, 시드 주문(`"101"`)처럼 그 형식이 아니면 지어내지 않고 `null`로 보낸다.
- 환불 지급(`process_claim_queue`)의 `refund_request` 멱등 키와 사건은 처리를 요청한 호출이 아니라 **클레임을 접수한 사건** 기준(`<claim.context_id>:refund_request:claim:<id>`)으로 고정한다. 안전 레이어는 C 보류 건을 사람이 승인한 뒤 **같은 키**로 다시 요청해야 `executed`를 돌려주므로, 호출마다 새 키를 쓰면 승인이 반영되지 않는다. 처리를 요청한 사건은 근거 `ref_ids`(`request:<context_id>`)에 남는다.
- 주문은 shop-api에서 읽는다(`shop_client.py`, `GET /api/shop/orders/{id}`, 결제가 끝난 주문만). 목 모드는 시드 주문을 쓰고, `SUPPORT_ORDER_SOURCE=shop-api`로 목 모드에서도 실제 shop-api를 쓸 수 있다. 환불 지급은 `refund_request`가 `executed`일 때만 shop-api 환불 API(`POST /payments/{id}/refund`, support 토큰)를 부른다. 키는 클레임 단위로 고정이라 실패하거나 응답이 없으면 그 건은 `held`(`shop_refund_failed:<코드>`)로 남고, 다시 처리하면 같은 키로 재시도해 중복 환불되지 않는다. 시드 주문은 결제 ID가 없어 환불 API를 부르지 않는다. 주소는 `AX_SHOP_API_URL`(기본 `http://shop-api:8010`)이다.
- `create_claim`·`decide_claim`·`receive_return`·`inspect_return`은 safety 레지스트리 등록이 필요하다 (`to:safety`). 미등록이면 C로 보류된다.
- 저장소는 `SUPPORT_PG_DSN`이 없으면 프로세스 메모리다(재시작하면 사라진다). 있으면 PostgreSQL에 저장한다 (아래 "영속 저장소").
- 정책 문서(KB)는 시드 조각이다. memory의 정책 문서 API가 생기면 교체한다.
- 외부 API를 직접 호출해 상태를 바꾸지 않는다.
