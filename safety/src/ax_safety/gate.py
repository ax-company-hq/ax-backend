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
import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from ax_contracts.schemas import ExecuteDecision, ExecuteRequest, HoldReason
from ax_safety.rules import ActionSpec, Registry, ThresholdTable, higher
from ax_safety.store import ExecutionRecord, Review, Store, ThresholdChange

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


@dataclass(frozen=True)
class ReviewOutcome:
    """AI 1차 판단 결과. `escalate`면 사람이 결정한다."""

    result: Literal["approve", "reject", "escalate"]
    reason_code: str = ""
    explanation: str = ""


@dataclass(frozen=True)
class ReviewTask:
    reviewer: str
    skill: str
    context_id: str
    tenant_id: str
    review_id: str
    timeout_seconds: float
    input: dict[str, Any]


@dataclass(frozen=True)
class ThresholdReviewTask:
    """재무 AI에게 기준 바꾸기를 묻는 내용. `untrusted`는 요청한 사람이 쓴 이유(믿지 않는 글)."""

    reviewer: str
    skill: str
    change_id: str
    context_id: str
    tenant_id: str
    timeout_seconds: float
    input: dict[str, Any]
    untrusted: dict[str, Any] = field(default_factory=dict)


Verifier = Callable[[VerifyTask], Awaitable[VerifyOutcome]]
Reviewer = Callable[[ReviewTask], Awaitable[ReviewOutcome]]
ThresholdReviewer = Callable[[ThresholdReviewTask], Awaitable[ReviewOutcome]]

# ---- 기준 바꾸기 (재무 AI가 결정, 안전 장치는 코드로 강제)
THRESHOLD_REVIEWER = "finance"
THRESHOLD_REVIEW_SKILL = "review_threshold_change"
GUARDRAIL = "guardrail"  # 안전 장치 범위 밖이라 거절한 기준 바꾸기의 decided_by
TIMEOUT_KEY = "review_timeout_seconds"
STEP_MIN_RATIO = 0.5  # TOML 원래 값의 0.5배 ~ 2배 안에서만 바꿀 수 있다
STEP_MAX_RATIO = 2.0
TIMEOUT_BOUNDS = (3600, 259200)  # 사람 결정 마감: 1시간 ~ 3일
REASON_MAX = 300
TENANT = "shop-demo"

AI_REVIEWER = "finance-ai"  # 재무 AI가 결정한 보류 건의 decided_by
RULE_REVIEWER = "auto-rule"  # 안전 장치 규칙이 바로 거절한 보류 건의 decided_by
Publisher = Callable[[str, ExecuteRequest | Review, dict[str, Any]], Awaitable[None]]


AUTO_REJECT_NOTE = {
    "amount_mismatch": (
        "적어 낸 금액과 계산이 달라 자동으로 거절했습니다. "
        "요청한 AI가 금액을 고쳐 다시 요청해야 합니다."
    ),
    "unregistered_action": (
        "등록되지 않은 일이라 자동으로 거절했습니다. "
        "필요한 일이면 재무 담당자에게 등록을 요청하세요."
    ),
    "schema_invalid": (
        "요청 내용이 잘못되어 자동으로 거절했습니다. 요청한 AI가 고쳐 다시 요청해야 합니다."
    ),
}


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
        reviewer: Reviewer | None = None,
        threshold_reviewer: ThresholdReviewer | None = None,
    ) -> None:
        unknown = {r.action for r in thresholds.rules} - {s.action for s in registry.actions}
        if unknown:
            raise ValueError(f"기준표 규칙의 액션이 레지스트리에 없다: {sorted(unknown)}")
        self.registry = registry
        self.thresholds = thresholds
        self.baseline = thresholds  # TOML 원래 값. 기준 바꾸기의 허용 범위는 이 값으로 정한다
        self._threshold_reviewer = threshold_reviewer
        self._threshold_lock = asyncio.Lock()
        self.store = store
        self._verifier = verifier
        self._reviewer = reviewer
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
        if amount is not None:  # 이번 금액을 더한 누적. AI 1차 판단과 근거 리포트에 쓴다
            report["totals"] = {
                metric: self._amount_metric(metric, spec, request, amount, unit, customer)
                for metric in ("daily_total", "context_total", "customer_total")
            }
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
        if result.status != "held":
            return result
        self.store.add_review(review)
        return await self._auto_review(request, review, result)

    async def _auto_review(
        self, request: ExecuteRequest, review: Review, held: ExecuteDecision
    ) -> ExecuteDecision:
        """심각하지 않은 C 건을 사람보다 먼저 판단한다. 판단하지 못하면 사람에게 둔다."""
        cfg = self.thresholds.auto_review
        if not cfg.enabled:
            return held
        reason = review.report.get("hold_reason", "")
        if reason in cfg.auto_reject_reasons:
            note = AUTO_REJECT_NOTE.get(reason, "요청이 잘못되어 자동으로 거절했습니다.")
            self._close_review(review, "rejected", RULE_REVIEWER, note)
            await self._emit_review(review)
            return self._current(review, held)
        if self._reviewer is None or request.action not in cfg.actions:
            return held

        task = ReviewTask(
            reviewer=cfg.reviewer,
            skill=cfg.skill,
            context_id=request.context_id,
            tenant_id=request.tenant_id,
            review_id=review.review_id,
            timeout_seconds=cfg.timeout_seconds,
            input={
                "review_id": review.review_id,
                "execution_id": review.execution_id,
                "action": request.action,
                "requester": request.requester,
                "params": request.params,
                "computed_amount": held.computed_amount,
                "hold_reason": reason,
                "rules": review.report.get("rules", []),
                "totals": review.report.get("totals", {}),
            },
        )
        try:
            outcome = await asyncio.wait_for(self._reviewer(task), timeout=cfg.timeout_seconds)
        except Exception as exc:  # AI가 답하지 않으면 사람에게 둔다
            outcome = ReviewOutcome("escalate", "reviewer_unavailable", type(exc).__name__)
        review.report["ai_review"] = {
            "result": outcome.result,
            "reason_code": outcome.reason_code,
            "explanation": outcome.explanation,
        }
        self._audit(request, "ai_review", review.execution_id, dict(review.report["ai_review"]))
        if outcome.result == "escalate":
            return held
        status = "approved" if outcome.result == "approve" else "rejected"
        self._close_review(review, status, AI_REVIEWER, outcome.explanation or None)
        await self._emit_review(review)
        return self._current(review, held)

    def _current(self, review: Review, fallback: ExecuteDecision) -> ExecuteDecision:
        record = self.store.get_execution(review.execution_id)
        return record.decision if record else fallback

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

    # ------------------------------------------------------------------ 기준 바꾸기
    # 기준표는 메모리에서 바꾼다(TODO: persist). 재시작하면 TOML 원래 값으로 돌아간다.
    def baseline_values(self) -> dict[str, int]:
        """바꿀 수 있는 기준과 TOML 원래 값."""
        values = {r.key: r.value for r in self.baseline.rules if r.metric != "paths_within"}
        values[TIMEOUT_KEY] = int(self.baseline.review_timeout_seconds)
        return values

    def current_value(self, key: str) -> int | None:
        if key == TIMEOUT_KEY:
            return int(self.thresholds.review_timeout_seconds)
        rule = next((r for r in self.thresholds.rules if r.key == key), None)
        if rule is None or rule.metric == "paths_within":
            return None
        return rule.value

    def guardrail_problem(self, key: str, new_value: int, reason: str) -> str | None:
        """안전 장치(코드) 검사. 문제가 있으면 쉬운 한국어 이유, 없으면 None."""
        if key.startswith("auto_review"):
            return "AI 판단 켜기·끄기 같은 설정은 사람만 바꿀 수 있습니다."
        if any(r.key == key and r.metric == "paths_within" for r in self.baseline.rules):
            return "허용 경로 목록은 숫자 기준이 아니라 바꿀 수 없습니다."
        baseline = self.baseline_values().get(key)
        if baseline is None:
            return "바꿀 수 있는 기준이 아닙니다. 기준 이름을 확인하세요."
        if not reason.strip():
            return "바꾸는 이유를 적어 주세요."
        if len(reason) > REASON_MAX:
            return f"이유는 {REASON_MAX}자 안으로 적어 주세요."
        if new_value <= 0:
            return "새 값은 0보다 큰 정수여야 합니다."
        low, high = math.ceil(baseline * STEP_MIN_RATIO), int(baseline * STEP_MAX_RATIO)
        if not low <= new_value <= high:
            return (
                f"처음 정한 값 {baseline:,}의 절반({low:,})부터 두 배({high:,})까지만 "
                f"바꿀 수 있습니다. 요청한 값: {new_value:,}"
            )
        if key == TIMEOUT_KEY and not TIMEOUT_BOUNDS[0] <= new_value <= TIMEOUT_BOUNDS[1]:
            return "사람 결정 마감은 1시간(3600초)부터 3일(259200초)까지만 정할 수 있습니다."
        if new_value == self.current_value(key):
            return "지금 값과 같습니다."
        return None

    def threshold_stats(self, key: str) -> dict[str, Any]:
        """재무 AI 판단 근거. 숫자는 모두 safety 기록에서 코드로 센다(최근 24시간)."""
        now = self._now()
        since = now - timedelta(hours=24)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        reviews = [
            r for r in self.store.list_reviews() if r.tenant_id == TENANT and r.created_at >= since
        ]
        if key != TIMEOUT_KEY:  # 그 기준 때문에 멈춘 건만
            reviews = [
                r for r in reviews if any(h.get("key") == key for h in r.report.get("rules", []))
            ]
        approved = [r for r in reviews if r.status == "approved"]
        rule = next((r for r in self.baseline.rules if r.key == key), None)
        action = rule.action if rule else None
        return {
            "action": action,
            "holds_24h": len(reviews),
            "executed_today": (
                self.store.executed_total(TENANT, action, since=day_start) if action else 0
            ),
            "ai_approved_24h": sum(r.decided_by == AI_REVIEWER for r in approved),
            "human_approved_24h": sum(
                r.decided_by not in (AI_REVIEWER, RULE_REVIEWER, "system") for r in approved
            ),
            "expired_24h": sum(r.status == "expired" for r in reviews),
        }

    async def request_threshold_change(
        self, key: str, new_value: int, reason: str, requested_by: str
    ) -> ThresholdChange:
        """기준 바꾸기 요청. 안전 장치 → 재무 AI 순서. 사람 결정이 필요하면 `pending`."""
        async with self._threshold_lock:
            change = ThresholdChange(
                change_id=f"thc_{uuid.uuid4().hex[:12]}",
                key=key,
                old_value=self.current_value(key),
                new_value=new_value,
                baseline=self.baseline_values().get(key),
                reason=reason[:REASON_MAX],
                requested_by=requested_by,
                created_at=self._now(),
            )
            self.store.add_change(change)
            self._change_audit(change, "threshold_change_requested")
            problem = self.guardrail_problem(key, new_value, reason)
            if problem:
                self._close_change(change, "rejected", GUARDRAIL, problem)
                return change
            change.stats = self.threshold_stats(key)
            if self._threshold_reviewer is None:
                change.explanation = "재무 AI에 묻지 못해 재무 담당자의 결정을 기다립니다."
                return change
            outcome = await self._ask_threshold_reviewer(change, reason)
            change.ai_review = {
                "result": outcome.result,
                "reason_code": outcome.reason_code,
                "explanation": outcome.explanation,
            }
            self._change_audit(change, "threshold_change_ai_review", change.ai_review)
            if outcome.result == "approve":
                self._apply_change(change, AI_REVIEWER, outcome.explanation)
            elif outcome.result == "reject":
                self._close_change(change, "rejected", AI_REVIEWER, outcome.explanation)
            else:
                change.explanation = (
                    outcome.explanation or "재무 AI가 답하지 않아 재무 담당자의 결정을 기다립니다."
                )
            return change

    async def decide_threshold_change(
        self, change_id: str, decision: Literal["approved", "rejected"], by: str, note: str | None
    ) -> ThresholdChange:
        """사람의 승인·거절. 이미 결정된 건이면 ValueError, 없으면 KeyError."""
        async with self._threshold_lock:
            change = self.store.get_change(change_id)
            if change is None:
                raise KeyError(change_id)
            if change.status != "pending":
                raise ValueError(f"이미 처리된 기준 바꾸기다: {change.status}")
            if decision == "rejected":
                self._close_change(change, "rejected", by, note or "재무 담당자가 거절했습니다.")
                return change
            # 사람이 승인해도 안전 장치 범위는 넘을 수 없다 (그사이 다른 변경이 들어왔을 수 있다)
            problem = self.guardrail_problem(change.key, change.new_value, change.reason)
            if problem:
                self._close_change(change, "rejected", GUARDRAIL, problem)
                return change
            self._apply_change(change, by, note or "재무 담당자가 승인했습니다.")
            return change

    async def _ask_threshold_reviewer(self, change: ThresholdChange, reason: str) -> ReviewOutcome:
        assert self._threshold_reviewer is not None
        timeout = self.thresholds.auto_review.timeout_seconds
        task = ThresholdReviewTask(
            reviewer=THRESHOLD_REVIEWER,
            skill=THRESHOLD_REVIEW_SKILL,
            change_id=change.change_id,
            context_id=self._change_context(change),
            tenant_id=TENANT,
            timeout_seconds=timeout,
            input={
                "change_id": change.change_id,
                "key": change.key,
                "current_value": change.old_value,
                "new_value": change.new_value,
                "baseline": change.baseline,
                "stats": change.stats,
            },
            untrusted={"reason_untrusted_text": reason},
        )
        try:
            return await asyncio.wait_for(self._threshold_reviewer(task), timeout=timeout)
        except Exception:  # 답이 없거나 늦으면 사람에게
            return ReviewOutcome("escalate", "reviewer_unavailable", "")

    def _apply_change(self, change: ThresholdChange, by: str, explanation: str) -> None:
        """기준표를 바꾸고 version을 올린다. 이후 모든 판정이 새 값을 쓴다."""
        table = self.thresholds
        change.old_value = self.current_value(change.key)
        if change.key == TIMEOUT_KEY:
            update: dict[str, Any] = {TIMEOUT_KEY: float(change.new_value)}
        else:
            update = {
                "rules": [
                    r.model_copy(update={"value": change.new_value}) if r.key == change.key else r
                    for r in table.rules
                ]
            }
        self.thresholds = table.model_copy(update={**update, "version": table.version + 1})
        change.version_after = self.thresholds.version
        self._close_change(change, "applied", by, explanation)

    def _close_change(
        self,
        change: ThresholdChange,
        status: Literal["applied", "rejected"],
        by: str,
        explanation: str,
    ) -> None:
        change.status = status
        change.decided_by = by
        change.decided_at = self._now()
        change.explanation = explanation
        self._change_audit(change, f"threshold_change_{status}")

    @staticmethod
    def _change_context(change: ThresholdChange) -> str:
        return f"ctx_req_{change.change_id}"

    def _change_audit(
        self, change: ThresholdChange, step: str, extra: dict[str, Any] | None = None
    ) -> None:
        self.store.append_audit(
            self._now(),
            self._change_context(change),
            TENANT,
            step,
            None,
            {
                "change_id": change.change_id,
                "key": change.key,
                "old_value": change.old_value,
                "new_value": change.new_value,
                "requested_by": change.requested_by,
                "decided_by": change.decided_by,
                "version_after": change.version_after,
                **(extra or {}),
            },
        )

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
