# 레포 구성 — AX 가상회사 (e커머스)

GitHub 조직 `ax-company-hq` 아래 레포 2개(모두 public)로 관리한다. 백엔드는 uv workspace 모노레포, 프론트엔드는 별도 레포이며 두 레포는 API로만 연결한다.

| 레포 | 스택 | 내용 |
| --- | --- | --- |
| `ax-company-hq/ax-backend` | Python · uv workspace | 계약, 공통 템플릿, 통합 에이전트, 안전 레이어, 회사 기억, 판매 사이트·대시보드 서버, 부서 에이전트 |
| `ax-company-hq/ax-frontend` | React · Vite · npm | 판매 사이트 화면, 대시보드 화면, API 타입(자동 생성) |

레포 밖에서 버전 태그로 가져다 쓰는 것: `DejaFix`(백엔드), `common-frontend`(프론트), `common-mcp`(백엔드)

## 왜 이 구성인가

- 에이전트들은 계약과 공통 템플릿을 같이 써서 강하게 묶여 있다. 한 레포(모노레포)에 두면 공통 코드 변경이 즉시 전체에 반영된다.
- 부서 폴더는 uv workspace의 독립 패키지라, 모노레포 안에서도 부서별로 분리된다.
- 프론트는 API만 호출하므로 레포를 나눠도 손해가 적고, 멘토 방침(frontend/backend 분리)을 충족한다.
- 프로젝트가 끝나면 부서 폴더를 이력째 떼어내 개인 포트폴리오 레포로 만들 수 있다.

## ax-backend

```
ax-backend/
├─ pyproject.toml         # workspace 설정
├─ uv.lock                # 의존성 잠금 (루트에 하나)
├─ CLAUDE.md
├─ .env.example
├─ contracts/             # 팀 계약 원본 (인터페이스만, 구현 없음)
│  ├─ schemas/            # Agent Card, 이벤트, A2A 메시지, execute 입출력
│  ├─ data-access/        # 다른 서비스 데이터 접근용 API·뷰 형식
│  └─ openapi/            # shop-api, dashboard-api의 openapi.json (CI가 자동 생성)
├─ agent-template/        # 공통 템플릿, execute 구현 — 준하
├─ orchestrator/          # 통합 에이전트 — 준하
├─ safety/                # 안전 레이어 — 태윤
├─ memory/                # 회사 기억 — 재현
├─ shop-api/              # 판매 사이트 서버 — 재헌
├─ dashboard-api/         # 대시보드 서버 — 병환
├─ agents/
│  ├─ planning/           # 기획개발 — 예진
│  ├─ infra-dejafix/      # DejaFix 어댑터 — 예진
│  ├─ support/            # 고객지원 — 병환
│  ├─ marketing/          # 마케팅 — 재헌
│  ├─ hr/                 # 인사 — 준하
│  ├─ finance/            # 재무/정산 — 태윤
│  └─ data/               # 데이터분석 — 재현
├─ infra/                 # 전체 compose, 장애 주입
├─ docs/                  # 결정 기록, 시나리오
└─ .github/               # CODEOWNERS, CI 워크플로
```

각 서비스·부서 폴더는 독립 패키지로, `pyproject.toml`, `README.md`(담당자 표시), `tests/`, `Dockerfile`을 갖는다.

## ax-frontend

```
ax-frontend/
├─ shop-web/              # 판매 사이트 화면 — 재헌
├─ dashboard-web/         # 대시보드 화면 — 병환
└─ types/                 # API 타입 (자동 생성, 직접 수정 금지)
```

`common-frontend` 템플릿을 기반으로 시작한다.

## 핵심 규칙

### 1. 계약은 인터페이스만, 구현은 템플릿에
- `contracts/`에는 스키마와 인터페이스(입력·출력 형식)만 둔다. `execute`의 실제 구현은 `agent-template/`에 둔다.
- 계약 폴더가 작아야 승인 대상이 줄어든다.

### 2. 부서끼리 import 금지 (CI로 강제)
- 부서 에이전트는 서로 import하지 않고, `contracts/`와 A2A로만 통신한다.
- CI의 `import-linter`가 `agents/*` 사이의 import를 차단한다.

### 3. 다른 서비스 데이터는 API·뷰로만
- data·finance 에이전트는 판매 사이트 DB에 직접 쿼리하지 않고, shop-api나 읽기 전용 뷰로 접근한다.
- dashboard-api는 safety·orchestrator 내부를 직접 읽지 않고 공통 로그 저장소만 읽는다.
- 접근 형식은 `contracts/data-access/`에 정의한다.

### 4. 프론트 타입은 자동 생성
- 백엔드 CI가 shop-api·dashboard-api의 `openapi.json`을 `contracts/openapi/`로 내보내고, 코드와 다르면 실패한다.
- 프론트는 백엔드 버전 태그의 `openapi.json`을 받아 `openapi-typescript`로 `types/`를 생성한다.
- 손으로 복사한 계약 사본은 두지 않는다.

## 브랜치와 머지

```
main        ← 야간 e2e 성공 시 release 워크플로가 머지 (AX_RELEASE=off면 정지)
 └─ dev     ← 기본 브랜치
     ├─ data/impact-analysis            (사람)
     ├─ data/12-impact-analysis-r0      (에이전트, ax-bot)
     └─ ci/automation-stage-1           (사람 경로)
```

- 작업 브랜치: dev에서 `<부서 id>/<기능>`으로 생성 → dev로 PR (영어 소문자, 단어 사이는 `-`)
- 머지는 **사람 승인 없이** 관문 + 다른 부서 에이전트 리뷰로 한다. 상세 설계는 `docs/automation-spec.md`가 원본

| PR 종류 | 머지 조건 |
| --- | --- |
| 부서 폴더 PR (contracts·agent-template·infra 포함) | ci·contracts-compat·automation-tests 통과 + folder-guard·test-guard·dev-health + 리뷰 부서 에이전트 승인(agent-review) |
| 핵심 경로 PR (`.github/`, `.claude/`, `CLAUDE.md`, 루트 파일, `scripts/safe-test.sh`) | 위 체크 + **code owner(재현·준하·태윤 중 한 명) 승인**. 에이전트 리뷰는 생략 |
| 감사 경로 PR (`docs/`, `contracts/`, `scripts/`, 루트 `pyproject.toml`) | 위 체크 + **human-path-audit**: 기계 규칙 통과 후 감사 에이전트 2개(보안·기준 정합) 승인. 사람 승인 없음 |
| contracts PR | 위 부서 폴더 PR 조건. 리뷰는 바뀐 계약 파일을 쓰는 부서 에이전트. 깨지는 변경은 contracts-compat이 막음 |
| 기준표 PR (`thresholds.toml`, `ai_review_policy.toml`) | 체크 + **code owner(태윤·재현·준하) 승인**. 에이전트는 못 바꿈 |
| 봇 PR (`revert/`, `openapi/sync-`) | 리뷰·호환 검사 생략, 허용 경로만 |

### 왜 사람 승인이 아니라 에이전트 리뷰인가

DejaFix에서는 "승인 1명 + 작성자 본인 승인 불가" 때문에 PR 30개가 막혔다. 사람 승인을 부서 에이전트의 교차 리뷰로 바꾸고, 리뷰 부서를 표로 고정해 같은 사람이 자기 PR을 리뷰하는 경우는 자동으로 다른 부서로 대체한다. 브랜치 보호의 Required approvals는 0이다.

### CODEOWNERS는 핵심 경로만

```
# .github/CODEOWNERS (요약, 원본 참고)
*                    @JangJaehyeonn @haakim7 @2taeyoon   # 핵심 기본값
/docs/  /contracts/  /scripts/  /pyproject.toml           # 감사 경로 (소유자 없음)
/agent-template/ ... /agents/*/ /infra/ /uv.lock          # 부서 폴더 (소유자 없음)
/scripts/safe-test.sh  thresholds.toml  ai_review_policy.toml  @재현·준하·태윤  # 맨 뒤
```

뒤의 줄이 앞의 `*`를 덮어써서, owner가 없는 줄의 경로는 code owner 승인이 필요 없다. 적힌 owner 중 한 명이 승인하면 된다.
부서 폴더 안의 기준표(`thresholds.toml`, `ai_review_policy.toml`)는 맨 뒤에 owner 줄을 따로 둔다 (team.yml `human_files`).

### 리뷰 부서

부서 id와 리뷰 부서 표는 `CLAUDE.md` 폴더 표와 `docs/automation-spec.md` "리뷰어 배정"을 따른다. contracts는 agent-template(준하) + safety(태윤) 두 에이전트가 리뷰하고, 작성자가 둘 중 한 사람이면 그 자리는 data(재현)가 맡는다.

## CI

| 단계 | 내용 |
| --- | --- |
| 경로 필터 | 바뀐 폴더의 테스트만 실행 (`agents/data/**` 변경 → data 테스트) |
| import-linter | 부서 간 import 차단 |
| contracts-compat | contracts·shop-api·dashboard-api 변경 시 merge-base와 비교해 하위 호환이 깨지면 실패 |
| 관문 (ax-bot) | folder-guard(남의 폴더 변경), test-guard(테스트 약화), dev-health(dev 빨간불이면 대기) |
| openapi-export | dev push 후 openapi.json이 코드와 다르면 봇이 `openapi/sync-` PR 생성 |
| LLM mock | CI 테스트는 LLM을 가짜 응답으로 대체. 실제 호출은 서버 PC의 시나리오 테스트에서만 |

## 충돌 방지

| 문제 | 대응 |
| --- | --- |
| `uv.lock` 충돌 | 직접 고치지 않는다. lock 갱신은 build 잡, 충돌은 sweeper의 lock-refresh |
| 공유 폴더 변경으로 다른 부서가 깨짐 | 작게, 호환되게 바꾸고 PR 본문에 영향받는 부서 명시 |
| 의존성 추가 | PR에서 합의 (workspace는 버전이 하나로 통일됨) |
| Claude Code가 남의 폴더 수정 | 훅(guard-folder)이 차단 + folder-guard 관문 + `to:<부서>` 요청 이슈로 분리 |
| dev가 깨짐 | dev-health가 새 머지를 멈추고, 마지막 초록 이후 첫 PR을 1회 revert |
| 포트 충돌 | `CLAUDE.md`의 포트 표 기준 |

## 6명 동시 개발

- LLM API 키는 각자 따로 `.env`에 둔다.
- 개발은 각자 PC에서 자기 서비스와 필요한 것만 띄우고, 서버 PC는 통합 테스트·데모에만 쓴다.
- 준하가 `contracts/`와 `agent-template/`의 인터페이스를 먼저 올리면 나머지가 기다리지 않고 시작할 수 있다.
- 작업 이슈는 `docs/requirements.md`에서 claude-plan이 만들고, 이슈의 `depends`가 닫혀야 claude-tasks가 집어 간다. 그래서 contracts 이슈를 phase 1·depends의 앞에 둔다.
- Claude 실행은 사람마다 하루 상한이 작성(write_cap)·리뷰(review_cap) 따로 있다. 상한은 `.github/team.yml`에서 조정한다.

## 두 레포 연결

```
# ax-frontend/.env
VITE_SHOP_API_URL=http://localhost:8010
VITE_DASHBOARD_API_URL=http://localhost:8020
```

- 각자 개발: 백엔드 `docker compose -f infra/compose.yml up`, 프론트 `npm run dev`
- 전체 데모: 두 레포를 같은 폴더에 나란히 clone하고, `infra/compose.yml`에서 `../ax-frontend`를 서비스로 빌드

## 프로젝트 종료 후 분리 (포트폴리오)

- `git filter-repo`로 자기 폴더를 이력째 독립 레포로 분리한다 (예: `agents/hr` → `ax-hr`).
- 분리하면 workspace 경로 의존성이 끊기므로, `contracts`·`agent-template` 의존성을 git 태그 의존성으로 바꾼다.
  ```
  ax-contracts @ git+https://github.com/ax-company-hq/ax-backend@v1.0.0#subdirectory=contracts
  ```
- 폴더 README가 그대로 레포 README가 된다.

## 시작 순서

1. 조직 관리자: `ax-backend`, `ax-frontend` 빈 레포 생성, 조직 Base permissions Write
2. 세팅 담당: 폴더 뼈대, workspace, `CLAUDE.md`, docs, CI(`ci`·`contracts-compat`) 커밋 → dev 브랜치, 기본 브랜치 dev, Allow auto-merge
3. 조직 관리자: ax-bot GitHub App 생성·설치, environment `claude`(6명 Claude 토큰)·`bot`(ax-bot 키), repo 변수 `AX_*`
4. 세팅 담당: 자동화 단계별 프롬프트(1~4, 프론트 F-1·F-2)로 `docs/automation-spec.md` 구현. 처음엔 `AX_AUTOMATION=off`·`AX_TASKS=off`
5. 조직 관리자: 브랜치 보호(API로 설정, ax-bot 상태는 출처를 앱 ID로 고정)
6. 준하: `contracts/`, `agent-template/` 인터페이스 커밋
7. `AX_AUTOMATION=on` → 리뷰·관문 동작 확인 → `AX_TASKS=on`(하루 1회부터)
