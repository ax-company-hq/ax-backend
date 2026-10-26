# marketing 에이전트 설계
1. 누구의 어떤 일을 대신하나 (하루 일 3가지):
   - 장애가 났을 때 점검·정상화 공지를 사실 필드로 만들어 게시한다 (`draft_notice` → `publish_notice`, 총괄이 부른다).
   - 고객지원이 요청한 보상 쿠폰을 발급한다 (`issue_coupon`).
   - (설계 단계, 목 모드) AI 가상 인플루언서의 쇼츠 영상을 수집·기획·제작·평가·보고한다 — `docs/notes/marketing/ai-influencer-pipeline.md`.
2. 스스로 판단하는 것 / 사람(safety 큐)에게 넘기는 것:
   - 스스로: 공지 초안 작성(읽기), 공지 게시(A 등급), 정해진 템플릿 안의 문안.
   - 넘김: 쿠폰 발급은 B이고 기준 이상이면 C로 사람에게 간다. 금액 교차검증과 등급 판정은 마케팅이 하지 않고 safety·finance가 한다. 쇼츠 제작(B)·게시(C)는 계획대로 safety 기준표에 등록해야 한다.
3. 다른 부서에게 받는 요청 (누가·언제·무엇) = skill 목록:
   - orchestrator · 장애 시나리오 A · `draft_notice`, `publish_notice`, `get_notice`
   - support · 보상 쿠폰이 필요할 때 · `issue_coupon`
   - (예정) 쇼츠 파이프라인 skill — `produce_short` 등
4. 다른 부서에게 하는 요청 (누구에게·무엇을):
   - finance · 쿠폰 금액 검증 (`issue_coupon`의 검증자)
   - safety · 액션 등급 등록과 비용 상한 기준값 (쇼츠 도입 시 `to:safety`)
   - 이벤트 발행: `notice.published`, `coupon.issued`. 실제 Redis 발행은 아직 구현되지 않았다 (agent-template `publish`가 TODO, #294).
5. 기억해야 하는 것 (memory 부서와 연결):
   - 공지 초안·게시 기록, 쿠폰 발급 기록 (지금은 메모리에만 있고 재시작하면 사라진다 — memory 부서 연결은 정해야 한다).
   - (예정) 쇼츠 후보·평가·비용 기록.
6. 잘하는지 보는 지표:
   - 공지: 장애 시작에서 게시까지 걸린 시간, 게시 거절 건수.
   - 쿠폰: 발급 요청 수, 보류(C)·거절 비율.
   - (예정) 쇼츠: 하루 후보 수·제작 수·평균 점수·비용 (`report_daily`).
