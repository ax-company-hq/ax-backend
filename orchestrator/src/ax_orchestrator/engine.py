"""plan 실행 엔진 (ORC-05·06·07·08·09·10).

엔진은 시나리오를 모른다. plan(단계·선행·skill)을 받아 순차·병렬로 부서를 부르고,
`input-required`를 중계하고, 규칙으로 이관하고, 실패를 재시도하고, 사건을 닫는다.
분석 문장·금액·등급은 만들지 않는다. 모든 값은 부서 결과에서 온 것이다.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from ax_contracts.schemas import (
    AgentRequest,
    AgentResult,
    ErrorCode,
    InputReason,
    RequestMeta,
    TaskStatus,
)
from ax_orchestrator.models import TERMINAL, Incident, Plan, Step, StepRun, iso, now
from ax_orchestrator.registry import Registry

Caller = Callable[[str, AgentRequest], Awaitable[AgentResult]]
Publisher = Callable[[str, Incident], Awaitable[None]]
# 규칙으로 안 풀릴 때만 쓰는 LLM 선택기: (skill, 후보 부서들) -> 부서 이름 또는 None
LlmPicker = Callable[[str, list[str]], Awaitable[str | None]]

TRANSFER_CODES = {ErrorCode.FORBIDDEN_SKILL.value, ErrorCode.HOP_LIMIT.value, "not_my_scope"}
MAX_FAILED_RETRIES = 2
MAX_CANCELED_RETRIES = 1


class Hold:
    """사람 승인을 기다리는 단계(C 등급 보류). 총괄은 승인하지 않는다."""

    def __init__(self) -> None:
        self.pending = True
        self.approved: bool | None = None
        self.reason = ""
        self.event = asyncio.Event()

    def decide(self, approved: bool, reason: str) -> None:
        if not self.pending:
            return
        self.pending = False
        self.approved = approved
        self.reason = reason
        self.event.set()


class Engine:
    def __init__(
        self,
        registry: Registry,
        caller: Caller,
        *,
        publish: Publisher | None = None,
        hold_timeout: float = 600.0,
        retry_delay: float = 0.0,
        step_timeout: float = 30.0,
        llm_pick: LlmPicker | None = None,
        author: str = "orchestrator",
    ) -> None:
        self.registry = registry
        self.caller = caller
        self.publish = publish
        self.hold_timeout = hold_timeout
        self.retry_delay = retry_delay
        self.step_timeout = step_timeout
        self.llm_pick = llm_pick
        self.author = author
        self._locks: dict[str, asyncio.Lock] = {}
        self._sequence: dict[str, int] = {}

    # ---------- 실행 ----------
    async def run(self, inc: Incident, plan: Plan) -> Incident:
        for step in plan.steps:
            inc.steps[step.id] = StepRun(step.id, step.skill, step.label)
        inc.log(f"사건 시작: {inc.title}")
        await self._publish("incident.opened", inc)

        by_id = {s.id: s for s in plan.steps}
        pending = set(by_id)
        running: dict[asyncio.Task[None], str] = {}
        while pending or running:
            progressed = True
            while progressed:  # 건너뛴 단계가 다른 단계의 선행을 끝내므로 변화가 없을 때까지 반복
                progressed = self._schedule(inc, by_id, pending, running)
            if not running:
                break
            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                running.pop(task)
                task.result()
        await self._close(inc, plan)
        return inc

    def _schedule(
        self,
        inc: Incident,
        by_id: dict[str, Step],
        pending: set[str],
        running: dict[asyncio.Task[None], str],
    ) -> bool:
        progressed = False
        for sid in sorted(pending):
            step = by_id[sid]
            deps = (*step.after, *step.also_after)
            if not all(inc.steps[d].status in TERMINAL for d in deps):
                continue
            pending.discard(sid)
            progressed = True
            failed = [d for d in step.after if inc.steps[d].status != "completed"]
            if failed:
                run = inc.steps[sid]
                run.status = "skipped"
                run.note = f"선행 단계 미완료({', '.join(failed)})"
                run.finished_at = now()
                inc.log(f"{step.label}: 건너뜀 — {run.note}")
                continue
            running[asyncio.create_task(self._run_step(inc, step))] = sid
        return progressed

    async def _run_step(self, inc: Incident, step: Step) -> None:
        run = inc.steps[step.id]
        run.started_at = now()
        run.status = "running"
        if step.kind == "record":
            await self._record(inc, step, run)
            return
        entry = self.registry.find(step.skill)
        if entry is None:
            self._skip(inc, run, "담당 부서를 확인할 수 없음 (Agent Card에 skill 없음)")
            return
        run.dept, run.grade = entry.dept, entry.grade
        if not entry.callable_by_orchestrator:
            self._skip(inc, run, "총괄이 요청할 수 없는 skill (allowed_requesters)")
            return
        inc.log(f"{step.label}: {entry.dept}에 요청")
        payload = step.build(inc)
        await self._call_and_handle(inc, step, run, entry.dept, payload)

    def _skip(self, inc: Incident, run: StepRun, note: str) -> None:
        run.status, run.note, run.finished_at = "skipped", note, now()
        inc.log(f"{run.label}: 건너뜀 — {note}")

    # ---------- 호출·재시도·이관·중계 ----------
    def _request(
        self, inc: Incident, step: Step, skill: str, payload: dict[str, Any], dept: str
    ) -> AgentRequest:
        sequence = None
        if step.serial_group:
            sequence = self._sequence.get(inc.context_id + step.serial_group, 0) + 1
            self._sequence[inc.context_id + step.serial_group] = sequence
        meta = RequestMeta(
            context_id=inc.context_id,
            token="orchestrator",
            path=["orchestrator"],
            deadline=now() + timedelta(seconds=self.step_timeout),
            idempotency_key=f"{inc.context_id}:{skill}:{step.id}",
        )
        return AgentRequest(meta=meta, skill=skill, input=payload, sequence=sequence)

    async def _send(
        self, inc: Incident, step: Step, skill: str, payload: dict[str, Any], dept: str
    ) -> AgentResult:
        lock = self._locks.setdefault(f"{inc.context_id}:{step.serial_group}", asyncio.Lock())
        req = self._request(inc, step, skill, payload, dept)
        if step.serial_group:
            async with lock:
                return await self.caller(dept, req)
        return await self.caller(dept, req)

    async def _call_and_handle(
        self, inc: Incident, step: Step, run: StepRun, dept: str, payload: dict[str, Any]
    ) -> None:
        failed_retries = canceled_retries = 0
        while True:
            run.attempts += 1
            result = await self._send(inc, step, step.skill, payload, dept)
            status, outcome = result.status, result.outcome or ""

            if status == TaskStatus.COMPLETED and outcome not in TRANSFER_CODES:
                self._done(inc, run, result)
                return
            if status == TaskStatus.INPUT_REQUIRED:
                done = await self._relay(inc, step, run, dept, payload, result)
                if done:
                    return
                continue_payload = run.output.pop("_next_payload", None)
                if continue_payload is None:
                    return
                payload = continue_payload
                continue
            if outcome in TRANSFER_CODES and status in (TaskStatus.REJECTED, TaskStatus.COMPLETED):
                await self._transfer(inc, step, run, dept, payload, outcome)
                return
            if status == TaskStatus.CANCELED and canceled_retries < MAX_CANCELED_RETRIES:
                canceled_retries += 1
                inc.log(f"{run.label}: 마감 초과, 재시도 {canceled_retries}")
                await asyncio.sleep(self.retry_delay)
                continue
            if status == TaskStatus.FAILED and failed_retries < MAX_FAILED_RETRIES:
                failed_retries += 1
                inc.log(f"{run.label}: 실패({outcome}), 재시도 {failed_retries}")
                await asyncio.sleep(self.retry_delay)
                continue
            self._fail(inc, run, f"{status.value}: {outcome or '사유 없음'}")
            return

    def _done(self, inc: Incident, run: StepRun, result: AgentResult) -> None:
        run.status = "completed"
        run.output = dict(result.output)
        run.note = result.outcome or ""
        run.finished_at = now()
        inc.outputs[run.step_id] = run.output
        inc.log(f"{run.label}: 완료")

    def _fail(self, inc: Incident, run: StepRun, note: str) -> None:
        run.status, run.note, run.finished_at = "failed", note, now()
        inc.log(f"{run.label}: 실패 — {note}")

    async def _transfer(
        self, inc: Incident, step: Step, run: StepRun, dept: str, payload: dict[str, Any], why: str
    ) -> None:
        """이관 큐: 규칙(같은 skill을 가진 다른 부서)으로 먼저, 안 되면 LLM 선택기."""
        run.reason_code = why
        candidates = [
            a for a in self.registry.alternatives(step.skill, dept) if a.callable_by_orchestrator
        ]
        target = candidates[0].dept if candidates else None
        used_llm = False
        if target is None and self.llm_pick is not None:
            names = sorted(self.registry.cards)
            target = await self.llm_pick(step.skill, [n for n in names if n != dept])
            used_llm = target is not None
        if target is None:
            self._fail(inc, run, f"이관 실패({why}): 맞는 부서를 찾지 못함")
            return
        inc.log(f"{run.label}: {dept} → {target} 이관({'LLM' if used_llm else '규칙'})")
        run.transferred_to = target
        run.dept = target
        result = await self._send(inc, step, step.skill, payload, target)
        if result.status == TaskStatus.COMPLETED and (result.outcome or "") not in TRANSFER_CODES:
            self._done(inc, run, result)
            run.note = f"이관 처리({'LLM' if used_llm else '규칙'})"
        else:
            self._fail(inc, run, f"이관 후에도 실패: {result.status.value}")

    async def _relay(
        self,
        inc: Incident,
        step: Step,
        run: StepRun,
        dept: str,
        payload: dict[str, Any],
        result: AgentResult,
    ) -> bool:
        """input-required 중계. 끝났으면 True, 다시 호출해야 하면 False(+ `_next_payload`)."""
        reason = result.reason_code
        run.reason_code = reason.value if reason else None
        if reason == InputReason.HUMAN_INPUT:
            return await self._wait_human(inc, step, run, dept, payload, result)
        if reason == InputReason.DEPT_INPUT:
            question = result.output.get("question") or {}
            q_skill = question.get("skill")
            entry = self.registry.find(q_skill) if q_skill else None
            if entry is None or not entry.callable_by_orchestrator:
                self._fail(inc, run, "다른 부서의 판단이 필요하지만 물을 부서를 찾지 못함")
                return True
            inc.log(f"{run.label}: {entry.dept}에 질의({q_skill})")
            answer = await self._send(inc, step, q_skill, question.get("input", {}), entry.dept)
            if answer.status != TaskStatus.COMPLETED:
                self._fail(inc, run, f"질의 실패: {answer.status.value}")
                return True
            run.output["_next_payload"] = {**payload, "answers": {q_skill: dict(answer.output)}}
            return False
        run.status = "skipped"
        run.note = "고객 자료 대기 — 사건 요약에 남김"
        run.finished_at = now()
        inc.log(f"{run.label}: 고객 입력 대기(보류)")
        return True

    async def _wait_human(
        self,
        inc: Incident,
        step: Step,
        run: StepRun,
        dept: str,
        payload: dict[str, Any],
        result: AgentResult,
    ) -> bool:
        hold = Hold()
        inc.holds[step.id] = hold
        run.status = "held"
        run.note = (result.output or {}).get("hold_reason", "") or "사람 승인 대기"
        inc.log(f"{run.label}: C 등급 보류 — 승인 대기열에 올림")
        try:
            await asyncio.wait_for(hold.event.wait(), timeout=self.hold_timeout)
        except TimeoutError:
            hold.decide(False, "review_timeout")
        if hold.approved:
            inc.log(f"{run.label}: 사람이 승인 — 재개")
            run.status = "running"
            run.attempts += 1
            again = await self._send(
                inc, step, step.skill, {**payload, "review_decision": "approved"}, dept
            )
            if again.status == TaskStatus.COMPLETED:
                self._done(inc, run, again)
            else:
                self._fail(inc, run, f"승인 후 재개 실패: {again.status.value}")
        else:
            run.status = "rejected_by_human"
            run.note = "시간 초과로 자동 거절" if hold.reason == "review_timeout" else "사람이 거절"
            run.finished_at = now()
            inc.log(f"{run.label}: {run.note}")
        return True

    def decide_hold(self, inc: Incident, step_id: str, approved: bool) -> bool:
        hold = inc.holds.get(step_id)
        if hold is None or not hold.pending:
            return False
        hold.decide(approved, "human")
        return True

    # ---------- 기록·종료 ----------
    async def _record(self, inc: Incident, step: Step, run: StepRun) -> None:
        entry = self.registry.find(step.skill)
        if entry is None:
            self._skip(inc, run, "담당 부서를 확인할 수 없음 (Agent Card에 skill 없음)")
            return
        run.dept, run.grade = entry.dept, entry.grade
        done = [r for r in inc.steps.values() if r.status == "completed"]
        problems = [
            r for r in inc.steps.values() if r.status in ("failed", "skipped", "rejected_by_human")
        ]
        payload = {
            "context_id": inc.context_id,
            "kind": "incident",
            "summary": f"{inc.title} — 완료 {len(done)}단계, 미완료 {len(problems)}단계",
            "decision_reason": (
                f"{inc.kind} plan 실행 결과 — 완료: {', '.join(r.skill for r in done) or '없음'}; "
                f"미완료: {', '.join(r.skill for r in problems) or '없음'}"
            ),
            "alternatives_considered": [
                *(f"{r.skill}: {r.status} ({r.note})" for r in problems),
                *(
                    f"{r.skill}: {r.dept}로 이관 ({r.reason_code})"
                    for r in done
                    if r.transferred_to
                ),
            ],
            "source_refs": [f"step:{r.step_id}" for r in done],
            "author": self.author,
            "area": "infra",
        }
        await self._call_and_handle(inc, step, run, entry.dept, payload)

    async def _close(self, inc: Incident, plan: Plan) -> None:
        required = {s.id for s in plan.steps if s.required}
        ok = all(inc.steps[i].status == "completed" for i in required)
        inc.status = "resolved" if ok else "degraded"
        inc.closed_at = now()
        inc.summary = [self._summary_line(r) for r in inc.steps.values()]
        statuses = [r.status for r in inc.steps.values()]
        retries = sum(max(r.attempts - 1, 0) for r in inc.steps.values())
        inc.metrics = {
            "mttr_seconds": round((inc.closed_at - inc.opened_at).total_seconds(), 2),
            "steps_total": len(statuses),
            "steps_completed": statuses.count("completed"),
            "held": sum(1 for r in inc.steps.values() if r.reason_code == "human_input"),
            "transfers": sum(1 for r in inc.steps.values() if r.transferred_to),
            "retries": retries,
            "auto_rate": round(
                1 - sum(1 for r in inc.steps.values() if r.reason_code) / max(len(statuses), 1), 2
            ),
        }
        inc.log(f"사건 종료: {inc.status}")
        await self._publish("incident.resolved", inc)

    @staticmethod
    def _summary_line(run: StepRun) -> str:
        who = run.dept or "미정"
        text = {
            "completed": "완료",
            "failed": f"실패 ({run.note})",
            "skipped": f"건너뜀 ({run.note})",
            "rejected_by_human": f"보류 후 거절 ({run.note})",
        }.get(run.status, run.status)
        return f"{run.label} [{who}] {text}"

    async def _publish(self, kind: str, inc: Incident) -> None:
        if self.publish is not None:
            await self.publish(kind, inc)


def new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex[:12]}"


__all__ = ["Engine", "Hold", "iso", "new_event_id"]
