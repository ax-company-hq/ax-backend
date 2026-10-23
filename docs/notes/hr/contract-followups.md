# hr 계약 후속 제안 (파일만, 계약·PR 없음)

hr 구현(`agents/hr`)이 현재 계약 안에서 우회한 곳과, 계약에 있으면 우회가 사라지는 곳을 모았다.
계약 변경은 계약 PR → 구현 PR 순서이고 소비자 파트 승인이 필요하다(`CLAUDE.md`). 아래는 **제안**이며 어느 것도 `contracts/`를 고치지 않았다.
우선순위: ① 다른 부서가 hr 카드를 쓰기 전에 필요 → ② 3단계(실제 연동) 전에 필요 → ③ 있으면 좋음.

## 0. 적용 상태 (2026-10-23, 로컬 브랜치 `feat/hr-dashboard-data`, PR 없음)

아래 표의 제안 중 **hr 쪽 계약은 이 브랜치에 이미 적용**해 hr 구현과 테스트(290개+)까지 맞춰 두었다. 다만 `contracts/` 변경이라 계약 PR로 올려 소비자 승인을 받아야 한다.

| 제안 | 적용 | 비고 |
|---|---|---|
| ① 1 `Card.status`에 `candidate` | ✅ | **호환성 검사가 "깨짐"으로 잡는다**(`Card`·`CardGetOutput`·`CardHit`·`CardLinkOutput`·`CardSearchOutput` 5건, 응답 enum 값 추가). 카드를 읽는 모든 소비자(memory·support·data·infra·planning)의 승인이 필요하다 |
| ① 3 `RecordCase.area` | ✅ | 선택 필드 추가라 호환 |
| ② 5 `ErrorCode.INVALID_INPUT` | ✅ | hr은 형식 위반을 `rejected`로 돌려준다 |
| ② 6 `ConfirmOffboardingOutput.status`에 `rejected` | ✅ | |
| ② 7 `ConfirmReassignmentOutput.status`에 `held` | ✅ | |
| ③ 11·12 온보딩 단계 완료 | ✅ | `update_onboarding_step`(입력 `UpdateOnboardingStepInput`, 출력 `StartOnboardingOutput`) 구현 |
| (새) 대시보드 인사 화면용 조회 | ✅ | `list_handovers`·`list_open_questions`·`list_onboardings`·`list_changes`·`list_candidate_cards`·`get_audit_timeline`, 사람 입력 `decide_candidate_card` — `hr.py`에 모델 추가, Agent Card는 18개 skill |
| ① 2 카드 구조화 필드(`area`·`outcome` 등) | ⬜ | 아직 `Card.extra` (memory와 정해야 함) |
| ① 4 `RecordCase.rejected`/`outcome` | ⬜ | 호출 쪽이 알려 주는 필드. 지금은 문장 추론(확신도 최대 mid) |
| ② 8·9 보류 `HoldReason`·`summary` | ⬜ | safety와 정함 |
| ③ 10 `ctx_hnd_` 접두사 | ⬜ | |
| ③ 14 답변 전달 경로 | ✅(결정) | dashboard-api가 hr을 A2A로 부른다(문의 API와 같은 방식). Agent Card에 `dashboard-api`를 읽기·답변·단계 완료·카드 결정 요청자로 넣었다 |
| memory 보강(후보 검색·영역 필터·목록·상태 변경) | ⬜ | `issue-drafts.md` 2번 |

PR을 나눌 때는 ① `Card`·`ErrorCode`·hr 모델(소비자 여럿) → ② hr 구현(`agents/hr`) 순서로 쪼갠다.

## ① 카드·기록 형식 (memory 재현, 태윤)

| # | 제안 | 지금 우회 방법 | 이유 |
|---|---|---|---|
| 1 | `Card.status`에 `candidate` 추가 (`active`·`candidate`·`superseded`·`retired`) | `status="active"` + `extra["candidate"]=true` + 태그 | 확신 낮은 카드를 다른 부서가 memory에서 읽을 때 확정 카드로 오해한다. `RecordCaseOutput` 문서도 `status=candidate`라고 이미 적고 있어 서로 맞지 않는다 |
| 2 | `Card`에 구조화 필드: `area`, `owner`, `alternatives_rejected`, `outcome`(`adopted`·`rejected`), `supersedes` | 모두 `Card.extra` | 계획 비교·인수인계·온보딩이 영역·기각 여부로 검색한다. memory 검색 인덱스에 영역·결과 필드가 필요하다 |
| 3 | `RecordCase.area` (선택, 문자열) | `source_refs`의 `area:<영역>` 항목 | 근거 참조에 영역 표시를 섞어 쓰고 있다 |
| 4 | `RecordCase.rejected` 또는 `kind` 외에 `outcome` | 요약 문장에서 기각 표현을 읽음(확신도 최대 mid) | 호출 쪽이 사실을 알고 있으니 문장 추론 대신 필드로 받는 게 정확하다 |

## ② 결과 형식

| # | 제안 | 지금 우회 방법 | 이유 |
|---|---|---|---|
| 5 | `ErrorCode.INVALID_INPUT` 추가(형식 위반은 `rejected`) | `failed` + `outcome="INVALID_INPUT"` | 공통 구성안 5장은 권한·형식·루프·토큰 위반을 `rejected`로 정한다. 지금 `ErrorCode`에 입력 오류 값이 없어 `rejected`로 못 쓴다(`AgentResult` 검증이 막는다) |
| 6 | `ConfirmOffboardingOutput.status`에 `rejected`(또는 `cancelled`) 추가 | 출력이 계약 모델이 아닌 dict(`{"status":"rejected"}`) | 사람이 보류를 거절·만료시킨 결과를 표현할 값이 없다 |
| 7 | `ConfirmReassignmentOutput.status`에 `held` 추가 | `held`는 `gate_blocked` 출력으로 돌려줌 | 실제 레지스트리가 이 액션을 C로 올리면 승인 대기를 표현해야 한다 |
| 8 | 보류 `HoldReason`에 "금액이 아닌 C" 값 추가 (가칭 `high_impact`) | `over_threshold`를 임시로 사용 | 퇴직 확정처럼 기준 금액이 없는 C의 사유 |
| 9 | 보류 건에 사람이 읽을 `summary` | 목은 `params.summary` | 보류 큐 화면에 "퇴직 확정 요청: emp_01 (payment)"를 보여 줘야 한다(재현·태윤에게 요청 중) |

## ③ 식별자·skill

| # | 제안 | 지금 우회 방법 | 이유 |
|---|---|---|---|
| 10 | hr이 스스로 시작하는 사건의 `context_id` 접두사 | `ctx_hnd_shift_<영역>_<시각>` (임시) | 교대 타이머가 인수인계를 시작한다. 공통 구성안 3장 접두사 목록(`ctx_inc_`·`ctx_req_` …)에 없다 |
| 11 | 온보딩 단계 완료 표시 skill — 아래 12 | 없음(진행률은 인수인계 확정 단계만 자동 완료) | `OnboardingStep.done`을 바꿀 요청이 없어 진행률이 오르지 않는다 |
| 12 | `update_onboarding_step` 제안: 입력 `{onboarding_id, step_id, done: bool}`, 출력 `StartOnboardingOutput`(갱신된 단계·진행률). 등급 A, 요청자는 orchestrator(대시보드가 사람 입력을 중계) | — | 새 담당자가 읽기 단계를 끝낼 때마다 호출. 진행률은 코드가 완료 단계 수로 다시 계산한다 |
| 13 | Card `priority`: `record_case`만 `required`, 나머지 9개는 `recommended` | 반영함(구현) | 공통 구성안 12장 기준 hr 필수는 `record_case` 하나 |
| 14 | `HandoverAnswerRequest`의 전달 경로 확정 | 사람 답변은 orchestrator가 중계한다고 가정(요청자 = orchestrator) | dashboard-api는 공통 로그 저장소만 읽는 서비스(`repo-structure.md`). 대시보드→orchestrator→hr 경로를 확정해야 한다 |

## 합의 순서 제안
1. 먼저 ① 1~3(카드·기록 형식)과 ② 5를 계약 PR 하나로: memory·safety·hr 소비자가 겹친다.
2. ② 6~9는 safety 신고 PR(`safety-grade-registration-draft.md`)과 같이 태윤과.
3. ③ 10~12는 통합 에이전트(orchestrator) 구현 직전에.
