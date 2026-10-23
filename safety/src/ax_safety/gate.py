"""실행 관문: 등급 판정과 A/B/C 처리 (SAF-01·03·04·05·06·08·10·11).

처리 순서 (재무 구성안 3-5, 공통 구성안 7장):

1. 멱등성: 같은 `(tenant_id, idempotency_key)`면 이전 판정을 그대로 돌려준다.
2. 레지스트리: 미등록 액션이거나 요청자가 액션 owner가 아니면 C(`unregistered_action`).
3. 금액은 코드가 계산한다(건수 × 단가 또는 금액 필드). 필드가 없거나 정수가 아니면
   C(`schema_invalid`), 요청 쪽이 적은 총액과 다르면 C(`amount_mismatch`).
4. 등급 = 레지스트리 기본 등급과 금액 규칙(건별·일일·사건별·고객별 누적) 중 높은 쪽.
   `claimed_grade`는 기록만 한다.
5. A는 `executed`. B는 검증자 skill로 교차검증해 승인이면 `executed`, 거절이면 `rejected`
   (`verify_rejected`), 무응답·오류면 C 보류(`verify_timeout`). C는 보류 큐에 넣고 `held`.
6. 모든 단계는 감사 로그에 남긴다. 감사 로그를 못 남기면 실행하지 않는다(fail-closed).

`executed`는 "요청 에이전트가 이제 쓰기 도구를 불러도 된다"는 뜻이다. 실제 쓰기는 요청 쪽이 한다.
C 건을 사람이 승인하면 같은 멱등성 키로 다시 요청했을 때 `executed`를 받는다.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from ax_contracts.schemas import ExecuteDecision, ExecuteRequest, HoldReason
from ax_safety.rules import ActionSpec, Registry, ThresholdTable, higher
from ax_safety.store import ExecutionRecord, Review, Store

logger = logging.getLogger("ax.safety.gate")


@dataclass(frozen=True)
class VerifyOutcome:
    result: Literal["approved", "rejected", "unavailable"]
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VerifyTask:
    """검증자에게 보내는 내용. `input`은 검증자 skill의 입력이다."""

    verifier: str
    skill: str
    context_id: str
    tenant_id: str
    execution_id: str
    timeout_seconds: float
    input: dict[str, Any]


Verifier = Callable[[VerifyTask], Awaitable[VerifyOutcome]]
Publisher = Callable[[str, ExecuteRequest | Review, dict[str, Any]], Awaitable[None]]


class AmountError(ValueError):
    pass


def compute_amount(spec: ActionSpec, params: dict[str, Any]) -> tuple[int | None, int | None, str]:
    """(금액, 단가, 계산식)을 돌려준다. 금액 규칙이 없는 액션은 (None, None, "")."""
    amount_spec = spec.amount
    if amount_spec is None:
        return None, None, ""

    def _int(name: str) -> int:
        value = params.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise AmountError(f"{name}는 정수여야 한다")
        if value < 0:
            raise AmountError(f"{name}는 0 이상이어야 한다")
        return value

    if amount_spec.amount_field is not None:
        amount = _int(amount_spec.amount_field)
        return amount, None, f"{amount_spec.amount_field} = {amount:,}원"
    count = _int(amount_spec.count_field or "")
    unit = _int(amount_spec.unit_field or "")
    amount = count * unit
    return amount, unit, f"{count:,} × {unit:,} = {amount:,}원"


def claimed_mismatches(spec: ActionSpec, params: dict[str, Any], amount: int) -> dict[str, Any]:
    """요청 쪽이 적은 총액 중 코드 계산값과 다른 것."""
    if spec.amount is None:
        return {}
    return {
        name: params[name]
        for name in spec.amount.claimed_fields
        if params.get(name) is not None and params[name] != amount
    }


class Gate:
    def __init__(
        self,
        registry: Registry,
        thresholds: ThresholdTable,
        store: Store,
        verifier: Verifier,
        publisher: Publisher,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        unknown = {r.action for r in thresholds.rules} - {s.action for s in registry.actions}
        if unknown:
            raise ValueError(f"기준표 규칙의 액션이 레지스트리에 없다: {sorted(unknown)}")
        self.registry = registry
        self.thresholds = thresholds
        self.store = store
        self._verifier = verifier
        self._publish = publisher
        self._now = now
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    # ------------------------------------------------------------------ 진입점
    async def execute(self, request: ExecuteRequest) -> ExecuteDecision:
        """`request.requester`는 호출한 쪽이 토큰에서 꺼낸 값으로 채워 넣어야 한다."""
        lock = self._locks.setdefault((request.tenant_id, request.action), asyncio.Lock())
        async with lock:
            previous = self.store.by_key(request.tenant_id, request.idempotency_key)
            if previous is not None:
                self._audit(request, "idempotent_replay", previous.execution_id)
                return previous.decision
            return await self._decide(request)

    # ------------------------------------------------------------------ 판정
    async def _decide(self, request: ExecuteRequest) -> ExecuteDecision:
        execution_id = f"exe_{uuid.uuid4().hex[:12]}"
        version = self.thresholds.version
        self._audit(
            request,
            "received",
            execution_id,
            {"action": request.action, "claimed_grade": request.claimed_grade},
        )

        spec = self.registry.get(request.action)
        if spec is None or request.requester not in spec.allowed_requesters:
            report = {"reason": "미등록 액션이거나 요청자가 이 액션을 신고하지 않았다"}
            return await self._hold(request, execution_id, HoldReason.UNREGISTERED_ACTION, report)

        try:
            amount, unit, formula = compute_amount(spec, request.params)
        except AmountError as exc:
            return await self._hold(
                request, execution_id, HoldReason.SCHEMA_INVALID, {"reason": str(exc)}
            )

        customer = self._param_str(spec.customer_field, request.params)
        report: dict[str, Any] = {"calculation": formula, "computed_amount": amount}
        if amount is not None:
            mismatched = claimed_mismatches(spec, request.params, amount)
            if mismatched:
                report["claimed"] = mismatched
                return await self._hold(
                    request,
                    execution_id,
                    HoldReason.AMOUNT_MISMATCH,
                    report,
                    amount=amount,
                    customer=customer,
                )

        hits = self._rule_hits(spec, request, amount, unit, customer)
        grade = spec.base_grade
        if hits:
            grade = higher(grade, "C")
            report["rules"] = hits
        if grade == "C":
            if not hits:
                report["rules"] = [{"key": "base_grade", "value": "C"}]
            return await self._hold(
                request,
                execution_id,
                HoldReason.OVER_THRESHOLD,
                report,
                amount=amount,
                customer=customer,
            )

        if grade == "A":
            decision = ExecuteDecision(
                execution_id=execution_id,
                grade="A",
                status="executed",
                computed_amount=amount,
                threshold_version=version,
            )
            return await self._finish(request, decision, customer, report, "safety.action_executed")

        if spec.rules_verified:  # 검증자 = 안전 레이어 코드 규칙. 규칙을 통과했으니 실행
            report["verification"] = {"verifier": spec.verifier, "result": "rules_passed"}
            decision = ExecuteDecision(
                execution_id=execution_id,
                grade="B",
                status="executed",
                computed_amount=amount,
                threshold_version=version,
                verifier=spec.verifier,
            )
            return await self._finish(request, decision, customer, report, "safety.action_executed")
        return await self._cross_verify(request, spec, execution_id, amount, customer, report)

    async def _cross_verify(
        self,
        request: ExecuteRequest,
        spec: ActionSpec,
        execution_id: str,
        amount: int | None,
        customer: str | None,
        report: dict[str, Any],
    ) -> ExecuteDecision:
        assert spec.verifier and spec.verify_skill  # 레지스트리 검증에서 보장
        task = VerifyTask(
            verifier=spec.verifier,
            skill=spec.verify_skill,
            context_id=request.context_id,
            tenant_id=request.tenant_id,
            execution_id=execution_id,
            timeout_seconds=self.thresholds.verify_timeout_seconds,
            input={
                "execution_id": execution_id,
                "action": request.action,
                "requester": request.requester,
                "params": request.params,
                "computed_amount": amount,
                "threshold_version": self.thresholds.version,
            },
        )
        self._audit(request, "verify_requested", execution_id, {"verifier": spec.verifier})
        try:
            outcome = await asyncio.wait_for(
                self._verifier(task), timeout=self.thresholds.verify_timeout_seconds
            )
        except Exception as exc:  # 무응답·오류는 모두 승인으로 보지 않는다(fail-closed)
            outcome = VerifyOutcome("unavailable", {"error": type(exc).__name__})
        self._audit(
            request,
            "verify_result",
            execution_id,
            {"verifier": spec.verifier, "result": outcome.result, **outcome.detail},
        )
        report["verification"] = {"verifier": spec.verifier, "result": outcome.result}

        if outcome.result == "unavailable":
            return await self._hold(
                request,
                execution_id,
                HoldReason.VERIFY_TIMEOUT,
                report,
                amount=amount,
                customer=customer,
                verifier=spec.verifier,
            )
        if outcome.result == "rejected":
            decision = ExecuteDecision(
                execution_id=execution_id,
                grade="C",
                status="rejected",
                computed_amount=amount,
                threshold_version=self.thresholds.version,
                verifier=spec.verifier,
                hold_reason=HoldReason.VERIFY_REJECTED,
            )
            return await self._finish(request, decision, customer, report, None)
        decision = ExecuteDecision(
            execution_id=execution_id,
            grade="B",
            status="executed",
            computed_amount=amount,
            threshold_version=self.thresholds.version,
            verifier=spec.verifier,
        )
        return await self._finish(request, decision, customer, report, "safety.action_executed")

    def _rule_hits(
        self,
        spec: ActionSpec,
        request: ExecuteRequest,
        amount: int | None,
        unit: int | None,
        customer: str | None,
    ) -> list[dict[str, Any]]:
        """걸린 규칙 목록. 금액이 아닌 규칙의 필드가 없거나 형식이 틀리면 걸린 것으로 본다."""
        hits: list[dict[str, Any]] = []
        for rule in self.thresholds.rules_for(spec.action):
            if any(request.params.get(k) != v for k, v in rule.when.items()):
                continue
            if rule.metric == "paths_within":
                paths = request.params.get(rule.field or "")
                if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                    hits.append({"key": rule.key, "invalid": rule.field})
                elif outside := rule.paths_outside(paths):
                    hits.append({"key": rule.key, "outside": outside, "allowed": rule.allowed})
                continue
            if rule.metric == "param":
                value = request.params.get(rule.field or "")
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    hits.append({"key": rule.key, "invalid": rule.field})
                    continue
                observed: int | None = value
            else:
                observed = self._amount_metric(rule.metric, spec, request, amount, unit, customer)
            if observed is not None and rule.hit(observed):
                hits.append(
                    {"key": rule.key, "observed": observed, "op": rule.op, "value": rule.value}
                )
        return hits

    def _amount_metric(
        self,
        metric: str,
        spec: ActionSpec,
        request: ExecuteRequest,
        amount: int | None,
        unit: int | None,
        customer: str | None,
    ) -> int | None:
        if amount is None:
            return None
        if metric == "amount":
            return amount
        if metric == "unit":
            return unit
        if metric == "daily_total":
            day_start = self._now().replace(hour=0, minute=0, second=0, microsecond=0)
            return amount + self.store.executed_total(
                request.tenant_id, spec.action, since=day_start
            )
        if metric == "context_total":
            return amount + self.store.executed_total(
                request.tenant_id, spec.action, context_id=request.context_id
            )
        if customer is None:  # customer_total
            return None
        return amount + self.store.executed_total(
            request.tenant_id, spec.action, context_id=request.context_id, customer=customer
        )

    # ------------------------------------------------------------------ 결과 처리
    async def _hold(
        self,
        request: ExecuteRequest,
        execution_id: str,
        reason: HoldReason,
        report: dict[str, Any],
        *,
        amount: int | None = None,
        customer: str | None = None,
        verifier: str | None = None,
    ) -> ExecuteDecision:
        now = self._now()
        review_id = f"rev_{uuid.uuid4().hex[:12]}"
        summary = request.params.get("summary")
        if isinstance(summary, str) and summary.strip():  # 사람이 읽을 설명. 요청 쪽 데이터다
            report = {"summary": summary.strip()[:200], **report}
        full_report = {
            "action": request.action,
            "requester": request.requester,
            "context_id": request.context_id,
            "idempotency_key": request.idempotency_key,
            "hold_reason": reason.value,
            "threshold_version": self.thresholds.version,
            "evidence": request.evidence.model_dump(mode="json"),
            "next_steps": ["approve", "reject", "adjust_and_resubmit"],
            **report,
        }
        review = Review(
            review_id=review_id,
            execution_id=execution_id,
            context_id=request.context_id,
            tenant_id=request.tenant_id,
            action=request.action,
            requester=request.requester,
            created_at=now,
            expires_at=now + timedelta(seconds=self.thresholds.review_timeout_seconds),
            report=full_report,
        )
        decision = ExecuteDecision(
            execution_id=execution_id,
            grade="C",
            status="held",
            computed_amount=amount,
            threshold_version=self.thresholds.version,
            verifier=verifier,
            hold_reason=reason,
            review_id=review_id,
        )
        result = await self._finish(request, decision, customer, full_report, "safety.action_held")
        if result.status == "held":
            self.store.add_review(review)
        return result

    async def _finish(
        self,
        request: ExecuteRequest,
        decision: ExecuteDecision,
        customer: str | None,
        report: dict[str, Any],
        event_type: str | None,
    ) -> ExecuteDecision:
        try:
            self._audit(
                request,
                "decided",
                decision.execution_id,
                decision.model_dump(mode="json", exclude_none=True),
            )
        except Exception:
            logger.exception("감사 로그 실패, 실행하지 않는다: %s", decision.execution_id)
            return ExecuteDecision(execution_id=decision.execution_id, grade="C", status="rejected")
        self.store.add_execution(
            ExecutionRecord(
                execution_id=decision.execution_id,
                created_at=self._now(),
                request=request,
                decision=decision,
                customer=customer,
                report=report,
            )
        )
        if event_type:
            await self._emit(event_type, request, self._event_data(request, decision))
        return decision

    # ------------------------------------------------------------------ 보류 큐 결정
    async def decide_review(
        self, review_id: str, decision: Literal["approved", "rejected"], by: str, note: str | None
    ) -> Review:
        """사람의 승인·거절. 이미 결정·만료된 건은 바꾸지 않고 ValueError."""
        await self.expire_reviews()
        review = self.store.get_review(review_id)
        if review is None:
            raise KeyError(review_id)
        if review.status != "pending":
            raise ValueError(f"이미 처리된 보류 건이다: {review.status}")
        self._close_review(review, decision, by, note)
        await self._emit_review(review)
        return review

    async def expire_reviews(self) -> list[Review]:
        """마감이 지난 보류 건을 자동 거절한다(`review_timeout`)."""
        now = self._now()
        expired = [r for r in self.store.list_reviews("pending") if r.expires_at <= now]
        for review in expired:
            self._close_review(review, "expired", "system", None)
            await self._emit_review(review)
        return expired

    def _close_review(
        self,
        review: Review,
        status: Literal["approved", "rejected", "expired"],
        by: str,
        note: str | None,
    ) -> None:
        review.status = status
        review.decided_by = by
        review.decided_at = self._now()
        review.note = note
        record = self.store.get_execution(review.execution_id)
        if record is not None:
            old = record.decision
            if status == "approved":
                new = old.model_copy(update={"status": "executed"})
            else:
                reason = HoldReason.REVIEW_TIMEOUT if status == "expired" else old.hold_reason
                new = old.model_copy(update={"status": "rejected", "hold_reason": reason})
            self.store.set_decision(review.execution_id, new)
        self.store.append_audit(
            self._now(),
            review.context_id,
            review.tenant_id,
            f"review_{status}",
            review.execution_id,
            {"review_id": review.review_id, "by": by, "note": note},
        )

    async def _emit_review(self, review: Review) -> None:
        decision = "approved" if review.status == "approved" else "rejected"
        data = {
            "review_id": review.review_id,
            "execution_id": review.execution_id,
            "action": review.action,
            "requester": review.requester,
            "decision": decision,
            "decided_by": review.decided_by,
        }
        await self._emit("safety.review_decided", review, data)

    # ------------------------------------------------------------------ 보조
    async def _emit(
        self, event_type: str, source: ExecuteRequest | Review, data: dict[str, Any]
    ) -> None:
        try:
            await self._publish(event_type, source, data)
        except Exception:  # 이벤트는 기록용이다. 판정 결과는 이미 감사 로그에 있다
            logger.warning("이벤트 발행 실패: %s %s", event_type, data.get("execution_id"))

    @staticmethod
    def _event_data(request: ExecuteRequest, decision: ExecuteDecision) -> dict[str, Any]:
        data = {
            "execution_id": decision.execution_id,
            "action": request.action,
            "requester": request.requester,
            "grade": decision.grade,
            "computed_amount": decision.computed_amount,
            "threshold_version": decision.threshold_version,
        }
        if decision.status == "held":
            data |= {"hold_reason": decision.hold_reason, "review_id": decision.review_id}
        return data

    @staticmethod
    def _param_str(name: str | None, params: dict[str, Any]) -> str | None:
        value = params.get(name) if name else None
        return value if isinstance(value, str) and value else None

    def _audit(
        self,
        request: ExecuteRequest,
        step: str,
        execution_id: str | None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.store.append_audit(
            self._now(),
            request.context_id,
            request.tenant_id,
            step,
            execution_id,
            {"requester": request.requester, **(detail or {})},
        )
