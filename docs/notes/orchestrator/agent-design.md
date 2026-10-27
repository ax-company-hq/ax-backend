# orchestrator 에이전트 설계

실제 회사의 이 부서: 운영 총괄(관리자) — 인시던트(사건) 관리 센터. 신호 접수, 사건 판단, 담당 부서 배정, 진행 모니터링, 사건 종료와 보고
벤치마킹 솔루션: (제안, 사람이 확인) PagerDuty, Opsgenie, ServiceNow 인시던트 관리. 부서끼리의 사건 흐름을 한곳에서 이어 주는 점이 닮았다
협업 부서: dejafix, data, planning, marketing, support, finance, hr, safety

1. 누구의 어떤 일을 대신하나 (이 부서 고유 업무 3가지):
   - 신호를 받아 사건을 열고 합친다: `shop.degraded`·`infra.anomaly_detected`는 사건으로, `metric.anomaly`는 severity가 high 이상일 때만 사건으로 연다. 같은 구성요소의 열린 사건이 있으면 새로 열지 않고 합친다. 사건마다 `ctx_inc_` 번호를 발급한다.
   - 사건 계획(plan, 시나리오 A: 결제 장애)대로 부서에 맡기고 모은다: 진단 → 영향 분석 → 롤백·관련 PR 연결·점검 공지 → 영향 고객 일괄 보상 → 복구 확인 → 정상화 공지 → 처리 시간 요약 → 사건 기록(13단계). 재무는 사건 중에 부르지 않는다(멘토 피드백 2026-10-26). 병렬·직렬 실행, 재시도, 이관 큐, `input-required` 중계를 한다.
   - 사건을 닫고 기록한다: `incident.resolved`를 내고 hr에 결정 카드 기록을 요청하며, 관리자 화면이 읽는 사건 현황 API를 제공한다. 총괄 자신의 `/a2a`로 다른 부서의 사건 조회에도 답한다.
2. 스스로 판단하는 것 / 사람(safety 큐)에게 넘기는 것:
   - 스스로(코드 규칙): 사건 열기·합치기, 맡길 부서 고르기(Agent Card), 재시도, 이관, 앞 단계 결과로 단계를 건너뛰기(원인 미확정이면 롤백 안 함, 영향 고객이 없으면 보상 요청 안 함, 복구 확인 실패면 정상화 공지 안 함).
   - 사람에게: C 등급 보류(롤백, 한도 넘는 쿠폰)는 총괄이 승인하지 않고 안전 관문 보류 큐의 사람 결정을 기다리며 중계만 한다. 맡을 부서가 없으면 이관 큐로 넘긴다. 원인 미확정·복구 확인 실패도 사람이 판단한다.
3. 다른 부서에게 받는 요청 (누가·언제·무엇) = 내 skill 목록:
   - 이벤트 · shop-api(`shop.degraded`·`shop.recovered`), data(`metric.anomaly`), dejafix(`infra.anomaly_detected`), support(`support.inquiry_received`·`support.inquiry_surge`) · 사건 판단
   - support · 문의를 접수할 때 · `find_active_incident` (구현됨, 문의가 진행 중인 사건 번호를 이어받게 함. 응답에 점검 공지 번호 `notice_id`도 담는다)
   - (예정) finance, support · 이상을 찾았을 때 · 이상 보고 접수 (총괄이 받아 사건으로 열고 담당 부서에 배정, 계약 선행)
   - finance(평상시) · 사후 감사에서 사건 구간을 알아야 할 때 · 총괄 `GET /api/incidents`를 읽기 전용으로 읽는다(`context_id`·`component`·`status`·`opened_at`·`closed_at`만, 새 skill 없음, 사건 중에는 부르지 않음). #417에서 합의, 접근 형식은 재무가 `contracts/data-access/`에 추가한다
   - (예정) support · 문의를 닫았을 때 · 해결 보고 `support.inquiry_resolved` (계약 #327)
4. 다른 부서에게 하는 요청 (누구에게·무엇을) = 필요한 skill 목록:
   - dejafix · `diagnose_incident`, `rollback_deployment`(B), `verify_recovery`
   - data · `impact_analysis`, `kpi_summary`
   - planning · `find_related_prs`, `link_incident`
   - marketing · `draft_notice`, `publish_notice`
   - support · `request_incident_compensation` (영향 고객 일괄 보상)
   - hr · `record_case` (사건 종료 때 1회), 담당자 교대 시 `request_assignment_change`·`start_handover`
5. 기억해야 하는 것 (memory 부서와 연결):
   - 사건·단계·보류 상태는 지금 총괄 메모리에만 있어 재시작하면 사라진다. 저장·복구는 아직 정하지 않았다.
   - 사건 종료 때 원인·조치·결정 이유를 hr `record_case`로 보내 회사 기억에 남긴다.
6. 잘하는지 보는 지표:
   - 사건 처리 시간(`mttr_seconds`), 단계 완료 수, 자동 처리율
   - 사람 승인 대기 수, 이관 수, 재시도 수
7. 하지 않는 일 (다른 부서·시스템 규칙의 일):
   - 분석 문장·금액·등급을 만들지 않는다. 숫자는 data·finance가, 등급은 safety가 정한다.
   - C 등급을 승인하지 않는다. 사람이 안전 관문 보류 큐에서 한다.
   - 부서 DB를 직접 건드리지 않고, 외부 텍스트를 지시로 해석하지 않는다.
   - 문의 경로의 보상·쿠폰은 고객지원이 직통으로 처리한다 (총괄은 영향 고객 일괄 보상만 요청한다. 결정 기록 참고).
   - 부서끼리 맡기는 일과 교차검증(예: 롤백 → 기획개발 `verify_rollback_target`)에는 끼지 않는다.
