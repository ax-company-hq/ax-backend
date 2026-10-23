# #190 기획개발 C 건 액션별 검토자 (보류, 선행 필요)

요청: `confirm_spec` → hr(`check_plan`), `merge_pr` → data + orchestrator(전원 승인). 자기 승인 금지, 마감 초과·escalate는 사람, 되돌릴 수 없는 일·보호 경로는 검토자 없이 항상 사람.

## 지금 코드 상태 (2026-10-23)
- `safety/` 안에 `auto_review`가 없다. `thresholds.toml`에 `[auto_review]` 표도 없고 `gate.py`에도 검토자 처리가 없다. 요청의 "#110 코드"는 이 브랜치에 없다.
- 그래서 "확장"할 대상이 없다. 1차 판단 자체를 새로 만들어야 하는데, 검토자 입출력(요청·응답·마감·escalate)은 부서 간 계약이라 `contracts/`에 먼저 정해져야 한다.

## 설계 메모 (계약이 정해지면 구현)
- 검토자 목록은 `thresholds.toml`에 액션별로 둔다(사람만 변경). 목록이 여럿이면 전원 승인, 한 명이라도 거절·escalate·무응답이면 사람(C 보류 유지).
- 검토자에서 요청 부서(planning)는 코드로 제외한다(자기 승인 금지).
- 규칙에 `irreversible`·`merge_irreversible`·`merge_allowed_paths`가 걸린 건은 검토자를 거치지 않고 사람으로 보낸다.
- 안전 레이어·검토자가 응답하지 않으면 실행하지 않는다(fail-closed). 검토 결과는 `evidence`와 함께 감사 로그에 남긴다.
- 학습(C → B)은 이 변경에 포함하지 않는다(#124 방향).
