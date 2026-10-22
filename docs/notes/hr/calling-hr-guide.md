# 다른 부서용 hr 호출 안내서

hr에게 "이 일이 있었다"고 알리는 가장 흔한 방법은 `record_case`다. 이 문서는 그 요청을 보내는 방법과 흔한 실수를 정리한다.
(전체 skill 표는 `agents/hr/README.md`, 형식 원본은 `contracts/src/ax_contracts/schemas/record.py`의 `RecordCase`.)

## 1. 언제 부르나
- 결정이 내려졌을 때: 기획 확정, 롤백 실행, PR 머지, 요청 종료, **기각한 안**
- 장애·사례를 남길 때: `incident`, `case`, `notice`
- 부르는 쪽이 판단하지 않는다. 결정인지 아닌지, 확신이 높은지는 hr이 정한다. 사실(요약·이유·대안·근거)만 정확히 보낸다.

## 2. 요청 예시 (결정 + 기각된 대안)
`POST http://hr:8104/a2a`

```json
{
  "meta": {
    "context_id": "ctx_inc_0001",
    "token": "<에이전트 토큰>",
    "path": ["orchestrator"],
    "deadline": "2026-10-23T01:00:30Z",
    "idempotency_key": "ctx_inc_0001:record_case:pr_412"
  },
  "skill": "record_case",
  "input": {
    "context_id": "ctx_inc_0001",
    "kind": "spec_confirmed",
    "summary": "결제 재시도를 최대 2회로 확정했다",
    "decision_reason": "중복 결제 위험을 줄이기 위해",
    "alternatives_considered": ["재시도 없음", "무제한 재시도"],
    "source_refs": ["pr:412", "area:payment"],
    "author": "planning"
  }
}
```

응답(요약):

```json
{
  "status": "completed",
  "outcome": "card_created",
  "output": {"outcome": "card_created", "card_id": "card_ab12cd34ef56", "confidence": "high"},
  "decision_reason": "중복 결제 위험을 줄이기 위해",
  "alternatives_considered": ["재시도 없음", "무제한 재시도"],
  "evidence": {"ref_ids": ["pr:412", "area:payment", "card_ab12cd34ef56"]}
}
```

`outcome` 값:

| 값 | 뜻 |
|---|---|
| `card_created` | 확신이 높아 카드가 바로 확정됨 |
| `candidate` | 근거가 부족해 후보 카드로 보류됨 (이유·대안·근거를 더 채워 다시 보내면 좋다) |
| `record_only` | 결정이 없는 기록이라 카드 없이 기록만 남김 |
| `duplicate` | 같은 기록이 이미 처리됨 (정상, 다시 처리하지 않는다) |
| `gate_held` 등 | 안전 레이어가 막음. 재시도하지 말고 사람 승인을 기다린다 |

## 3. 꼭 지킬 것
1. **`idempotency_key`는 필수**다. `context_id + 액션 + 대상` 형태로, 같은 이벤트면 항상 같은 값을 쓴다. 다르면 같은 기록이 카드 두 장이 된다.
2. **영역은 `source_refs`에 `area:<영역>`으로** 넣는다(`payment`, `search`, `catalog`, `support-desk`, `infra-ops`, `inventory`). 없으면 영역별 검색·인수인계·계획 비교에서 이 카드를 찾지 못한다.
3. **이유와 대안을 비우지 않는다.** 이유·대안·근거가 모두 있어야 확신도가 high다. 모르면 비워 두면 되고(지어내지 않는다) 그 카드는 후보가 된다.
4. **외부 문구(고객 메시지, 메일 본문 등)는 `untrusted`에** 넣는다. `summary`나 `decision_reason`에 그대로 붙이지 않는다. hr은 `untrusted`를 지시로 읽지 않는다.
5. **`path`를 이어 붙인다.** 받은 `path`에 자기 이름을 더해 보낸다. hr 이름이 이미 있으면 루프로 거부된다.
6. **`deadline`은 30초 안**으로 둔다. 넘기면 처리하지 않고 `timeout`을 돌려준다.

## 4. 흔한 실수

| 증상 | 원인 | 고치기 |
|---|---|---|
| `rejected` / `FORBIDDEN_SKILL` | 요청자가 Agent Card의 `allowed_requesters`에 없음 | 토큰의 에이전트 이름 확인. `record_case`는 orchestrator·planning·support·marketing·finance·data·infra만 가능 |
| `failed` / `INVALID_INPUT` | 입력이 `RecordCase` 형식과 다름, `idempotency_key` 없음 | 필드 이름과 `kind` 값(`spec_confirmed` 등 10종) 확인 |
| 카드가 계속 `candidate` | 이유·대안·근거 중 둘 이상이 비어 있음 | 사실만큼은 채워서 보낸다 |
| 같은 결정이 카드 두 장 | `idempotency_key`가 매번 달라짐 | 대상(PR 번호 등)으로 키를 고정 |
| 기각한 안을 보냈는데 계획 비교에 안 걸림 | `kind`가 `decision_rejected`가 아니거나 `area:` 누락 | 둘 다 확인 |
| 보내고 아무 변화 없음 | `outcome`이 `record_only` | 결정 문장(`decision_reason` 또는 `summary`)이 비어 있지 않은지 확인 |

## 5. 조회용 skill (상태를 바꾸지 않는다)
- `get_roster`: 영역별 담당·교대와 공백 영역. 입력 `{"area": "payment"}`(생략하면 전체). 개인 정보는 없고 영역·역할·담당자 ID만 나온다.
- `check_plan`: 새 계획 초안이 과거 기각·확정 결정과 충돌하는지. 입력 `{"draft": {"title", "summary", "area", "kind": "spec|ticket|deploy_plan"}}`.
  결과의 `verdict`가 `warn`이면 `findings[].card_ids`가 근거 카드다. 관련 카드가 없으면 `no_related_cards: true`(= "기록에 없음")이며 판정하지 않는다.

## 6. 직접 불러 보기
```bash
uv run ax-test-call --agent http://localhost:8104 --skill record_case --input input.json --as planning
```
눈으로 확인하려면 데모(`uv run --package ax-agent-hr python -m hr_agent.demo` → http://localhost:8114)의 **카드 덱** 탭에서 같은 입력을 보낼 수 있다.
