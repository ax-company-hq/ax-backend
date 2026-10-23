# ax-backend 통합 규약

e커머스 가상회사의 부서별 AI 에이전트를 A2A로 연결한 AX 플랫폼 백엔드.
기획서·요구사항정의서의 요구사항 ID(COM, ORC, SAF, DEP, NFR 등)를 기준으로 작업한다. 레포 구성의 상세 이유는 `docs/repo-structure.md`를 본다.

- 스택: Python, uv workspace, a2a-sdk + FastAPI, 서비스 1개당 컨테이너 1개
- LLM: Gemini (DejaFix와 동일). 반드시 `agent-template/`의 OpenAI 호환 래퍼로만 호출하고, 모델 이름·엔드포인트는 `.env`로 설정한다. 코드에 특정 모델을 하드코딩하지 않는다 (나중에 더 좋은 모델로 교체 예정)
- 프론트엔드는 별도 레포 `ax-company-hq/ax-frontend`. API로만 연결한다.

## 폴더와 담당

| 폴더 | 부서 id | 내용 | 담당 | 리뷰 부서 (자동) |
| --- | --- | --- | --- | --- |
| `contracts/` | contracts | 팀 계약 원본: schemas, data-access, openapi | 준하, 태윤, 재현 | 바뀐 계약 파일을 쓰는 부서 (`team.yml` `contracts_affected`, 공통 파일은 agent-template + safety). 깨지는 변경은 `contracts-compat`이 막음 |
| `agent-template/` | agent-template | 공통 템플릿, execute 구현 | 준하 | finance + planning |
| `orchestrator/` | orchestrator | 통합 에이전트 | 준하 | dejafix |
| `safety/` | safety | 안전 레이어 | 태윤 | agent-template |
| `memory/` | memory | 회사 기억 | 재현 | support |
| `shop-api/` | shop-api | 판매 사이트 서버 | 재헌 | data |
| `dashboard-api/` | dashboard-api | 대시보드 서버 | 병환 | safety |
| `agents/planning/` | planning | 기획개발 에이전트 | 예진 | orchestrator |
| `agents/infra-dejafix/` | dejafix | DejaFix 어댑터 | 예진 | orchestrator |
| `agents/support/` | support | 고객지원 에이전트 | 병환 | marketing |
| `agents/marketing/` | marketing | 마케팅 에이전트 | 재헌 | support |
| `agents/hr/` | hr | 인사 에이전트 | 준하 | memory |
| `agents/finance/` | finance | 재무/정산 에이전트 | 태윤 | data |
| `agents/data/` | data | 데이터분석 에이전트 | 재현 | finance |
| `infra/` | infra | 전체 compose, 장애 주입 | 준하 | finance + planning |
| `docs/notes/<부서>/` | 그 부서 | 부서별 작업 메모 | 그 부서 담당 | 그 부서와 같음 |
| 그 외 전부 (`.github/`, `.claude/`, `scripts/`, `CLAUDE.md`, `docs/`, 루트 파일) | — | 자동화·문서·규칙 (human_paths) | 사람 | **사람 code owner 승인** (`.github/`·`.claude/`·`scripts/`·`CLAUDE.md`는 재현, 준하, 태윤 / 그 밖은 재현, 준하) |
| 부서 폴더 안의 기준표 (`safety/.../thresholds.toml`, `agents/finance/.../ai_review_policy.toml`) | — | 기준 금액 등급표·AI 재량 한도 (`team.yml` `human_files`, human_paths로 취급) | 사람 | **사람 code owner 승인** (태윤, 재현, 준하). 에이전트는 못 바꿈 |
| 루트 `uv.lock` | — | 의존성 잠금 | 자동 생성 | 같은 PR에서 그 부서 `pyproject.toml`이 바뀐 경우만 허용 |

리뷰 부서 담당이 PR 작성자와 같은 사람이면 자동으로 다른 부서로 바뀐다. 상세는 `docs/automation-spec.md` "리뷰어 배정".

서비스·부서 폴더는 각각 독립 패키지로 `pyproject.toml`, `README.md`(담당자 표시), `tests/`, `Dockerfile`을 갖는다.

## 작업 규칙 (반드시 지킬 것)

1. **자기 부서 폴더(+ `docs/notes/<부서>/`)만 수정한다.** 다른 폴더 수정이 필요하면 코드를 고치지 말고 `to:<부서>` 요청 이슈로 넘긴다 (Actions 안의 Claude는 `result.json`의 `requests`에 적는다).
2. **부서끼리 import하지 않는다.** 부서 에이전트는 `contracts/`와 A2A로만 통신한다. CI의 import-linter가 막는다.
3. **계약은 `contracts/`에만 정의한다.** 메시지·이벤트·API 형식을 각 폴더 안에 새로 만들지 않는다. `contracts/`에는 인터페이스만 두고 구현은 넣지 않는다.
4. **다른 서비스 데이터는 API·읽기 전용 뷰로만 읽는다.** 판매 사이트 DB에 직접 쿼리하지 않는다. dashboard-api는 공통 로그 저장소만 읽는다. 접근 형식은 `contracts/data-access/`에 있다.
5. **공유 폴더를 바꾸는 PR은 본문에 영향받는 폴더를 적는다.** 작게, 기존과 호환되게 바꾼다.
6. **API 응답 형식을 바꾸면** `contracts/openapi/`가 CI로 다시 생성되는지 확인하고, 프론트 담당(재헌, 병환)에게 알린다.
7. **비밀값은 `.env`에만 둔다.** 커밋 금지. 부서 환경변수 예시는 `<폴더>/.env.example`에 둔다. 루트 `.env.example`은 공통 변수만 두며 사람 승인 경로다. LLM 키는 각자 따로 쓴다.
8. **`uv.lock`은 직접 고치지 않는다.** 자기 폴더 `pyproject.toml`에 의존성을 추가하면 lock 갱신은 CI의 build 잡이 한다. 충돌은 sweeper의 lock-refresh가 처리한다.
9. **테스트를 지우거나 약하게 만들지 않는다.** test-guard가 부서별로 테스트 삭제(바뀐 파일에 있던 테스트 이름이 사라짐)와 의미 있는 assert 수 감소(`assert True` 같은 상수 assert는 세지 않음)를 막는다. 파일을 나누거나 옮기는 것은 테스트 이름이 남아 있으면 통과한다.
10. **자동화 파일은 사람만 바꾼다.** `.github/`, `.claude/`, `scripts/`, `CLAUDE.md`, `docs/`(notes 제외)는 code owner 승인이 필요하다. 부서 폴더 안에 `CLAUDE.md`, `.claude/`, `.mcp.json`을 만들지 않는다.
11. **기준 문서와 다른 동작은 docs PR이 먼저다.** `docs/requirements.md`, `docs/agent-common.md`(기준 문서)와 다르게 동작해야 하면, 먼저 기준 문서를 고치는 docs PR을 팀이 합의해 머지한 뒤 코드 PR을 올린다. 코드 PR 본문에 그 docs PR 번호를 적는다. docs PR 없이 기준 문서와 다른 코드 PR은 리뷰에서 막는다.

## 표기·형식

| 항목 | 규칙 |
| --- | --- |
| 필드 이름 | 우리가 정의하는 모든 필드는 snake_case (`context_id`, `tenant_id`, `idempotency_key`). A2A SDK 프로토콜 필드만 SDK 그대로 |
| ID | 접두사 문자열: `ctx_`, `evt_`, `ord_`, `pay_`, `prd_`, `sel_`, `ret_`, `rfd_`, `cpn_`, `cmp_`, `ntc_`, `inq_`, `exe_`, `rev_`, `card_` |
| 이벤트 발행 | 모든 서비스는 agent-template `events.publish`(Redis Streams)로만 발행한다. 별도 HTTP 이벤트 엔드포인트를 만들지 않는다 |
| 쓰기 API 호출 제한 | 판매 사이트 쿠폰·공지 API는 마케팅 관문 토큰만, 환불 API는 고객지원 관문 토큰만 허용 |
| context_id | 사건 `ctx_inc_`(통합 에이전트만 발급), 변경 요청 `ctx_req_`, 공지·패턴 `ctx_ntc_`, 캠페인 `ctx_cmp_` |
| tenant_id | 모든 테이블·요청·이벤트·카드에 필수, 값 `shop-demo` |
| 시간·금액 | UTC ISO8601로 저장(화면만 KST), 금액은 정수 원 |
| API | 경로 `/api/<부서>/<복수 명사>`, 성공 `{data, meta}`, 실패 `{error: {code, message}}` |
| 환경변수 | 부서 접두사 대문자 (`DATA_MOCK`, `SUPPORT_LLM_MODEL`). 공통 값은 접두사 없이 |
| 테이블 | 복수형 snake_case, 모두 `tenant_id`·`context_id` 포함 |
| 커밋 | Conventional Commits, 영문 접두사 (`feat(data): add impact analysis`) |
| 이벤트 | 공통 봉투 `event_id, type, version, occurred_at, tenant_id, context_id, source, idempotency_key, data, untrusted`. 사실만 담고 판단 문장은 넣지 않는다 |

부서 간 책임 경계와 이벤트 카탈로그는 `docs/`의 「에이전트 공통 구성안」을 따른다.

## 브랜치와 머지

자동화의 전체 설계는 `docs/automation-spec.md`가 원본이다. 여기에는 개발자가 알아야 할 것만 적는다.

- 기본 브랜치 `dev`. main은 야간 e2e 성공 후 release 워크플로가 머지한다 (`AX_RELEASE=off`면 정지).
- 브랜치 이름: `<부서 id>/<기능>`, 영어 소문자, 단어 사이는 `-`
  - 사람: `data/impact-analysis`, `agent-template/execute-wrapper`
  - 에이전트(ax-bot): `<부서>/<이슈번호>-<slug>-r<재시도>`
  - 사람 경로 작업(`.github/` 등)은 `ci/<기능>`, 문서는 `docs/<기능>`
  - `.`으로 시작하는 브랜치 이름은 git이 거부한다
- PR이 dev에 머지되는 조건 (전부 초록이면 자동 머지):
  - `ci`, `contracts-compat`, `automation-tests` (GitHub Actions)
  - `folder-guard`, `test-guard`, `dev-health`, `agent-review` (ax-bot 커밋 상태)
  - human_paths를 바꿨으면 code owner 승인 (`.github/`·`.claude/`·`scripts/`·`CLAUDE.md`·기준표 toml은 재현·준하·태윤 중 한 명, 그 밖의 human_paths는 재현·준하 중 한 명)
- `agent-review`는 다른 부서 에이전트가 한다. 단 공유 폴더(`contracts/`, `agent-template/`)·안전 레이어(`safety/`)·통합 에이전트(`orchestrator/`)를 건드렸거나 부서 2개 이상에 걸친 PR에만 돈다. 부서 폴더 하나 안에서만 바뀐 PR은 `agent-review`가 "not required"로 통과하고 CI·관문만으로 자동 머지된다. 사람 승인 수(Required approvals)는 0이다. 사람이 승인 버튼을 누를 필요가 없다.
- contracts는 사람 승인 없이, 바뀐 계약 파일을 쓰는 부서의 에이전트 리뷰 + 하위 호환 검사(`contracts-compat`)로 머지한다. 하위 호환이 깨지는 변경은 `contracts-compat`이 막으니, 필드 추가 → 이전 → 삭제처럼 호환되는 단계로 나눈다.
- 사람 PR은 자기가 맡은 부서 여러 개를 함께 바꿀 수 있고, 해당 리뷰 부서 전부가 승인해야 한다. 에이전트 PR은 부서 하나만.
- 급한 수정: code owner(재현·준하)가 `hotfix` 라벨을 붙이면 관문을 통과한다. 새 커밋이 올라오면 라벨이 떨어진다.
- 에이전트 PR이 ci나 관문에서 실패하면 실패 요약을 이슈에 힌트로 남기고 PR을 닫아 한 번 다시 작성한다. 같은 이슈가 두 번 실패하면 `needs-human`, 작성이 턴 한도에 걸리면 `needs-split`(작업을 나눠 다시 올린다)으로 멈춘다.
- 부서마다 열린 에이전트 PR 2개·진행 중 이슈 3개까지만 동시에 작성한다. 넘으면 `wip-wait`로 기다렸다가 자리가 나면 자동으로 시작한다.
- 48시간 넘게 변화가 없는 에이전트 PR·이슈는 매일 정리 작업이 닫고 목록을 report 이슈에 남긴다.
- 핵심 파일(`.github/`, `.claude/`, CODEOWNERS, 기준표 toml)은 사람 PR로만 바뀐다 (에이전트 PR은 folder-guard가 막음). 머지되면 report 이슈에 기록하고 재현을 멘션하며, 매일 정리 report에 "지난 24시간 핵심 파일 변경" 목록이 남는다.

## 로컬 개발 (각자 PC의 Claude Code)

- `.claude/settings.local.example.json`을 `settings.local.json`으로 복사해 자기 부서를 적는다: `{"env": {"AX_DEPTS": "data,memory"}}`
  - 이 값이 없으면 훅이 모든 쓰기를 막는다
- 로컬 훅: 자기 부서 폴더 밖 수정 차단, `git push`는 `origin <자기 부서>/<기능>`만 (force·delete·dev·main 차단)
- 로컬에서 만든 PR도 같은 관문과 에이전트 리뷰를 거친다

## 에이전트 구현 규칙 (안전장치)

모든 에이전트는 `agent-template/`에서 시작하며, 아래 규칙은 템플릿이 강제한다. 우회하는 코드를 작성하지 않는다.

- **실행 관문**: 상태를 바꾸는 액션(쿠폰 지급, 롤백, 정산 보류, 공지 게시 등)은 반드시 `execute()`를 거친다. 외부 API를 직접 호출해 상태를 바꾸지 않는다.
- **contextId 필수**: 모든 요청·로그에 사건 단위 contextId를 붙인다.
- **루프 거부·홉 제한**: 요청의 `path`에 자기 자신이 있으면 에러로 반환한다. path 길이가 3을 넘으면 이관 큐로 넘긴다.
- **타임아웃**: 직통 요청은 `deadline`까지 응답이 없으면 포기하고 통합 에이전트에 보고한다.
- **인증**: 모든 요청에 에이전트 토큰을 붙인다. requester는 토큰에서 꺼내며 직접 쓰지 않는다.
- **권한(ACL)**: 자기 skill을 요청할 수 있는 에이전트를 Agent Card에 명시한다.
- **신뢰 경계**: 고객 문의·판매자 정보 같은 외부 입력은 `untrusted` 필드에만 담는다. 프롬프트에는 데이터로만 넣고 지시로 해석하지 않는다. 다른 에이전트에 넘길 때도 `untrusted` 표시를 유지한다.
- **구조화 출력**: LLM 응답은 pydantic 스키마로 검증하고, 실패하면 재요청한다.
- **계산은 코드로**: 건수·금액·비율 같은 숫자는 SQL/코드로 산출한다. LLM은 해석과 판단만 한다.
- **근거 첨부**: 결과 메시지에 `evidence`(쿼리, 데이터 요약, 참조 로그 ID)를 붙인다.
- **멱등성**: 실행 요청에는 `idempotency_key`(context_id + 액션 + 대상)를 붙인다.
- **루프 검사는 새 요청에만**: 동기 질의의 응답은 같은 Task의 결과라 루프가 아니다.
- **C 등급 보류**: 모든 C 건은 safety 보류 큐 하나로 간다. 부서별 보류 목록을 만들지 않는다. 담당자가 대시보드에서 승인·거절하고, 데모 기준 10분 안에 결정이 없으면 자동 거절된다.
- **fail-closed**: 안전 레이어가 응답하지 않으면 실행하지 않는다.

## Agent Card

각 부서 폴더에 `agent-card.json`을 두고, 형식은 `contracts/schemas/`를 따른다. skill마다 다음을 적는다.

- `id`, `description`, `input_schema`, `output_schema`
- `grade`: `read` | `A` | `B` | `C`
- `allowed_requesters`: 이 skill을 요청할 수 있는 에이전트 목록
- `verifier`: B 등급의 교차검증 주체. 자기 에이전트면 안 된다
- `priority`: `required` | `recommended` | `optional`

## 포트

| 서비스 | 포트 |
| --- | --- |
| orchestrator | 8000 |
| safety | 8001 |
| memory | 8002 |
| shop-api | 8010 |
| dashboard-api | 8020 |
| agents/planning | 8101 |
| agents/support | 8102 |
| agents/marketing | 8103 |
| agents/hr | 8104 |
| agents/finance | 8105 |
| agents/data | 8106 |
| agents/infra-dejafix | 8107 |
| redis (이벤트 버스) | 6379 |
| ax-frontend shop-web / dashboard-web | 3000 / 3001 |

새 서비스를 추가하면 이 표부터 갱신한다.

## 실행

- 전체: `docker compose -f infra/compose.yml up`
- 개발할 때는 자기 PC에서 자기 서비스와 필요한 것만 띄운다. 서버 PC는 통합 테스트·데모에만 쓴다.
- 각 서비스 폴더의 `compose.yml`을 `infra/compose.yml`이 `include:`로 불러온다.
- 프론트까지 함께 띄울 때는 두 레포를 같은 폴더에 나란히 clone한다.

## 테스트와 CI

- 각 서비스·부서는 통합 에이전트 없이 동작하는 단독 테스트 1개 이상을 `tests/`에 둔다.
- CI는 바뀐 폴더의 테스트만 실행한다 (경로 필터).
- CI 테스트에서 LLM은 mock으로 대체한다. 실제 호출은 서버 PC의 시나리오 테스트에서만 한다.
- 시나리오 테스트(SCN-A, SCN-B, SCN-L, SCN-S)는 `docs/` 시나리오 기준으로 `infra/`에서 실행한다. 매일 00시(KST) e2e가 mock 모드로 돌고, 실패하면 원인 부서에 priority 이슈가 생긴다.
- 테스트는 `scripts/safe-test.sh <부서>`와 같은 방식(비밀값 없는 컨테이너, `<부서>_MOCK=1`)으로 돈다. 테스트가 네트워크·비밀값에 기대지 않게 쓴다.
