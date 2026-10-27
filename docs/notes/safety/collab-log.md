# safety 협업 메모

- 2026-10-26 hr · 제거된 hr skill 5개 등급 등록 정리 요청 (#375) · 4개는 이 PR에서 정리, `confirm_reassignment`는 `thresholds.toml` 규칙(사람 승인 파일)과 함께 지워야 해서 사람 PR로 남김
- 2026-10-26 agent-template · LLM 사용량 기록 (#288) · `LogKind` 확장은 호환성 검사가 막아 계약 `llm_usage`로 변경 (#371, #380 머지)
- 2026-10-26 support · `support.inquiry_resolved` 이벤트 (#327) · 이벤트 종류 추가용 검사기 예외(#396)가 머지돼 #383 진행 중
- 2026-10-26 dashboard-api · 고객관리 읽기 API 6개 (#388), 쓰기 사람 토큰 (#381) · 승인, openapi 재생성 (#390)
- 2026-10-27 shop-api · `approve_data_deletion` 등급 등록·보류 사유 질문 5개 (요청 #431) · 2·3·5번 답하고 `high_impact` 보류 사유 추가(PR), 1번은 registry.toml 사람 확인, 4번(승인 기한)은 사람에게 넘김
