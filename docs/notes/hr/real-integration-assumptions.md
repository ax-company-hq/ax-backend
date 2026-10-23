# hr 실제 연동 뼈대: 가정 목록

코드: `agents/hr/src/hr_agent/clients/real.py`(planning·memory), `clients/safety.py`(`TemplateGate`), `clients/errors.py`.
기본은 모두 목이다. 켜는 방법:

| 환경변수 | 값 | 효과 |
|---|---|---|
| `HR_MOCK` | `1`(기본 목) / 없음 | 없으면 안전 레이어가 템플릿 `execute()`(실제 safety)로 바뀐다. 목 명부·진행 중 시드는 들어가지 않는다 |
| `HR_PLANNING_MODE` | `a2a` | planning을 A2A(`call_agent`)로 부른다 |
| `HR_MEMORY_MODE` | `http` | memory를 REST로 부른다 (`HR_MEMORY_URL`, 없으면 `AX_MEMORY_URL`, 없으면 `http://localhost:8002`) |

## 가정 (확인되면 `clients/real.py`만 고치면 된다)

### planning (예진)
- Agent Card에 `list_in_progress`, `verify_handover` skill이 있다. (지금 planning Card의 skill은 비어 있다)
- 입출력은 `contracts/.../planning.py`의 `ListInProgressInput/Output`, `VerifyHandoverInput/Output`이다.
- 호출 형식: `POST {planning}/a2a`, `meta.path`에 hr 이름이 붙고 토큰은 hr 토큰으로 바뀐다(`call_agent`가 처리). 마감은 `min(요청 마감, 지금 + 20초)`.
- 응답이 `completed`가 아니거나 출력 형식이 다르면 지어내지 않고 `UpstreamError` → hr 결과는 `failed` / `UPSTREAM_UNAVAILABLE`.

### memory (재현) — 지금 memory 서비스에는 `/health`만 있다
- REST 경로와 `{"data": ...}` 봉투는 hr이 정한 가정이다:
  - `POST /api/memory/cards` (카드 저장, 응답 `{data:{card_id}}`)
  - `GET /api/memory/cards/{id}`, `GET /api/memory/cards?area=&states=`
  - `GET /api/memory/cards/search?q=&area=&states=&kinds=&limit=` (응답 `{data:[{card, score}]}`)
  - `PATCH /api/memory/cards/{id}/status` (`{status, superseded_by}`), `PATCH /api/memory/cards/{id}/candidate` (`{candidate}`)
- 점수(`score`)는 0~1이고 클수록 비슷하다. hr은 이 점수로 한 번 더 걸러(`check_plan`의 `_score`) 쓴다.
- 카드의 영역·기각 여부 등은 지금처럼 `Card.extra`에 담는다(계약 후속 PR 전까지).

### 안전 레이어 (태윤)
- hr은 템플릿 `execute()`만 부른다. 템플릿이 safety에 닿지 못하면 `rejected`(fail-closed)를 돌려주고 hr은 아무것도 바꾸지 않는다(`outcome: gate_rejected`).
- 액션 이름: 회사 기억은 레지스트리 등록 이름(`memory.card_write`, `memory.card_link`)을 쓴다. 나머지 hr 액션은 아직 미등록이라 safety가 C로 보류한다(`safety-grade-registration-draft.md`).

## 오류 처리 규칙
- 다른 서비스 오류는 `UpstreamError`로 올라와 핸들러 감싸개(`handlers._guarded`)가 `failed` / `UPSTREAM_UNAVAILABLE`로 바꾼다. 메시지에는 서비스 이름만 싣는다(상태 코드·내부 문구 제외).
- 쓰기 전에 오류가 나면 상태가 바뀌지 않는다. 카드를 쓴 뒤 연결(대체) 단계에서 오류가 나면 같은 요청을 다시 보내면 이어서 처리된다(관문 키가 대상별이라 중복 실행되지 않는다).
- 타이머(`HR_TIMER`)는 오류가 나도 멈추지 않고 감사 로그(`timer.upstream_error`)만 남긴다.

## 테스트
`agents/hr/tests/test_real_clients.py`: 가짜 memory·planning 서버(`httpx.MockTransport`)로 호출 형식, 오류 처리, hr 전체 흐름(기각 카드 저장 → 계획 비교 경고, 인수인계 초안에 planning 항목 반영)을 확인한다.
