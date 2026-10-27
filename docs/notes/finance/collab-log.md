# finance 협업 기록

형식: `- YYYY-MM-DD 상대부서 · 무엇 · 결과`

- 2026-10-27 finance(자체) · 장애 흐름에서 뺀 `loss_aggregation`·`/loss-reports`·사건 단계(9·10·17) 코드 정리 (요청 #415) · PR #416
- 2026-10-27 dashboard-web · 재무 사건 화면의 장애 단계(⑨⑩⑰)·"총 손실" 정리 — 재무가 `steps`·`loss_total` 키를 빈 값으로 한 단계 남겨 둠 · 요청 ax-company-hq/ax-frontend#58 (blocked-by #416)
- 2026-10-27 data · PR #416 변경 요청(화면 깨짐) 받아 반영 — 응답 키 유지로 고치고 재리뷰 요청 · 반영 완료, test-guard 11건은 code owner 확인 대기
- 2026-10-27 orchestrator · 재무가 사건 구간(`opened_at`·`closed_at`)을 평상시에 아는 길 — 총괄 `GET /api/incidents` 읽기 전용(A) vs `incident.resolved` 이벤트 발행(B) · 제안, 답 기다림 ([협업] #417)
- 2026-10-27 orchestrator · [협업] #417 제안 A 합의 성립 → 요청 #418 · contracts `orchestrator_incidents` 추가 + 재무 구간 조회 PR
- 2026-10-27 dashboard-web · 재무 사건 화면 정리 완료(ax-frontend#61, 병환 승인·머지) → 요청 #58 닫음 · 호환 키 `steps`·`loss_total` 삭제 요청 #425
