# contracts
담당: 준하·태윤 · 포트: 없음
## 1. 역할
팀 계약 원본. 에이전트·서비스가 주고받는 메시지·이벤트·Agent Card·실행 관문 입출력의 **형식(pydantic v2 모델과 Enum)** 만 정의한다. 구현 코드는 넣지 않는다.
계약은 `contracts/`에만 둔다. 각 폴더 안에 메시지·이벤트·API 형식을 새로 만들지 않는다.
## 2. 실행 방법
라이브러리 패키지라 서버가 없다. 테스트: `uv run pytest contracts/tests`
사용: `from ax_contracts.schemas import AgentRequest, AgentResult, EventEnvelope, AgentCard`
## 3. 입출력 계약
`src/ax_contracts/schemas/` (공통 규칙: 필드는 snake_case, 시간은 UTC, 금액은 정수 원, `tenant_id` 기본값 `shop-demo`)

| 파일 | 내용 |
| --- | --- |
| `agent_card.py` | `AgentCard`, `Skill` (B 등급은 `verifier` 필수, 자기 검증 금지) |
| `message.py` | `RequestMeta`, `AgentRequest`, `AgentResult`, `Evidence`, `TaskStatus`, `InputReason`, `ErrorCode` |
| `event.py` | `EventEnvelope`, `EventType`, 대표 이벤트 data 모델 5종과 `EVENT_DATA_MODELS` |
| `execute.py` | `ExecuteRequest`, `ExecuteDecision`, `HoldReason` |
| `record.py` | `RecordCase`, `RecordKind`, `Card` |
| `escalation.py`, `mcp.py` | `Escalation`, `McpEnvelope` |
| `ids.py` | ID·`context_id` 접두사 이름 (검증은 하지 않는다) |

- 데이터 접근 형식: `src/ax_contracts/data_access/` (설명은 `data-access/README.md`)
- OpenAPI: `openapi/` (shop-api·dashboard-api CI가 자동 생성)
- 이벤트 data 모델은 대표 5개만 있다. 나머지는 발행 담당(shop 이벤트는 재헌, safety 이벤트는 태윤)이 PR로 추가한다.
## 4. 의존
`pydantic`만. 다른 ax 패키지를 참조하지 않는다 (import-linter가 막는다).
## 5. 하지 않는 것
- 구현 로직(안전 레이어 호출, 인증 검증, LLM 호출 등)을 넣지 않는다
- 부서 전용 형식을 이 폴더 밖에 만들지 않는다
- 필드 삭제·이름 변경은 하위 호환을 깨므로 하지 않는다 (추가만, `contracts-compat`가 검사한다)
