"""프로젝트별 보기 (프로토타입 `projects.tsx`의 gather·stageDone).

변경 요청 1건(context_id 하나)을 프로젝트 하나로 보고 요청 → 작업 → 운영 → 처리 → 소통 → 문서를
모은다. 같은 요청으로 연결만 한 것(중복 연결)은 원본 프로젝트에 붙인다. 단계 판정은 코드 규칙이다.
"""

from __future__ import annotations

from planning_agent.domain.models import Request, State, dump

STAGES = ["요청", "작업", "운영", "처리", "소통", "문서"]


def projects_of(s: State) -> list[Request]:
    return [r for r in s.requests if r.state != "중복 연결"]


def _order(r: Request) -> int:
    if r.state in ("작업 중", "기획 중"):
        return 0
    return 1 if r.state == "접수" else 2


def sorted_projects(s: State) -> list[Request]:
    """화면 왼쪽 목록 순서: 진행 중 → 접수 → 나머지 (같은 묶음 안은 등록 순)."""
    return sorted(projects_of(s), key=_order)


def gather(s: State, r: Request) -> dict:
    spec = next((x for x in s.specs if x.id == r.spec or x.request == r.id), None)
    tasks = [t for t in s.tasks if spec and t.spec == spec.id]
    task_ids = {t.id for t in tasks}
    prs = [p for p in s.prs if p.task in task_ids]
    pr_ids = {p.id for p in prs}
    incidents = [
        i
        for i in s.incidents
        if any(c.pr in pr_ids for c in i.candidates) or i.context_id == r.context_id
    ]
    inc_ctx = {i.context_id for i in incidents}
    follow_ids = {i.follow_up for i in incidents}
    dups = [x for x in s.requests if x.duplicate_of == r.id]
    follow = [x for x in s.requests if x.id in follow_ids and x.id != r.id]
    ids = [x for x in (r.id, spec.id if spec else None) if x]
    threads = [t for t in s.threads if spec and t.on == spec.id]
    mentions = [
        {**dump(m), "ch": t.title}
        for t in s.threads
        if t.kind != "spec" and t.on != (spec.id if spec else None)
        for m in t.messages
        if any(i in m.text for i in ids)
    ]
    decisions = [x for x in s.decisions if spec and x.spec == spec.id]
    docs = [
        d for d in s.docs if d.about == r.id or any(any(i in b.text for i in ids) for b in d.blocks)
    ]
    # 이 요청의 context_id + 이 프로젝트 PR이 연결된 장애 사건의 context_id
    activity = [a for a in s.activity if a.context_id == r.context_id or a.context_id in inc_ctx]
    replies = [
        a for a in activity if a.from_ == "planning" and a.to not in ("planning", "safety", "shop")
    ]
    return {
        "spec": spec,
        "tasks": tasks,
        "prs": prs,
        "incidents": incidents,
        "dups": dups,
        "follow": follow,
        "threads": threads,
        "mentions": mentions,
        "decisions": decisions,
        "docs": docs,
        "activity": activity,
        "replies": replies,
    }


def stage_done(r: Request, g: dict) -> dict[str, bool]:
    """단계별로 어디까지 왔나 (코드 규칙)."""
    return {
        "요청": True,
        "작업": len(g["tasks"]) > 0,
        "운영": any(p.merged for p in g["prs"]),
        "처리": r.state == "완료",
        "소통": any(len(t.messages) > 0 for t in g["threads"])
        or len(g["mentions"]) > 0
        or len(g["decisions"]) > 0,
        "문서": len(g["docs"]) > 0,
    }


def project_summary(s: State, r: Request) -> dict:
    g = gather(s, r)
    done = stage_done(r, g)
    return {
        "id": r.id,
        "context_id": r.context_id,
        "title": r.title,
        "from": r.from_,
        "state": r.state,
        "auto": bool(r.auto and r.auto.ok),
        "spec_id": g["spec"].id if g["spec"] else None,
        "stages": [{"name": st, "done": done[st]} for st in STAGES],
    }


def project_detail(s: State, r: Request) -> dict:
    g = gather(s, r)
    done = stage_done(r, g)
    out: dict = {"request": dump(r), "stages": [{"name": st, "done": done[st]} for st in STAGES]}
    for key, value in g.items():
        if key == "spec":
            out[key] = dump(value) if value else None
        elif key == "mentions":
            out[key] = value
        else:
            out[key] = [dump(v) for v in value]
    return out
