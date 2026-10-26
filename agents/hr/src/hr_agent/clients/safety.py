"""안전 레이어(실행 관문) 연결. 상태를 바꾸는 모든 액션은 여기를 거친다.

- `TemplateGate`: 템플릿의 `execute()`로 실제 안전 레이어를 부른다(3단계).
- `MockGate`: 안전 레이어를 시뮬레이션한다. 등급 A는 바로 실행, B는 검증자가 확인한 뒤 실행,
  C는 보류 큐에 넣고 사람이 승인해야 실행한다. 미등록 액션은 C로 보류한다.

등급 신고(레지스트리의 원본은 태윤의 `safety`)는 `HR_ACTIONS`에 적은 초안이다. 같은
`idempotency_key`의 두 번째 요청은 이전 결과를 돌려준다. 보류는 정해진 시간(기본 10분)이
지나면 자동 거절된다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from ax_contracts.schemas import Evidence, ExecuteDecision, HoldReason, RequestMeta
from hr_agent.clients.errors import UpstreamError
from hr_agent.config import Clock
from hr_agent.store import Store
from hr_agent.util import dumps, loads, stable_id


@dataclass(frozen=True)
class ActionSpec:
    grade: Literal["A", "B", "C"]
    verifier: str | None = None  # B 교차검증 주체 (표시용 이름)


# 등급 신고 초안 (인사 구성안 HR2 5장). 레지스트리 원본은 safety의 registry.toml이다.
HR_ACTIONS: dict[str, ActionSpec] = {
    "memory.card_write": ActionSpec("A"),  # 레지스트리 등록 이름(회사 기억)
    "memory.card_link": ActionSpec("A"),  # 대체·폐기 연결, hr만 요청 가능
    "record_case": ActionSpec("A"),
    "start_handover": ActionSpec("A"),
    "answer_handover_question": ActionSpec("A"),
    "confirm_handover": ActionSpec("B", verifier="planning"),
    "confirm_handover_incomplete": ActionSpec("C"),  # 완성도 미달이면 사람이 보고 결정한다
}


@dataclass(frozen=True)
class VerifyOutcome:
    approved: bool
    detail: dict[str, Any]


Verifier = Callable[[dict[str, Any], RequestMeta], Awaitable[VerifyOutcome]]


class GateClient(Protocol):
    async def execute(
        self, action: str, params: dict[str, Any], context: RequestMeta, evidence: Evidence
    ) -> ExecuteDecision: ...


class TemplateGate:
    """템플릿 `execute()`로 실제 안전 레이어를 부른다."""

    async def execute(
        self, action: str, params: dict[str, Any], context: RequestMeta, evidence: Evidence
    ) -> ExecuteDecision:
        from ax_agent_template.execute import execute

        try:
            return await execute(action, params, context, evidence)
        except (NotImplementedError, OSError) as exc:  # 안전 레이어에 닿지 못했다: 실행하지 않는다
            raise UpstreamError("safety", type(exc).__name__) from exc


class MockGate:
    def __init__(
        self,
        store: Store,
        clock: Clock,
        hold_ttl_minutes: int = 10,
        registry: dict[str, ActionSpec] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._ttl = timedelta(minutes=hold_ttl_minutes)
        self.registry = dict(HR_ACTIONS if registry is None else registry)
        self._verifiers: dict[str, Verifier] = {}
        self.calls: list[tuple[str, str]] = []  # (action, 결과 status) 기록, 테스트·데모용

    def register_verifier(self, action: str, verifier: Verifier) -> None:
        self._verifiers[action] = verifier

    # ---- execute ----------------------------------------------------------
    async def execute(
        self, action: str, params: dict[str, Any], context: RequestMeta, evidence: Evidence
    ) -> ExecuteDecision:
        key = context.idempotency_key
        if not key:
            raise ValueError("상태 변경에는 idempotency_key가 필요하다")
        self.expire()
        row = self._store.one(
            "SELECT decision, action, params FROM gate_decisions WHERE idem_key = ?", (key,)
        )
        if row:
            # 같은 키를 다른 액션·다른 대상에 다시 쓰면 저장된 결정을 돌려주지 않고 거부한다
            saved = loads(row["params"], {})
            if row["action"] != action or saved.get("target") != params.get("target"):
                self.calls.append((action, "conflict"))
                return ExecuteDecision(
                    execution_id=stable_id("exe_", key, action, str(params.get("target"))),
                    grade="C",
                    status="rejected",
                    hold_reason=HoldReason.VERIFY_REJECTED,
                )
            return ExecuteDecision.model_validate(loads(row["decision"]))
        decision = await self._decide(action, params, context, key)
        self._save(key, action, context.context_id, params, decision)
        self.calls.append((action, decision.status))
        return decision

    async def _decide(
        self, action: str, params: dict[str, Any], context: RequestMeta, key: str
    ) -> ExecuteDecision:
        exe_id = stable_id("exe_", key)
        spec = self.registry.get(action)
        if spec is None:  # 미등록 액션은 C로 보류한다
            return self._held(exe_id, key, HoldReason.UNREGISTERED_ACTION)
        if spec.grade == "A":
            return ExecuteDecision(execution_id=exe_id, grade="A", status="executed")
        if spec.grade == "B":
            verifier = self._verifiers.get(action)
            if verifier is None:  # 검증자가 없으면 실행하지 않는다 (fail-closed)
                return ExecuteDecision(
                    execution_id=exe_id,
                    grade="B",
                    status="rejected",
                    verifier=spec.verifier,
                    hold_reason=HoldReason.VERIFY_TIMEOUT,
                )
            outcome = await verifier(params, context)
            if outcome.approved:
                return ExecuteDecision(
                    execution_id=exe_id, grade="B", status="executed", verifier=spec.verifier
                )
            return ExecuteDecision(
                execution_id=exe_id,
                grade="B",
                status="rejected",
                verifier=spec.verifier,
                hold_reason=HoldReason.VERIFY_REJECTED,
            )
        # 신고 등급이 C: 기준 금액이 없는 액션이라 보류 사유는 `over_threshold`를 임시로 쓴다.
        return self._held(exe_id, key, HoldReason.OVER_THRESHOLD)

    @staticmethod
    def _held(exe_id: str, key: str, reason: HoldReason) -> ExecuteDecision:
        return ExecuteDecision(
            execution_id=exe_id,
            grade="C",
            status="held",
            hold_reason=reason,
            review_id=stable_id("rev_", key),
        )

    def _save(
        self,
        key: str,
        action: str,
        context_id: str,
        params: dict[str, Any],
        decision: ExecuteDecision,
    ) -> None:
        now = self._clock().isoformat()
        summary = str(params.get("summary") or action)
        self._store.execute(
            "INSERT INTO gate_decisions(idem_key, action, context_id, decision, summary, params,"
            " status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                action,
                context_id,
                dumps(decision.model_dump(mode="json")),
                summary,
                dumps(params),
                decision.status,
                now,
            ),
        )

    # ---- 보류 큐 (C) ---------------------------------------------------------
    def list_holds(self, status: str | None = None) -> list[dict[str, Any]]:
        """보류 건 목록. 표시용 `summary`를 포함한다(대시보드 보류 큐가 보여 줄 값)."""
        self.expire()
        rows = self._store.all(
            "SELECT * FROM gate_decisions WHERE json_extract(decision, '$.review_id') IS NOT NULL"
            " ORDER BY created_at, idem_key"
        )
        out = []
        for row in rows:
            decision = ExecuteDecision.model_validate(loads(row["decision"]))
            state = "pending" if decision.status == "held" else decision.status
            if status is not None and state != status:
                continue
            out.append(
                {
                    "review_id": decision.review_id,
                    "action": row["action"],
                    "context_id": row["context_id"],
                    "summary": row["summary"],
                    "status": state,
                    "hold_reason": decision.hold_reason,
                    "created_at": row["created_at"],
                    "decided_at": row["decided_at"],
                }
            )
        return out

    def decide_hold(self, review_id: str, approved: bool, by: str = "human") -> ExecuteDecision:
        """사람이 보류 건을 승인·거절한다. 승인하면 같은 키의 결과가 실행됨이 된다."""
        row = self._row_by_review(review_id)
        if row is None:
            raise KeyError(review_id)
        decision = ExecuteDecision.model_validate(loads(row["decision"]))
        if decision.status != "held":
            return decision  # 이미 결정됐다
        updated = decision.model_copy(
            update={"status": "executed" if approved else "rejected"}
            | ({} if approved else {"hold_reason": HoldReason.VERIFY_REJECTED})
        )
        self._store.execute(
            "UPDATE gate_decisions SET decision = ?, status = ?, decided_at = ? WHERE idem_key = ?",
            (
                dumps(updated.model_dump(mode="json")),
                updated.status,
                self._clock().isoformat(),
                row["idem_key"],
            ),
        )
        self.calls.append((f"review:{by}", updated.status))
        return updated

    def expire(self) -> int:
        """정해진 시간 안에 처리되지 않은 보류 건을 자동 거절한다."""
        now = self._clock()
        count = 0
        for row in self._store.all("SELECT * FROM gate_decisions WHERE status = 'held'"):
            if now - _parse(row["created_at"]) < self._ttl:
                continue
            decision = ExecuteDecision.model_validate(loads(row["decision"]))
            expired = decision.model_copy(
                update={"status": "rejected", "hold_reason": HoldReason.REVIEW_TIMEOUT}
            )
            self._store.execute(
                "UPDATE gate_decisions SET decision = ?, status = ?, decided_at = ?"
                " WHERE idem_key = ?",
                (
                    dumps(expired.model_dump(mode="json")),
                    "rejected",
                    now.isoformat(),
                    row["idem_key"],
                ),
            )
            count += 1
        return count

    def _row_by_review(self, review_id: str) -> dict[str, Any] | None:
        return self._store.one(
            "SELECT * FROM gate_decisions WHERE json_extract(decision, '$.review_id') = ?",
            (review_id,),
        )


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)
