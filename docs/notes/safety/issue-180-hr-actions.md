# #180 hr 액션 9개 등록과 답변

## 반영한 것
- `registry.toml`: hr 액션 9개 등록 (owner = hr)
  - A: `start_handover`·`answer_handover_question`·`request_assignment_change`·`start_onboarding`·`update_onboarding_step`
  - B: `confirm_handover`(planning, `verify_handover`), `confirm_reassignment`(safety 코드 규칙)
  - C: `confirm_handover_incomplete`, `confirm_offboarding`
- `thresholds.toml` 규칙 3개 (제안값, version은 올리지 않음 → 확인 후 사람이 올린다)
- `gate.py`: 보류 보고서에 `params.summary`를 `summary`로 담는다 (문자열, 200자까지)
- 테스트: `safety/tests/test_hr_actions.py`

## 답
1. `confirm_reassignment` 검증자: **안전 레이어 코드 규칙**으로 한다. 새 규칙 종류는 필요 없다.
   기존 `param` 규칙으로 `unassigned_count > 0`, `duplicate_count > 0`이면 C. 필드가 없거나 정수가 아니면 C(fail-closed).
   두 건수는 hr이 읽기 전용 데이터로 계산해 params에 넣는다. safety가 다시 계산하지는 않는다(독립 검증은 아님).
   독립 검증이 필요하면 hr 밖 부서가 검증 skill을 열어야 하며, 그때 verifier를 바꾸는 PR을 따로 낸다.
2. 기준 금액 없는 C의 `HoldReason`: 지금은 `over_threshold`로 나가고 보고서 `rules`에 `{"key": "base_grade", "value": "C"}`가 남는다.
   `high_impact` 추가는 `contracts/` 변경이라 contracts에 요청했다. 추가되면 `gate.py`에서 기본 등급 C일 때 그 값을 쓴다.
3. 설명 문구: `params.summary` 그대로 맞다. 보류 보고서 `summary`에 담긴다(공백 제거, 200자 초과분 자름).
   요청 쪽 데이터이므로 untrusted로 다루고, 화면에서는 텍스트로만 보여준다.
4. 완성도 기준: safety `thresholds.toml`에 둔다 (`handover_completeness`, `completeness_percent < 80`이면 C).
   hr은 `completeness_percent`(0~100 정수)를 `confirm_handover` params에 넣어야 한다. 없으면 보류된다.
   `confirm_handover_incomplete`는 등급 C 고정으로 같이 유지한다.
5. 무응답 처리: 가정이 맞다. 안전 레이어가 응답하지 않으면 템플릿 `execute()`가 `exe_unavailable`을 돌려주고 safety는 아무것도 승인하지 않는다.
   safety가 사람 거절·검증 거절을 알리는 것은 `hold_reason`이 `verify_rejected`·`review_timeout`일 때뿐이다. 사유 없는 `rejected`는 safety가 판정한 것이 아니다.
   (감사 로그 실패 시 safety 자신도 사유 없는 `rejected`를 돌려준다. 이때도 실행하지 않는다.)
6. 보류 기한: 맞다. hr은 따로 기한을 두지 않고 `review_timeout`을 따른다. 현재 `review_timeout_seconds`는 600초이며, 24시간 변경은 #99 몫이라 여기서 바꾸지 않았다.
