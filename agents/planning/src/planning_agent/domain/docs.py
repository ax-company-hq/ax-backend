"""정리 문서 블록 모으기 (프로토타입 `data.ts` gatherBlocks).

요청 하나에 걸린 것(기획서 문단·작업·PR·대화·결정·장애)을 모아 정리 문서 블록으로 만든다.
"""

from __future__ import annotations

from planning_agent.domain.models import DEPT_NAME, Doc, DocBlock, State


def next_block_id(doc: Doc) -> str:
    def num(block_id: str) -> int:
        try:
            return int(block_id[1:])
        except ValueError:
            return 0

    return f"b{max((num(b.id) for b in doc.blocks), default=0) + 1}"


def gather_blocks(s: State, about: str) -> list[DocBlock]:
    r = next((x for x in s.requests if x.id == about), None)
    if r is None:
        return []
    out: list[tuple[str, str, bool | None]] = []

    def add(kind: str, text: str, done: bool | None = None) -> None:
        out.append((kind, text, done))

    add("h2", "요청")
    add("ref", f"{r.id} · {DEPT_NAME[r.from_]} · {r.title} · 상태 {r.state}")
    linked = f" · {r.duplicate_of}에 연결된 요청" if r.duplicate_of else ""
    add("p", f"근거: {r.evidence}{linked}")
    spec_id = r.spec
    if spec_id is None:
        origin = next((y for y in s.requests if y.id == r.duplicate_of), None)
        spec_id = origin.spec if origin else None
    spec = next((x for x in s.specs if x.id == spec_id), None)
    tasks = [t for t in s.tasks if spec and t.spec == spec.id]
    task_ids = {t.id for t in tasks}
    prs = [p for p in s.prs if p.task in task_ids]
    thread = next((t for t in s.threads if spec and t.on == spec.id), None)
    decisions = [x for x in s.decisions if spec and x.spec == spec.id]
    incidents = [i for i in s.incidents if spec and any(c.spec == spec.id for c in i.candidates)]
    if spec:
        add("h2", f"기획 문서 {spec.id} · {spec.title} ({spec.status})")
        add("p", f"문제: {spec.problem} / 목표: {spec.goal}")
        for p in spec.paras:
            kind = "요구사항" if p.kind == "req" else "완료 기준"
            warn = f" · 경고 {p.warn.card}" if p.warn else ""
            inc = f" · 장애 {p.incident}" if p.incident else ""
            add("ref", f"{p.id} {kind} · {p.text}{warn}{inc}")
    if tasks:
        add("h2", "작업·PR")
        for t in tasks:
            add("ref", f"{t.id} {t.title} · {t.state}")
        for p in prs:
            auto = " (자동)" if p.auto else ""
            add(
                "ref",
                f"{p.id} {p.title} · 검수 {p.reviews[-1]} · "
                f"{'머지됨' if p.merged else '열림'}{auto}",
            )
    if (thread and thread.messages) or decisions:
        add("h2", "대화·결정")
        for m in thread.messages if thread else []:
            add("p", f"{m.who}: {m.text}")
        for x in decisions:
            add("ref", f"{x.id} 결정 · {x.text} · 이유 {x.reason} · 대안 {x.alternatives}")
    if incidents:
        add("h2", "장애 연결")
        for i in incidents:
            follow = f" · 재발 방지 {i.follow_up}" if i.follow_up else ""
            add(
                "ref",
                f"{i.context_id} {i.title} · 관련 PR "
                f"{', '.join(c.pr for c in i.candidates)}{follow}",
            )
    add("h2", "남은 일")
    if spec and spec.question:
        add("todo", f"질문 답 받기: {spec.question}", False)
    if spec and spec.status == "초안":
        add("todo", f"{spec.id} 기획서 확정", False)
    for p in prs:
        if not p.merged:
            add("todo", f"{p.id} 검수·머지", False)
    add("todo", f"{DEPT_NAME[r.from_]}에 결과 회신", r.state == "완료")
    return [
        DocBlock(id=f"b{i + 1}", kind=kind, text=text, done=done)
        for i, (kind, text, done) in enumerate(out)
    ]
