# 고객지원 AI 고도화: 다른 기업이 요구하는 기능 조사 (2026-10-27)

> 목적: 고객지원 AI 플랫폼을 도입하는 기업들이 공통으로 요구하는 기능을 찾아, 우리 고객지원 에이전트의 다음 작업 후보를 정한다.
> 방법: 웹 검색(2026-10-27). 출처 대부분이 **벤더(공급사)가 쓴 구매 가이드**라서 순위·성과 수치(해결률 70~90% 등)는 검증되지 않았다. 여러 출처에서 **반복해서 나온 기준**만 골랐다.
> 상태: 조사 메모다. 구현 결정은 아니고, 어느 것부터 할지는 담당(병환)이 정한다.

## 한눈에 보기

| # | 기능 | 기업이 요구하는 이유 | 우리 상태 | 첫 단계 제안 |
| --- | --- | --- | --- | --- |
| 1 | 사람 이관(핸드오프) 품질·에스컬레이션 규칙 | 사람이 받을 때 맥락이 끊기면 재문의·중복 처리가 생긴다 | 요약·`escalated` 상태는 있으나 "AI가 이미 한 조치"를 구조화해 넘기지 않는다 | `summarize_inquiry`에 이미 한 조치·이관 사유 코드를 코드가 채움 |
| 2 | 지식 공백 탐지·자동 QA | 사람 QA는 샘플링뿐이라 AI 결정 전수 점검과 문서 보강 근거가 필요하다 | 채택률·가드 적중·`reject_draft` 데이터는 쌓이나 분류별 집계가 없다 | 분류별 "근거 없음·초안 거절·이관" 비율을 세는 읽기 skill |
| 3 | 개인정보·규제 대응의 증빙(감사 기록) | GDPR·EU AI Act 대응은 건별 기록이 증빙의 핵심이다 | 마스킹·가드·근거 첨부·삭제 요청 1단계(#438)는 있으나 마스킹 조치 기록·보유기간 삭제가 없다 | 마스킹 결과(종류·건수, 원문 제외)를 문의별 감사 기록으로 남김 |

참고: "행동하는 AI"(환불·쿠폰 같은 실제 처리)는 요즘 기본 조건인데, 우리는 `execute()` 관문·멱등 키·안전 등급으로 이미 충족한다. 그래서 위 세 가지를 우선 후보로 뽑았다.

## 1. 사람 이관 품질과 에스컬레이션 규칙

**조사에서 나온 요구**
- 사람이 받을 때 대화 전체, 고객의 의도, **AI가 이미 한 조치**, 추천 다음 단계를 한 번에 볼 수 있어야 한다. 숨은 조치가 있으면 환불이 중복될 수 있다.
- 이관 사유는 "복잡함" 같은 일반 문구가 아니라 구체적이어야 하고, 스킬·언어·등급에 따라 라우팅한다. 사람이 해결한 뒤 AI가 대화를 되돌려 받는 흐름도 거론된다.
- 자동 이관 트리거: 고객이 사람을 **명시적으로 요구**할 때, 같은 주제가 **3회 반복**될 때. 규칙은 과거 티켓 100~200건으로 오탐을 시험하고, 보수적으로 시작해 매주 오탐을 검토한다.
- 성과는 이관 비율이 아니라 **재문의율**로 잰다. 이관 시 구조화된 요약, 상품 식별자·오류 메시지 원문 유지가 QA 점검 항목이다.

**우리 상태와 간극**
- 있음: `summarize_inquiry`(요약·다음 조치·사람에게 넘길 필요), `set_inquiry_status`(`escalated`), 문의에 연결된 클레임·쿠폰(`inquiry_resolved` 이벤트의 `claim_id`·`claim_decision`·`refund_amount`).
- 없음: 이관할 때 "이미 한 조치" 목록(클레임 판정, 보상 요청, 결제 진단 결과)을 구조화해 함께 넘기기, 이관 사유 코드, 반복·명시 요청 트리거, 재문의율 지표.

**첫 단계**
- `summarize_inquiry` 출력에 `actions_taken[]`(코드가 클레임·보상·진단 기록에서 채움)와 `escalation_reason_code`를 더한다. LLM이 쓰는 문장이 아니라 코드가 센 사실이다.
- 반복 트리거는 같은 구매자의 같은 분류 문의 3회(저장소만 본다)로 시작한다.

## 2. 지식 공백 탐지와 자동 QA

**조사에서 나온 요구**
- 에스컬레이션을 "문서가 부족하다는 증거"로 보고, 어떤 근거 문서가 이관으로 이어졌는지 모아 **영향이 큰 공백부터** 보여 준다.
- 사람 QA는 일부만 샘플링하지만 AI는 **모든 대화**를 점검할 수 있다. 자동 점검은 빠진 정보·잘못된 조치는 잘 잡지만 "고객을 제대로 이해했는가"는 약해서, 사람 점검(의도 해석·라우팅·다음 단계에 대한 신뢰)과 병행하라고 한다.
- AI 결정도 사람 상담원처럼 검토할 수 있어야 한다: 대화 단위 점수, 결정 로그, 내보낼 수 있는 감사 기록.

**우리 상태와 간극**
- 있음: 초안 채택률·수정 정도·가드 적중·분류별 건수(`inquiry_overview`), 상담원이 거절한 초안 기록(`reject_draft`), 근거 검색 결과(근거 문서 ID).
- 없음: 분류·근거 문서별로 "근거 없음(검색 0건)", "초안 거절", "이관" 비율을 묶어 보여 주는 집계, 공백 순위.

**첫 단계**
- LLM 없이 코드로 세는 읽기 skill(`inquiry_overview` 확장). 대시보드 고객지원 AI 탭에 표로 붙일 수 있다.
- 자동 QA 점수는 이번 단계에서 만들지 않는다(사람 점검 병행 원칙, 오탐 검증 필요).

## 3. 개인정보·규제 대응의 증빙

**조사에서 나온 요구**
- 모델 호출 전에 게이트웨이에서 개인정보를 가리고(식별자를 자리표시자로 교체), **무엇을 어떻게 가렸는지 재생 가능한 기록**을 남긴다.
- 건별로 시각, 판단 경로, 쓴 모델, 근거 출처를 기록하는 것이 GDPR 처리 기록(Art. 30)과 EU AI Act 대응의 핵심 증빙으로 거론된다. 보존기간과 삭제 요청 처리도 같이 요구한다.
- 이 영역의 문구는 **제품 주장**일 뿐 규제 준수 보증이 아니다. 법령 원문(GDPR 조문, EU AI Act 위험 분류)은 이 조사에 없으니 법무·변호사 확인이 필요하다.

**우리 상태와 간극**
- 있음: 개인정보 마스킹(`guard.mask_pii`), 답변 가드, `evidence`(근거 첨부), 공통 로그, `privacy_deletion` 분류와 본인 1차 판단 skill(#438, 1단계), DejaFix로 보내는 개인정보 암호화(`secure_transfer`).
- 없음: 문의별 마스킹 결과 기록(종류·건수), 문의 원문 보유기간 삭제(3개월, 처리 중 5년 — #430 3단계), 근거 출처를 답변 단위로 남기는 감사 조회.

**첫 단계**
- 마스킹 결과(탐지 종류·건수·조치, **원문 제외**)를 문의별 감사 기록으로 남긴다. #430 3단계(보유기간 삭제)와 한 묶음으로 설계한다.

## 정하는 순서(제안)
1. **1번 → 2번 → 3번** 순서를 권한다. 1번은 이미 있는 연결(클레임·쿠폰)을 활용해 비용이 가장 작고, 2번은 대시보드 화면과 묶으면 시연에 잘 보이며, 3번은 #430 후속 단계와 같이 가는 편이 효율적이다.
2. 각 기능은 새 skill이거나 기존 skill의 출력 필드 추가이고, 계약 변경이 필요한 경우(예: 이관 사유 코드를 이벤트로 내보내기)는 `contracts/`에 추가 요청을 먼저 올린다.
3. 시연 필수 범위 밖이면 설계 메모로 두고 구현은 후순위로 한다.

## 출처
- [Best AI Customer Support Software for Enterprise Teams in 2026 (Opensend)](https://www.opensend.com/post/ai-customer-support-software-enterprise-teams)
- [Choose an AI customer support platform: human-AI collaboration (Fini)](https://www.usefini.com/guides/choose-ai-customer-support-platform-human-ai-collaboration)
- [AI customer support platforms: handoff quality and context preservation (Fini)](https://www.usefini.com/guides/ai-customer-support-platforms-handoff-quality-context-preservation)
- [Human agent handoff (Voiceflow)](https://www.voiceflow.com/blog/human-agent-handoff.md)
- [Knowledge gap detection (Open.cx)](https://open.cx/knowledge-gap-detection)
- [AI-driven QA in customer service (NICE)](https://nice.com/info/ai-driven-qa-in-customer-service-enhancing-support-quality)
- [Closing the AI communication gap with customer service QA (Petronella Tech)](https://petronellatech.com/blog/closing-the-ai-communication-gap-with-customer-service-qa/)
- [AI to human handoffs should feel like one conversation (CX Today)](https://cxtoday.com/ai-to-human-handoffs-should-feel-like-one-conversation-so-why-are-han)
- [How to detect a customer needs a human agent (Typewise)](https://www.typewise.app/blog/ai-detect-customer-needs-human-agent)
- [Intelligent support agent handoff (Halo AI)](https://haloagents.ai/blog/intelligent-support-agent-handoff)
- [How AI agents handle PII (Fin)](https://fin.ai/learn/ai-agents-pii-data-security)
- [Best AI support platforms with PII redaction and data masking (Lorikeet)](https://www.lorikeetcx.ai/articles/best-ai-support-pii-redaction-data-masking-2026)
- [Contact center compliance (FutureAGI)](https://futureagi.com/glossary/contact-center-compliance/)
