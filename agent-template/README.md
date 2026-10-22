# agent-template
담당: 준하 · 포트: 없음
## 1. 역할
모든 에이전트가 시작하는 공통 뼈대. Agent Card 공개, A2A 요청 수신, 실행 관문, 에이전트 호출, LLM 호출, 이벤트 발행·구독의 **시그니처와 규칙**을 정한다.
v0은 인터페이스 확정이 목적이라 내부 로직 중 일부(실제 토큰 검증, safety 호출, LLM 호출, Redis, 이관 큐 연동)는 `TODO` 스텁이다. 목 모드(`AGENT_TEMPLATE_MOCK=1`)에서는 단독 테스트가 돌아간다.
## 2. 실행 방법
라이브러리 패키지라 서버가 없다. 테스트: `uv run pytest agent-template/tests`
테스트 클라이언트: `uv run ax-test-call --agent http://localhost:8106 --skill impact_analysis --input input.json`
(기본으로 `orchestrator`인 것처럼 요청한다. `--as <이름>`으로 요청자를 바꾸고, 목 모드의 토큰은 `mock:<이름>`이다)

### 새 에이전트를 이 템플릿으로 시작하는 방법
`agents/hr/`가 최소 예시다. 복사해서 시작한다.

1. `agents/<폴더>/`에 `pyproject.toml`, `README.md`(5항목), `Dockerfile`, `.env.example`, `tests/`를 둔다. 패키지 이름은 `<부서>_agent`다.
2. `agent-card.json`을 쓴다. 형식은 `contracts`의 `AgentCard`다. skill마다 `id`, `description`, `input_schema`, `output_schema`, `grade`(`read`·`A`·`B`·`C`), `allowed_requesters`, `verifier`(B 필수, 자기 자신 금지), `priority`를 적는다.
3. `handlers.py`에 skill마다 `async def <skill_id>(request: AgentRequest) -> AgentResult`를 만들고 `HANDLERS = {"<skill_id>": handler}`로 모은다. 카드의 skill과 핸들러 키는 정확히 같아야 한다.
4. `main.py`에서 `app = create_agent_app(AgentCard.model_validate_json(...), HANDLERS)`로 앱을 만든다.
5. 상태를 바꾸는 일은 `from ax_agent_template.execute import execute`로만 한다. 쓰기 MCP 도구는 `executed`를 받은 뒤에만 부르고 LLM 도구 목록에서 뺀다.
6. 다른 에이전트는 `call_agent(target, req)`로 부른다. LLM은 `LLMClient`로만 부르고 테스트에서는 `MockLLMClient`를 쓴다.
7. 단독 테스트(공통 8항목: Agent Card·결정성·루프·권한·멱등성·인젝션·계산·상태 변경)를 `tests/`에 쓴다.

### 모듈

| 파일 | 내용 |
| --- | --- |
| `server.py` | `create_agent_app(card, handlers)`. 수신 순서: context_id·tenant_id → 토큰 → 루프 → 홉 제한(이관 큐) → ACL → 마감 → 핸들러 → 결과 검증 |
| `execute.py` | `execute(action, params, context, evidence) -> ExecuteDecision`. safety로 전송, 응답 없으면 거부(fail-closed) |
| `client.py` | `call_agent(target, req)`. path에 자기 이름 추가, 마감 초과는 `TIMEOUT` |
| `llm.py` | `LLMClient`, `MockLLMClient`, `LLMSettings`(`<부서>_LLM_*` → 공통 `LLM_*`), `format_prompt`(`untrusted`는 데이터 블록으로만) |
| `events.py` | `publish`, `subscribe` (Redis Streams, 목 모드는 메모리 버스) |
| `queue.py` | `SerialQueue` 인터페이스 (마케팅 보상 쿠폰용, 스텁) |
| `logging.py` | `log_event(context_id, tenant_id, kind, payload)` |
| `testclient.py` | `ax-test-call` CLI |
| `config.py` | 포트 표, 에이전트 이름·토큰·주소, 목 모드 |

## 3. 입출력 계약
`contracts/` 참조 (`AgentCard`, `AgentRequest`, `AgentResult`, `EventEnvelope`, `ExecuteRequest`, `ExecuteDecision`).
A2A 전송은 v0에서 `POST /a2a`(JSON) 한 개다. a2a-sdk 연결은 TODO다.
## 4. 의존
`ax-contracts`, `fastapi`, `httpx`
## 5. 하지 않는 것
- 부서 전용 로직을 넣지 않는다 (공통 뼈대만)
- 안전장치를 우회하는 경로를 만들지 않는다
- 형식을 새로 정의하지 않는다 (계약은 `contracts/`에 있다)
- `import queue`·`import logging`은 표준 라이브러리를 뜻한다. 이 패키지의 모듈은 항상 `ax_agent_template.queue`처럼 쓴다. 모듈 폴더 안에서 `python 파일.py`로 직접 실행하지 않는다 (표준 라이브러리를 가린다).
