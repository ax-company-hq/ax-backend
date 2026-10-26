# marketing 에이전트 설계
- 실제 회사의 이 부서: e커머스의 마케팅팀(CRM·커머스 마케터). 고객에게 알리고(공지·안내), 혜택을 주고(쿠폰), 캠페인과 SNS 콘텐츠를 운영하고, 성과를 본다. 캣북스에서는 장애 공지, 보상 쿠폰 발급, 공개 쿠폰 캠페인과 Instagram 게시가 이 부서 일이다.
- 벤치마킹 솔루션: 배달의민족 CRM 마케터, 이베이재팬 CRM Planning Specialist, i-boss 커머스 마케터 공고에서 업무 기술 26개를 뽑아 7개 분류로 정리했다 (2026-10-26, 무신사·29CM·쿠팡 공고는 확인하지 못함). 구현된 것은 공지 3개(`draft_notice`·`publish_notice`·`get_notice`), 쿠폰 발급 1개, 공지 찾기 1개(`list_active_notices`)이고 캠페인·SNS·성과 보고는 예정이다. 정리본 v2는 #2-총괄에이전트 채널(2026-10-26)에 공유했다.
- 협업 부서: support (보상 쿠폰 요청, 게시 중 공지 조회), finance (쿠폰 금액 교차검증), orchestrator (장애 공지 요청), safety (등급·상한 기준), data (캠페인 성과 측정, 예정), memory (기록 보관, 연결 미정).

1. 누구의 어떤 일을 대신하나 (하루 일 3가지):
   - 장애가 났을 때 점검·정상화 공지를 사실 필드로 만들어 게시한다 (`draft_notice` → `publish_notice`, 총괄이 부른다).
   - 고객지원이 요청한 보상 쿠폰을 발급한다 (`issue_coupon`).
   - (설계 단계, 목 모드) AI 가상 인플루언서의 쇼츠 영상을 수집·기획·제작·평가·보고한다 — `docs/notes/marketing/ai-influencer-pipeline.md`.
2. 스스로 판단하는 것 / 사람(safety 큐)에게 넘기는 것:
   - 스스로: 공지 초안 작성(읽기), 공지 게시(A 등급), 정해진 템플릿 안의 문안.
   - 넘김: 쿠폰 발급은 B이고 기준 이상이면 C로 사람에게 간다. 금액 교차검증과 등급 판정은 마케팅이 하지 않고 safety·finance가 한다. 쇼츠 제작(B)·게시(C)는 계획대로 safety 기준표에 등록해야 한다.
3. 다른 부서에게 받는 요청 (누가·언제·무엇) = skill 목록:
   - orchestrator · 장애 시나리오 A · `draft_notice`, `publish_notice`, `get_notice`, `list_active_notices`
   - support · 보상 쿠폰이 필요할 때 · `issue_coupon` (장애 사건에서는 영향 고객마다 `sequence` 1부터 이어서 요청)
   - support · 공지 번호를 모르는 사건 문의일 때 · `list_active_notices`
   - finance · 공지의 사실·기간을 확인할 때 · `get_notice`, `list_active_notices`
   - (예정) 캠페인 skill — `draft_campaign`, `create_campaign`, `campaign_report`
   - (예정) 쇼츠 파이프라인 skill — `produce_short` 등
4. 다른 부서에게 하는 요청 (누구에게·무엇을):
   - finance · 쿠폰 금액 검증 (`issue_coupon`의 검증자)
   - safety · 액션 등급 등록과 비용 상한 기준값 (쇼츠 도입 시 `to:safety`)
   - (예정) data · 캠페인 성과 측정값
   - 이벤트 발행: `notice.published`, `coupon.issued`. 실제 Redis 발행은 아직 구현되지 않았다 (agent-template `publish`가 TODO, #294).
5. 기억해야 하는 것 (memory 부서와 연결):
   - 공지 초안·게시 기록, 쿠폰 발급 기록 (지금은 메모리에만 있고 재시작하면 사라진다 — memory 부서 연결은 정해야 한다).
   - (예정) 쇼츠 후보·평가·비용 기록.
6. 잘하는지 보는 지표:
   - 공지: 장애 시작에서 게시까지 걸린 시간, 게시 거절 건수.
   - 쿠폰: 발급 요청 수, 보류(C)·거절 비율.
   - (예정) 쇼츠: 하루 후보 수·제작 수·평균 점수·비용 (`report_daily`).
7. 하지 않는 일:
   - 장애 원인을 공지에 적지 않는다. 공지는 확인된 사실 필드로만 쓴다 (원인 분석은 인프라 몫).
   - 쿠폰 대상과 보상 단가를 정하지 않는다. 고객지원이 판정해 요청하고, 마케팅은 받은 값으로 총액(건수×단가)만 코드로 계산한다.
   - 쿠폰 금액을 검증하거나 등급을 판정하지 않는다 (finance·safety).
   - 효과 측정과 고객 세그먼트를 만들지 않는다 (데이터분석 몫).
   - 사람 승인 없이 쇼츠를 게시하지 않는다 (게시는 C).
   - 고객에게 자동 DM·메시지를 보내지 않는다 (Meta 정책상 동의가 필요해 후보로만 둔다).
