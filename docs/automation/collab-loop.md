# 협업 루프 (collab-loop)

## 지금 켜진 자동화 / 꺼진 자동화

2026-10-23 기준, ax-backend. 워크플로 파일은 지우지 않았고 스위치(repo 변수)나 workflow disable로만 껐다. ax-frontend는 `ci.yml`만 있다.

### 켜진 자동화

| 자동화 | 워크플로 | 스위치 | 메모 |
| --- | --- | --- | --- |
| ci, contracts-compat | `ci.yml` | 항상 | dev 필수 체크 |
| automation-tests | `automation-tests.yml` | 항상 | dev 필수 체크 |
| guards (folder-guard, test-guard, dev-health) | `guards.yml` | 항상 (dev-health는 `AX_DEV_RED` 참조) | dev 필수 체크 |
| auto-merge | `auto-merge.yml` | 항상 | agent-review가 dev 필수 체크가 아니면 예약하지 않게 되어 있어 **지금은 예약 안 함** → 루프가 승인 후 자동 머지 예약 |
| bot-ci-failed | `bot-ci-failed.yml` | `AX_AUTOMATION=on` | |
| 봇 재작성 (재시도·wip 재개) | `_claude-write.yml` | `AX_AUTOMATION=on` | guards 재대기·bot-ci-failed·wip-resume이 직접 실행. 이미 진행 중인 봇 이슈만 해당 |
| stale-sweep, wip-resume | `stale-sweep.yml`, `wip-resume.yml` | `AX_AUTOMATION=on` | |
| hotfix, team-guard, core-change-notify | 각 파일 | 항상 | |

### 꺼진 자동화

| 자동화 | 워크플로 | 끈 방법 | 이유 | 다시 켜는 방법 |
| --- | --- | --- | --- | --- |
| agent-review | `claude-review.yml` | workflow disable + dev 필수 체크에서 `agent-review` 제거 | 루프 교차 리뷰로 대체 | `gh workflow enable claude-review.yml -R ax-company-hq/ax-backend` + dev 필수 체크에 `agent-review`(app_id 5419374) 추가 |
| 봇 코드 작성 (`to:` 라벨 자동 처리) | `claude-request.yml` | workflow disable (`AX_AUTOMATION`은 on 유지: bot-ci-failed 등과 묶여 있음) | 요청은 루프가 처리 | `gh workflow enable claude-request.yml -R ax-company-hq/ax-backend` |
| 아침 리포트 | `morning.yml` | `AX_MORNING=off` | 루프 중심 운영 | `AX_MORNING=on` |
| /go (오토파일럿 작성 시작) | `go.yml` | `AX_GO=off` | 루프 중심 운영 | `AX_GO=on` |
| relay (blocked-by 해제·요청 합치기) | `relay.yml` | `AX_RELAY=off` | 루프 중심 운영 | `AX_RELAY=on` |
| 실서버 실행 | `real-run.yml` | `AX_REAL_RUN=off` | 루프 중심 운영 | `AX_REAL_RUN=on` |
| 야간 e2e·자동 revert | `scenario-a.yml` | `AX_E2E`, `AX_AUTO_REVERT` 미설정 | 아직 켠 적 없음 (자동 revert는 남기기로 했지만 지금은 꺼져 있음) | 둘 다 `on` |
| release | (워크플로 없음) | `AX_RELEASE=off` | | |

## 루프 실행 (v2)

- 지시문: `.claude/commands/collab-loop.md`
- 실행: ax-backend 폴더에서 `/loop 30m /collab-loop` (재현은 `/loop 30m /collab-loop 닥터`)
- 시작 전: 작업 폴더가 `dev`이고 커밋 안 한 변경이 없어야 한다 (아니면 루프가 "작업 폴더 정리 필요"만 보고하고 끝낸다)
- 내 계정과 부서는 `.github/team.yml`에서 찾는다: `depts.<부서>.owners`·`token_owner`, 프론트는 `frontend_depts`. 리뷰 부서는 `reviewers`(바뀐 부서 → 리뷰 부서), 그 담당자는 그 부서의 `owners`

### 한 회차에 하는 일

| 일 | 무엇 | 회차 한도 |
| --- | --- | --- |
| A. PR | 내 PR·내 부서 봇 PR 정리(CI·변경 요청·충돌), 남의 PR 리뷰·승인, 자동 머지 예약 | 새 PR 2, 리뷰 3 |
| B. 대화 | 협업 부서 에이전트와 `[협업]` 이슈 스레드에서 질문·제안·합의 | 답글 4, 새 스레드 1 |
| C. 받은 요청 | `to:<내 부서>` 이슈의 skill을 테스트 먼저 만들어 PR | (새 PR 한도 안에서) |
| D. 보낼 요청 | 내 설계에 필요한 skill을 `to:<상대 부서>`로 요청 | 새 요청 2 |

요청·합의가 없는 새 기능은 루프가 만들지 않는다 (사람이 별도 세션에서 한다).

### 부서 원칙 (멘토링 4회차, 2026-10-23)

- 에이전트는 다른 부서 요청을 대신 처리하는 보조 도구가 아니라 **캣북스의 한 부서**다. 업무 범위는 실제 회사의 그 부서가 기준이고, 시중 솔루션을 벤치마킹한다
- 그 부서의 일이 아닌 것은 하지 않는다. 예: 재무가 환불 한 건의 금액을 승인하지 않는다. 건별 처리는 시스템 규칙(safety)과 담당 부서가 한다
- 정책은 부서가 정하고 집행은 safety 규칙이 한다. 숫자는 코드가 계산하고 AI는 해석·설명만 한다
- 흐름: 자동 수집 → 규칙으로 맞추고 분류 → 예외만 사람에게 → 결과 보고

### 설계 문서 (에이전트 부서 10개만)

- 대상: data, memory, finance, safety, hr, orchestrator, support, marketing, planning, dejafix
- 위치: `docs/notes/<부서>/agent-design.md` — **사람이 채운다**. 루프는 없으면 빈 틀만 PR로 올리고 내용은 지어내지 않는다
- 틀 항목: 실제 회사의 이 부서, 벤치마킹 솔루션, 협업 부서, 1 고유 업무 3가지, 2 스스로 판단 / 사람에게 넘김, 3 받는 요청(내 skill), 4 하는 요청(필요한 skill), 5 기억, 6 지표, 7 하지 않는 일
- 설계가 비어 있으면 그 부서 루프는 A·C만 하고 B·D는 하지 않는다 ("설계 미작성"으로 보고)
- 그 밖의 부서(contracts, agent-template, infra, dashboard-api, shop-api, 프론트 앱)는 설계 문서 없이 A·C만 한다

### 대화 규칙 (B)

- 스레드: 이슈 제목 `[협업] <부서A> ↔ <부서B>: <주제>`, 라벨 `talk`, `talk:<부서A>`, `talk:<부서B>`
- 메시지 종류: 질문 · 제안 · 답변 · 합의 · 결정 필요. 첫 줄은 `[loop:<이름>/<부서>]`
- **질문·제안에만 답한다**. 답변·합의에는 답하지 않는다 (끝없이 주고받지 않게). 물을 것이 있으면 종류를 `질문`으로 쓴다
- **합의를 남긴 루프가 후속까지 처리한다**: `talk-agreed` 라벨 → 만들 부서로 `to:` 요청(계약이 필요하면 contracts 규칙대로) → 스레드에 번호를 남기고 닫기
- 사람에게 넘김(`talk-decision` + 두 담당자 멘션, 사람 코멘트가 올 때까지 루프는 답하지 않음):
  - 어느 쪽이든 `결정 필요`를 남김
  - 코멘트 8개가 넘도록 합의 없음
  - 돈·환불·보상 기준, 사람 승인(safety) 기준, 고객 정보 범위, 기존 계약 변경이 걸림
- 사람 팀원 코멘트(`[loop:`로 시작하지 않음)는 그 스레드의 결정이다

### 라벨

| 라벨 | 뜻 |
| --- | --- |
| `talk` | 부서 간 대화 스레드 |
| `talk:<부서>` | 그 부서가 참여하는 스레드 (부서 15개 + shop-web·dashboard-web·planning-web) |
| `talk-agreed` | 합의됨, 후속 요청 생성 |
| `talk-decision` | 사람 결정 필요 |
| `waiting` | 받은 요청이 `blocked-by #M`에 막혀 기다리는 중 |
| `request`, `to:<부서>`, `in-progress`, `needs-human`, `blocked` | 기존 라벨 그대로 |

### 막히면

- 훅·권한에 막힌 명령은 우회하지 않고 보고에 남긴다 (사람이 처리)
- `needs-human` PR과 `talk-decision` 스레드는 사람이 본다. 닥터 루프가 매 보고 맨 위에 목록과 경과 시간을 올린다
