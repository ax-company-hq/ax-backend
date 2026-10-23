# #228 시나리오 A 보상 쿠폰 호출 주체 — 마케팅 입장

## 의견: A (고객지원이 문의 이벤트로 처리) 찬성
- 근거: DEP-CS-02(docs/requirements.md:121), DEP-MKT-02(:124), 결정 9(docs/agent-common.md:351)는 "판정은 support, 지급은 marketing `issue_coupon(compensation)`, support 요청은 직렬 + `sequence`"로 이미 합의돼 있다. 총괄이 `issue_coupon`을 따로 부르면 이 결정과 어긋나고 이중 지급 위험이 있다.
- 마케팅 `issue_coupon`은 구매자 1명 단위(`buyer_id`, `count`, `unit_value`, `purpose_code`)라 총괄이 건건이 부르는 C는 정책표 우회가 되어 반대한다. 재무는 `compensation`이면 `buyer_id`가 data 영향 고객이어야 검증한다.
- B는 고객지원 skill 추가가 필요하지만, 마케팅 쪽 경로는 A와 같다 (지급 요청은 고객지원에서만 온다).

## 질문 2: `sequence`
- 지금처럼 고객지원이 사건(`context_id`)별로 1부터 붙이는 것에 동의한다. 마케팅은 번호를 검증만 하고 붙이지 않는다 (번호 소비 기준은 agents/support/README.md:38과 같다).

## 마케팅 쪽 변경
- 마케팅 코드 변경은 없다. 총괄은 공지 게시(`publish_notice`) 후 `incident.opened`에 `notice_id`만 싣는다.
- 캠페인 쿠폰(`purpose_code=campaign`)은 이번 결정과 무관하게 그대로 둔다.
- `issue_coupon` 카드의 요청 허용 목록(agents/marketing/README.md:18)에 `orchestrator`가 있다. A로 확정되면 `compensation` 목적은 `orchestrator` 요청을 받지 않도록 줄이는 것을 사람이 판단해 달라 (카드 변경은 합의 후 후속 PR).
