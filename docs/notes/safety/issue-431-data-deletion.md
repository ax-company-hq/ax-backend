# 요청 #431 (shop-api → safety): 개인정보 삭제 승인 액션 등급 등록

담당: 태윤 (safety). 요청자: 재헌 (shop-api). 2026-10-27.

재헌이 `approve_data_deletion`(고객의 개인정보 삭제를 사람이 승인하는 액션)의 등급 등록과
질문 5개를 물었다. 이 문서는 safety 쪽 답과, 이번에 코드로 한 것·사람에게 넘긴 것을 적는다.

## 1. 등록 여부 → 등록한다 (단, 등록 자체는 사람 확인 뒤)

미등록이어도 C로 보류되지만 보류 사유가 `unregistered_action`이고 보고서에 owner·설명이 남지
않는다. 등록하면 `owner`·`base_grade`·`summary`가 보류 보고서에 분명히 남고, 요청자가
owner가 아닐 때 막히는 검사도 걸린다. 제안대로 `owner = support`, `base_grade = "C"`,
검증자 없음, 금액 없음이 맞다 (삭제 허용 여부는 늘 사람이 정하므로 A·B로 내려가는 조건을 두지 않는다).

**등록 줄 자체는 이 PR에 넣지 않았다.** `safety/src/ax_safety/rules/registry.toml` 머리말이
"사람만 바꾼다(PR)"로 적혀 있어 루프가 손대지 않는다 (`.github/team.yml`의 `human_files`에는
없지만 파일이 스스로 그렇게 적고 있다). 아래 줄을 사람이 넣으면 된다.

```toml
# ---- 고객지원 (병환). 개인정보 삭제는 되돌릴 수 없어 늘 사람이 정한다 (요청 #431)
[[actions]]
action = "approve_data_deletion"
owner = "support"
base_grade = "C"
```

4번(승인 기한)의 답에 따라 이 줄에 설정이 더 붙을 수 있으니, 4번을 정한 뒤 함께 넣는 쪽이 낫다.

## 2. 기준 금액 없는 C의 `HoldReason` → `high_impact`를 더했다 (이번 PR)

전에는 기본 등급이 C라서 보류된 건도 `over_threshold`("금액 기준을 넘음")로 나갔다. #180(hr)에서
같은 질문이 나와 `high_impact`를 더하기로 적어 뒀는데(`issue-180-hr-actions.md` 17~18줄) 실제로
들어가 있지 않았다. 이번에 넣었다.

- `contracts/.../schemas/execute.py`: `HoldReason.HIGH_IMPACT = "high_impact"` 추가 (추가만)
- `safety/.../gate.py`: 규칙에 걸려 C가 된 건은 `over_threshold`, 기본 등급이 C인 건은 `high_impact`.
  보고서의 `rules = [{"key": "base_grade", "value": "C"}]`는 그대로 남는다
- 영향 받는 기존 액션: `confirm_settlement`(finance), `confirm_handover_incomplete`(hr)
- 대시보드는 이미 `report.rules`의 `base_grade`를 먼저 보고 "항상 사람이 확인"으로 적고 있고
  모르는 사유에는 "확인 필요" 대체 문구가 있다 (`dashboard-web/.../SafetyQueue.tsx`). 깨지지 않는다
- `agents/hr/src/hr_agent/clients/safety.py`의 목은 아직 `over_threshold`를 돌려준다.
  hr 폴더라 건드리지 않았다 — 실제 관문과 맞추려면 hr(준하)이 바꾸면 된다

## 3. 설명 문구 위치 → `params.summary` 그대로 맞다

`gate.py`가 보류 보고서의 `summary`에 담는다 (공백 제거, 200자 초과분 자름).
#180에서도 같은 답이었다 (`issue-180-hr-actions.md` 9·19줄).

## 4. 승인 기한 → 사람이 정할 일 (루프가 정하지 않음)

지금은 기한이 지나면 자동 거절(`review_timeout`)이다. 삭제 요청을 자동 거절 대신 이관·재알림으로
바꾸는 것은 **사람 승인(safety) 기준**을 바꾸는 일이고, 재헌이 적은 대로 처리 기한이 법령으로
정해져 있을 수 있어 `[확인 필요]`다. 루프가 정하지 않고 code owner(재현·준하)와 태윤이 정한다.

정해야 하는 것:
- 자동 거절을 액션별로 끌 수 있게 할지 (레지스트리에 `on_review_timeout = "reject" | "transfer"` 같은 설정)
- 이관이면 누구에게 다시 알리는지 (대시보드 재알림인지, 총괄 이관 큐인지)
- 법령 기한이 있으면 그 기한을 어디에 적는지 (기준표는 금액용이다)

## 5. 실행 기록 → 승인 기록은 관문이, 집행 완료는 요청 쪽이 공통 로그에 남긴다

승인·거절은 관문이 실행 기록(`GET /api/safety/executions`)과 `safety.action_*` 이벤트로 남긴다.
실제 삭제는 운영자가 shop-api CLI로 하므로 관문 밖이다. 같은 사건으로 묶으려면 집행한 쪽이
같은 `context_id`로 공통 로그(`contracts/.../data_access/common_log.py`)에 한 줄 남기면 된다.
새 로그 종류가 필요하면 `LogKind` 추가는 contracts 요청으로 받는다 (추가만).

관문이 삭제 집행 자체를 확인하지는 않는다. 집행이 관문 밖이라는 것이 이 흐름의 전제다.
