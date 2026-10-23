# hr 두 번째 야간 작업 보고 (추가 17개 업무)

브랜치 `feat/hr-impl` (origin 백업, 마지막 커밋 `81ae349`). PR·Slack·`contracts/`·타 팀 폴더 수정 없음.
첫 번째 보고는 `morning-report-1023.md`.

## 1. 끝낸 것

| 업무 | 결과 |
|---|---|
| 추가 A-1 독립 코드 리뷰·수정 | 1차(버그 10건)·plan-reviewer(26건 중 hr에서 고칠 것)·2차(8건)를 모두 반영하고 회귀 테스트로 고정 |
| 추가 A-2·3 실패 경로·안전 회귀 | `test_safety_regression.py` 81건 전부 "잘못 통과 0건" |
| 추가 A-4 반복·퍼징 | 시나리오 26개×20회 동일, 무작위 입력 약 25,500건(고정 시드 9개)에서 500·handler_error 0건 |
| 추가 A-5 통합 시나리오 | 시나리오 20~26(전체 흐름·입사·퇴직 재개·잠금·퇴직자 후임·재질문·결정 대체), 총 26개 |
| 추가 1 결정 탐지 개선 | 표본 68+20문장. 규칙을 정한 뒤 처음 본 20문장 기준 이유 16→20, 기각 18→20. 리뷰가 찾은 오탐 8건도 반영 |
| 추가 4 감사 로그 조회 | `AuditLog.timeline/contexts`, `/api/audit`, 데모 탭 |
| 추가 B-1·B-2 | 인계자 질문 재질문, 퇴직 보류 거절·만료 뒤 취소→다시 열기 |
| 추가 B-3 | 온보딩 완료 표시 제안서(`contract-followups.md` 11·12항) |
| 추가 C-8 실제 연동 뼈대 | planning A2A·memory HTTP·안전 레이어 fail-closed, 가짜 서버 테스트 12개, 가정은 `real-integration-assumptions.md` |
| 추가 C-9 신고서 초안 브랜치 | 로컬 `draft/hr-safety-registry`(워크트리 `C:/wt/hr-registry-draft`, 푸시 없음), safety 테스트 62개 통과 |
| 추가 D-10 문서 변경안 | `contract-followups.md`(14개), `docs-change-proposals.md` |
| 추가 D-11 데모 스크립트·KPI | `demo-script-and-kpi.md` |
| 추가 8·9 문서 | `operations-guide.md`, `calling-hr-guide.md` |
| 추가 E 데모 화면 | 탭 순서·안내 문구·보류 큐 탭·헤더 배지 |

검증: hr 261개·레포 전체(support 제외) 670개 통과, `ruff check`·`lint-imports` 통과. 데모는 `uv run --package ax-agent-hr python -m hr_agent.demo` → http://localhost:8114.

## 2. 독립 리뷰가 잡은 것 (고침)
- **같은 멱등성 키로 승인이 다른 대상에 따라가던 구멍**: 관문 키에 대상 ID를 넣고, 같은 키가 다른 대상에 쓰이면 관문이 거부한다.
- 퇴직자가 후임이 될 수 있던 것, 퇴직 보류가 거절·만료돼도 잠금이 영구히 남던 것(이제 취소+해제, 잠금은 TTL로 만료).
- 재배치 적용이 반쯤만 바뀔 수 있던 것(이제 한 트랜잭션, 상태 선점), 확정 때 요청과 다른 후임을 받던 것.
- 안전 레이어에 닿지 못한 것을 "사람이 거절"로 처리하던 것(이제 `GATE_UNAVAILABLE`, 아무것도 안 바뀜).
- 퇴직·이동한 사람의 교대가 공백 계산을 왜곡하던 것(이제 활성 담당의 교대만 센다, 발령·퇴직 때 교대도 후임에게 이동).
- 결과 상태 규칙: 정책 거절은 `completed`+`outcome`, 보류는 `input-required`(`human_input`).
- 회사 기억 액션 이름을 실제 레지스트리 이름(`memory.card_write`, `memory.card_link`)으로.
- 카드 작성자는 요청 본문이 아니라 토큰의 요청자. 정규식 오탐("승인해"가 이유로 읽히는 것 등).

## 3. 고치지 않고 사람이 정할 것

| # | 내용 | 이유 |
|---|---|---|
| 1 | `scripts/compat-check.py`를 계약 PR #103 브랜치에서 고쳤다 | plan-reviewer가 "자동화 파일 변경은 `ci/` 브랜치로 분리하고 code owner 승인"을 요구. 분리해서 다시 올릴지 |
| 2 | `record_case` 요청자 목록을 7곳(orchestrator·planning·support·marketing·finance·data·infra)으로 넓혔다 | 리뷰는 "처음엔 좁게"를 권한다. 원래는 4곳(orchestrator·planning·support·infra). 어디까지 허용할지 |
| 3 | `confirm_handover_incomplete`(완성도 미달을 C로 올리는 별도 액션) | 요청 쪽이 등급을 고르는 구조라는 지적. 기준 키를 safety 기준표로 옮기는 안이 맞다(신고표 질문 4) |
| 4 | `confirm_reassignment` 검증자 형식 | safety는 `safety` 검증자에 코드 규칙을 쓰는데 "담당 공백·중복"을 표현할 규칙 종류가 없다(신고표 질문 1) |
| 5 | 브랜치 이름이 `feat/hr-impl`, 규칙은 `<부서 id>/<기능>`(예 `hr/...`) | PR을 만들 때 이름을 바꿔야 한다. 한 브랜치에 contracts·scripts가 섞여 있어 PR을 나눠야 한다 |
| 6 | 테이블에 `tenant_id`·`context_id`가 없는 것, `idempotency` 단수 이름 | 운영 저장소로 옮길 때 정리 |
| 7 | 운영 모드에서도 memory·planning 기본값이 목 | `HR_MEMORY_MODE=http`·`HR_PLANNING_MODE=a2a`로 켠다. 실제 서비스(memory API, planning skill)가 생겨야 한다 |
| 8 | 데모 보류 승인 엔드포인트와 목 안전 레이어가 `src/`에 있다 | 운영 이미지에 들어가지 않게 하려면 `demo/`·`clients/safety.py`의 목을 분리할지 정해야 한다 |
| 9 | 퇴직 확정이 승인된 뒤 공백 때문에 막혀도 승인 기록이 남아, 공백을 메운 뒤 같은 요청을 보내면 바로 적용된다 | 승인 시점과 적용 시점의 규칙이 다를 수 있다. 승인에 유효기간을 둘지 |
| 10 | 타이머가 인수인계를 스스로 시작할 때 내부 요청(토큰 `internal:hr`)을 쓴다 | 인증·ACL을 거치지 않는다. 공통 구성안에 시스템 시작 요청 규칙이 필요하다 |

## 4. 알려진 한계
- 응답 시간·정확도는 목·개발자 표본 기준이다(KPI 문서에 명시).
- memory REST 경로·봉투와 planning skill은 상대가 없어 hr이 가정했다.
- 계약 우회(`Card.extra`, `source_refs`의 `area:`)는 후속 계약 PR 전까지 그대로다.
- 레포 전체 `ruff format --check`는 hr 밖 파일 7개가 걸린다(기존 상태). `agents/support` 테스트 하나는 dev에서도 실패한다.

## 5. 아침에 할 일 제안
1. 3장의 1·2·3번(요청자 범위, 브랜치·PR 분리, safety 형식)부터 정한다.
2. 데모에서 "5 발령·퇴직 → 보류 큐" 흐름과 "감사 로그"를 한 번 눌러 본다.
3. `contract-followups.md`를 태윤·재현과 읽고 계약 PR 순서를 정한다.
4. planning skill·memory API가 생기면 3단계(목→실제 교체)를 시작한다.
