""" "이 코드 왜 이렇게 했지?" 역추적 (프로토타입 `trace.ts`).

코드 줄 → PR → 작업 → 기획서 문단 → 결정 → 요청 → 근거. 모든 연결은 문단 id로 이어져 있어서
코드 규칙으로만 따라간다 (LLM 없음).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from planning_agent.domain.models import PR, State

StepKind = Literal[
    "코드", "PR", "작업", "문단", "결정", "기각 이력", "기획서", "요청", "근거", "장애"
]


class CodeLine(BaseModel):
    path: str
    line: int
    text: str


class TraceStep(BaseModel):
    kind: StepKind
    title: str
    detail: str | None = None
    href: str | None = None


# 목 데이터에는 실제 diff가 없어 PR이 바꾼 대표 줄을 문단에 맞춰 둔다.
# 실제로는 github_get_pull의 변경 줄이 들어온다.
_CODE_BY_PARA: dict[str, list[tuple[str, int, str]]] = {
    "SPEC-5:R3": [("shop-api/payment/config.py", 12, "PAYMENT_TIMEOUT_MS = 8000")],
    "SPEC-7:R1": [
        ("shop-api/cart/service.py", 41, "if payment.failed: keep_cart(cart, mark_sold_out=True)"),
        ("shop-api/cart/service.py", 42, 'return CartResult(kept=True, reason="payment_failed")'),
    ],
    "SPEC-7:R2": [
        ("shop-web/checkout/FailNotice.tsx", 18, '<RetryButton to="/checkout" keepCart />')
    ],
}


def code_lines_for(
    spec: str | None, para: str | None, gate_paths: str, label: str
) -> list[CodeLine]:
    rows = _CODE_BY_PARA.get(f"{spec}:{para}")
    if rows:
        return [CodeLine(path=p, line=n, text=t) for p, n, t in rows]
    return [CodeLine(path=f"{gate_paths}pages/notice.tsx", line=7, text=f'label: "{label}"')]


def changed_paths_for(spec: str | None, para: str | None, gate_paths: str) -> list[str]:
    """PR의 바뀐 파일 경로 (역추적 코드 줄과 같은 규칙)."""
    lines = code_lines_for(spec, para, gate_paths, "")
    return list(dict.fromkeys(line.path for line in lines))


def code_of(s: State, pr: PR) -> list[CodeLine]:
    task = next((t for t in s.tasks if t.id == pr.task), None)
    label = task.title if task else pr.title
    return code_lines_for(
        task.spec if task else None, task.para if task else None, pr.gate.paths, label
    )


def trace_of(s: State, pr_id: str, line: CodeLine | None = None) -> list[TraceStep]:
    pr = next((p for p in s.prs if p.id == pr_id), None)
    if pr is None:
        return []
    task = next((t for t in s.tasks if t.id == pr.task), None)
    spec = next((x for x in s.specs if task and x.id == task.spec), None)
    para = next((p for p in spec.paras if task and p.id == task.para), None) if spec else None
    req = next((r for r in s.requests if spec and r.id == spec.request), None)
    decisions = [x for x in s.decisions if spec and x.spec == spec.id]
    steps: list[TraceStep] = []
    code = line or code_of(s, pr)[0]
    steps.append(TraceStep(kind="코드", title=f"{code.path}:{code.line}", detail=code.text))
    how = ("자동 머지" if pr.auto else "사람 머지") if pr.merged else "머지 전"
    steps.append(
        TraceStep(
            kind="PR",
            title=f"{pr.id} {pr.title}",
            detail=f"{pr.branch} · {how} · 검수 {' → '.join(pr.reviews)}",
            href="#/work/prs",
        )
    )
    if task:
        steps.append(
            TraceStep(
                kind="작업",
                title=f"{task.id} {task.title}",
                detail=f"상태 {task.state}",
                href="#/work/prs",
            )
        )
    if para and spec:
        steps.append(
            TraceStep(
                kind="문단", title=f"{spec.id} {para.id}", detail=para.text, href="#/work/specs"
            )
        )
        if para.warn:
            steps.append(TraceStep(kind="기각 이력", title=para.warn.card, detail=para.warn.text))
        if para.incident:
            steps.append(
                TraceStep(
                    kind="장애",
                    title=para.incident,
                    detail="이 문단이 장애에 연결됨 (link_incident)",
                    href="#/work/aftercare",
                )
            )
    for x in decisions:
        steps.append(
            TraceStep(
                kind="결정",
                title=f"{x.id} {x.text}",
                detail=f"이유: {x.reason} · 검토한 대안: {x.alternatives} · 출처: {x.from_}",
            )
        )
    if spec:
        auto = " · 자동 확정 (사람 확정 없음)" if spec.auto else ""
        steps.append(
            TraceStep(
                kind="기획서",
                title=f"{spec.id} {spec.title}",
                detail=f"문제: {spec.problem} · 목표: {spec.goal}{auto}",
                href="#/work/specs",
            )
        )
    if req:
        origin = "사람" if req.from_ == "human" else req.via
        steps.append(
            TraceStep(
                kind="요청",
                title=f"{req.id} {req.title}",
                detail=f"{origin} · {req.evidence}",
                href="#/requests/incoming",
            )
        )
    if spec and spec.basis:
        steps.append(
            TraceStep(kind="근거", title=f"근거 {len(spec.basis)}건", detail=" / ".join(spec.basis))
        )
    return steps


def trace_text(steps: list[TraceStep]) -> str:
    """어시스턴트 답변용 한 문단."""
    if not steps:
        return ""

    def pick(kind: str) -> list[TraceStep]:
        return [x for x in steps if x.kind == kind]

    code = pick("코드")[0]
    pr = pick("PR")
    parts = [f"{code.title}의 `{code.detail}`는 {pr[0].title if pr else None}에서 들어왔습니다."]
    para = pick("문단")
    if para:
        parts.append(f'이 PR은 {para[0].title} "{para[0].detail}"를 구현한 것입니다.')
    warn = pick("기각 이력")
    if warn:
        parts.append(f"이 문단에는 기각 이력 {warn[0].title}({warn[0].detail})가 붙어 있습니다.")
    dec = pick("결정")
    if dec:
        joined = ", ".join(f"{d.title} ({d.detail})" for d in dec)
        parts.append(f"관련 결정은 {joined}입니다.")
    req = pick("요청")
    if req:
        pieces = (req[0].detail or "").split(" · ")
        basis = pieces[1] if len(pieces) > 1 else "기록 없음"
        parts.append(f"출발점은 {req[0].title}이고 근거는 {basis}입니다.")
    return " ".join(parts)
