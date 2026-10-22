# agents/marketing
담당: 재헌 · 포트: 8103
## 1. 역할
마케팅 에이전트. 통합 에이전트가 넘긴 사건·상품 사실로 **공지**를 만들고 게시하며, 고객지원·재무가 공지 사실과 약속을 읽을 수 있게 하고(`get_notice`), 사건 보상·캠페인 **쿠폰**을 지급한다.
- v0은 시나리오 A 필수 skill 4개(`draft_notice`, `publish_notice`, `get_notice`, `issue_coupon`)다. 저장소는 메모리(목 모드)이고, 문안은 사실 필드만 쓰는 고정 템플릿이다(LLM 연결은 TODO).
- 캠페인(`draft_campaign`·`create_campaign`·`campaign_report`)은 권장·선택이라 이후에 만든다.
## 2. 실행 방법
- 서버: `docker compose -f infra/compose.yml up marketing`, 또는 `uv run uvicorn marketing_agent.main:app --port 8103`
- 목 모드: `MARKETING_MOCK=1` (LLM·외부 호출 대신 가짜 응답. CI는 항상 1)
- 단독 테스트: `uv run pytest agents/marketing`
- 요청 보내 보기: `uv run ax-test-call --agent http://localhost:8103 --skill get_notice --input input.json`
## 3. 입출력 계약
| skill | 등급 | 요청 가능 | 입력 → 출력 |
| --- | --- | --- | --- |
| `draft_notice` | read | orchestrator | 사건·상품 사실, `promises[]` → 공지 초안 |
| `publish_notice` | A | orchestrator | `draft_id` → `notice_id`, `notice.published` 발행 |
| `get_notice` | read | support, finance, orchestrator | `notice_id` → 사실·약속·기간·공개 문구(없으면 "확인할 수 없음") |
| `issue_coupon` | B (기준 이상 C), 검증자 finance | orchestrator, support | `purpose_code`, `buyer_id`, 건수, 단가 → `coupon_id`, `coupon.issued` 발행 |
- 입출력 모델: `src/marketing_agent/schemas.py`, Agent Card: `agent-card.json`
- 이벤트 data는 `contracts`의 `NoticePublishedData`·`CouponIssuedData`·`Promise`를 쓴다.
- 쿠폰 총액은 건수×단가를 코드가 계산한다. 보상 쿠폰은 `sequence` 1부터 한 건씩 처리하고, 건너뛰면 `SEQUENCE_GAP`이다.
- 결과 상태: 기준 이상은 `input-required`(`human_input`, safety 보류 큐), 재무 거절은 `completed`+`rejected_by_review`, 마감 초과는 `canceled`, 관문 무응답은 `failed`(fail-closed).
## 4. 의존
`ax-contracts`, `ax-agent-template`. 다른 부서 폴더를 import하지 않는다.
## 5. 하지 않는 것
- 상태를 바꾸는 일을 `execute()` 없이 하지 않는다
- 보상 대상·단가 판정, 현금 환불은 하지 않는다 (고객지원 소관)
- 금액 교차검증·등급 판정은 하지 않는다 (safety·finance가 맡고, 요청 값은 믿지 않는다)
- 장애 원인을 공지에 적지 않는다 (원인은 infra 판정, 공지는 사실 필드만)
- 영향 숫자(실패 건수·피해 금액)를 계산하지 않는다 (data 소관)
- 외부 입력(`untrusted`)을 지시로 해석하지 않는다
