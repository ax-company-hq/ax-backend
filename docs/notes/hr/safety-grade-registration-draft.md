# hr 안전 등급 신고표 (초안, 태윤에게 보낼 것)

> 상태: **초안**. 아직 `safety/src/ax_safety/rules/registry.toml`에 반영하지 않았다(그 파일은 사람이 PR로만 바꾼다).
> hr 쪽 목 구현의 등급은 `agents/hr/src/hr_agent/clients/safety.py`의 `HR_ACTIONS`에 같은 값으로 들어 있다.

## 1. 신고할 액션 (owner = hr)

| 액션 | 기본 등급 | 검증자 | 이유 |
|---|---|---|---|
| `memory_write_card` | A | — | 카드 쓰기·대체 연결. 기록이 목적이고 되돌릴 수 있다(상태만 `superseded`) |
| `record_case` | A | — | **이미 등록됨.** 결정 없는 기록만 저장하는 경로 |
| `start_handover` | A | — | 초안 생성만 한다. 외부에 영향 없음 |
| `answer_handover_question` | A | — | 인계자 답변 저장 |
| `request_assignment_change` | A | — | 요청 접수와 확인 목록 생성. 권한 목록은 전달만 한다 |
| `start_onboarding` | A | — | 읽을 순서 목록 생성 |
| `confirm_handover` | **B** | planning (`verify_handover`) | 확정 전에 진행 중 항목이 인계 목록에 빠짐없이 있는지 교차검증 |
| `confirm_handover_incomplete` | **C** | — | 완성도가 기준(제안 80%) 미만이면 사람이 보고 결정 |
| `confirm_reassignment` | **B** | 코드 규칙 (아래 질문 1) | 담당 공백·중복이 없을 때만 적용, 적용 뒤 재검증 |
| `confirm_offboarding` | **C** | — | 퇴직 확정은 되돌리기 어려워 항상 사람 승인 |

미등록 액션은 safety가 C로 보류하므로, 위 액션을 먼저 등록하지 않으면 hr의 쓰기는 모두 보류된다.

## 2. registry.toml 제안 항목

```toml
# ---- 인사 hr (준하)
[[actions]]
action = "memory_write_card"
owner = "hr"
base_grade = "A"

[[actions]]
action = "start_handover"
owner = "hr"
base_grade = "A"

[[actions]]
action = "answer_handover_question"
owner = "hr"
base_grade = "A"

[[actions]]
action = "request_assignment_change"
owner = "hr"
base_grade = "A"

[[actions]]
action = "start_onboarding"
owner = "hr"
base_grade = "A"

[[actions]]
action = "confirm_handover"
owner = "hr"
base_grade = "B"
verifier = "planning"
verify_skill = "verify_handover"

[[actions]]
action = "confirm_handover_incomplete"
owner = "hr"
base_grade = "C"

[[actions]]
action = "confirm_reassignment"
owner = "hr"
base_grade = "B"
# verifier: 아래 질문 1 참고

[[actions]]
action = "confirm_offboarding"
owner = "hr"
base_grade = "C"
```

## 3. 태윤에게 물어볼 것

1. **`confirm_reassignment`의 검증자.** 변경 뒤 담당 공백·중복이 없는지를 코드 규칙으로 확인하는데, 레지스트리는 `verifier`가 owner와 달라야 하고 에이전트 skill을 부르는 형식이다. 후보: (a) safety 자체 코드 규칙으로 등록(`pr_changed_lines`처럼 파라미터 규칙으로 — 입력 `ops`에서 영역별 owner 수를 센다), (b) hr가 검증 skill을 따로 열어 safety가 호출. hr 쪽 목 구현은 (a)와 같은 방식으로 `ops`를 받아 계산한다.
2. **C 보류 사유.** 기준 금액이 없는 C 액션에 쓸 `HoldReason`이 마땅치 않아 목에서는 `over_threshold`를 임시로 쓴다. `high_impact`(가칭) 같은 값을 추가할지.
3. **보류 큐 표시 문구.** 보류 건에 사람이 읽을 `summary`(예: "퇴직 확정 요청: emp_01 (payment)")를 어디에 담아 보낼지. 목은 `params.summary`를 쓴다 (재현·태윤에게 요청 중인 내용과 같음).
4. **완성도 기준 80%.** 제안값이다. 기준표(`thresholds.toml`)로 둘지 hr 설정으로 둘지.
5. **다단계 승인.** 퇴직은 `confirm_reassignment`(B) 후 `confirm_offboarding`(C) 두 번이 필요하다. C 승인 뒤 같은 `idempotency_key`로 다시 요청하면 `executed`를 받는 흐름(safety README)을 그대로 쓴다.
