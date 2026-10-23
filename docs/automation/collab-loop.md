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

## 루프 실행

- 지시문: `.claude/commands/collab-loop.md`
- 실행: ax-backend 폴더에서 `/loop 30m /collab-loop` (재현은 `/loop 30m /collab-loop 닥터`)
- 내 계정과 부서는 `.github/team.yml`에서 찾는다: `depts.<부서>.owners`·`token_owner`, 프론트는 `frontend_depts`. 리뷰 부서는 `reviewers`(바뀐 부서 → 리뷰 부서), 그 담당자는 그 부서의 `owners`
