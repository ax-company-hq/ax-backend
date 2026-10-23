"""상태 판정: 같은 입력 3회 결과 + 직전 결과 로 pass / pending / broken / regressed 를 정한다.

- 3번이 모두 같아야 그 결과로 본다. 다르면 flaky로 보고 fail(통과 아님)로 친다.
- pass: 통과.  pending: 구현이 아직 없음(또는 앞 단계 때문에 못 감, 공통 로그 기록기 없음).
- broken: 있는데 실패했고 통과한 적이 없다.  regressed: 통과한 적이 있는데 지금은 통과가 아니다.
- 직전 결과가 없으면(첫 실행) regressed가 생길 수 없다 = 기준선만 남긴다.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ax_contracts.data_access.dev_status import DevStatus, Stage, StageResult, Totals
from ax_e2e.scenario_a import paths
from ax_e2e.scenario_a.result import Outcome
from ax_e2e.scenario_a.stages import CUSTOMER, INCIDENT
from ax_e2e.scenario_a.stages import Stage as StageDef

RUNS_REQUIRED = 3


def merge_runs(outcomes: list[Outcome]) -> Outcome:
    """같은 단계의 N회 결과를 하나로. 하나라도 다르면 fail(flaky)."""
    states = [o.state for o in outcomes]
    if len(set(states)) == 1:
        return outcomes[0]
    return Outcome("fail", f"실행마다 결과가 다름(flaky): {', '.join(states)}")


def had_passed(prev: StageResult | None) -> bool:
    return prev is not None and (
        prev.state in ("pass", "regressed") or prev.last_pass_commit is not None
    )


def to_result(
    raw: Outcome, prev: StageResult | None, commit: str | None, now: datetime
) -> StageResult:
    passed_before = had_passed(prev)
    if raw.state == "pass":
        state, note = "pass", raw.note
    elif raw.state == "fail":
        state, note = ("regressed" if passed_before else "broken"), raw.note
    elif raw.state == "blocked":
        note = f"blocked_by:{','.join(raw.blocked_by)} {raw.note}".strip()
        state = "regressed" if passed_before else "pending"
    else:  # absent | pending
        state, note = ("regressed" if passed_before else "pending"), raw.note
    same = prev is not None and prev.state == state
    return StageResult(
        state=state,
        since=prev.since if same and prev.since else now,
        last_pass_commit=commit if state == "pass" else (prev.last_pass_commit if prev else None),
        issue=prev.issue if same and state != "pass" else None,
        note=note[:300] if note else None,
    )


def judge(
    stages: list[StageDef],
    runs: list[dict],
    prev: DevStatus | None,
    commit: str | None,
    now: datetime | None = None,
) -> tuple[DevStatus, dict]:
    """(DevStatus 부분 결과, 상세). runs는 once.run_once 결과 N개."""
    now = now or datetime.now(UTC)
    prev_by_name = {s.name: s for s in (prev.stages if prev else [])}
    out_stages: list[Stage] = []
    detail_stages = []
    for s in stages:
        key = f"{s.path}:{s.key}"
        pv = prev_by_name.get(s.label)
        views = {}
        for view in ("unit", "chain"):
            raw = merge_runs([Outcome.from_json(r[view][key]) for r in runs])
            before = getattr(pv, view) if pv else None
            views[view] = (raw, to_result(raw, before, commit, now))
        out_stages.append(
            Stage(
                no=s.no,
                name=s.label,
                dept=s.dept,
                req_ids=s.req_ids,
                unit=views["unit"][1],
                chain=views["chain"][1],
            )
        )
        detail_stages.append(
            {
                "no": s.no,
                "path": s.path,
                "key": s.key,
                "name": s.label,
                "dept": s.dept,
                "folder": paths.cards().get(s.dept, {}).get("_folder"),
                "skill": s.skill,
                "req_ids": s.req_ids,
                "condition": s.condition,
                "unit": {"state": views["unit"][1].state, "raw": views["unit"][0].to_json()},
                "chain": {"state": views["chain"][1].state, "raw": views["chain"][0].to_json()},
            }
        )
    totals = Totals(
        unit_pass=sum(1 for s in out_stages if s.unit.state == "pass"),
        chain_pass=sum(1 for s in out_stages if s.chain.state == "pass"),
        real_pass=0,
        total=len(out_stages),
        real_runs_consecutive=0,
    )
    status = DevStatus(generated_at=now, stages=out_stages, totals=totals)
    detail = {
        "stages": detail_stages,
        "baseline": prev is None,
        "commit": commit,
        "paths": path_scores(stages, out_stages),
    }
    return status, detail


def path_scores(stages: list[StageDef], out: list[Stage]) -> dict:
    """연결(사건) n/12 와 고객 경로 n/5 를 따로 센다 (완성은 둘 다 사건 ctx 하나로 17/17)."""
    scores: dict = {}
    for path in (INCIDENT, CUSTOMER):
        idx = [i for i, s in enumerate(stages) if s.path == path]
        scores[path] = {
            "total": len(idx),
            "unit_pass": sum(1 for i in idx if out[i].unit.state == "pass"),
            "chain_pass": sum(1 for i in idx if out[i].chain.state == "pass"),
        }
    return scores


def regressions(status: DevStatus) -> list[dict]:
    """이슈를 만들 대상: 통과하다 실패한 단계. 앞 단계 때문에 막힌 것은 원인이 아니다."""
    found = []
    for s in status.stages:
        for view in ("unit", "chain"):
            r = getattr(s, view)
            if r.state == "regressed" and not (r.note or "").startswith("blocked_by:"):
                found.append(
                    {
                        "no": s.no,
                        "name": s.name,
                        "dept": s.dept,
                        "view": view,
                        "note": r.note,
                        "since": r.since.isoformat() if r.since else None,
                        "last_pass_commit": r.last_pass_commit,
                        "req_ids": s.req_ids,
                    }
                )
    return found
