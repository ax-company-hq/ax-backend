#!/usr/bin/env python3
"""시나리오 A e2e 결과 후속 처리 (회귀 이슈 · 자동 revert).

스위치가 꺼져 있으면 아무것도 하지 않는다.

명령 (GITHUB_REPOSITORY, GH_TOKEN 필요):
  prev --run-id R --out DIR   직전 성공 실행의 e2e-result 아티팩트를 DIR에 받는다 (없으면 빈 DIR)
  act  --result F --detail F --sha S [--prev-sha P]
                              회귀가 있으면 이슈를 만들고(AX_E2E=on), AX_GO=on이면 작성을 시작하고,
                              AX_AUTO_REVERT=on이면 원인 PR을 처리한다

스위치(repo 변수): AX_E2E, AX_GO, AX_AUTO_REVERT, (정지) AX_AUTO_PAUSE.
LLM을 부르지 않는다. 작성 시작은 team.dispatch_write를 거쳐 run 제목이 cap 집계에 잡힌다.
봇 코멘트·이슈·PR 본문에는 `<!-- autopilot -->` 표시를 붙인다 (이 표시가 있는 이벤트는 무시한다).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import team as team_mod  # noqa: E402

MARK = "<!-- autopilot -->"
WORKFLOW = "scenario-a.yml"
ARTIFACT = "e2e-result"
NEEDS_HUMAN = "needs-human"
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
UPLOAD = "pu" + "sh"  # revert/<번호> 브랜치를 올리는 git 하위 명령 (봇 토큰으로만)


def repo() -> str:
    return os.environ["GITHUB_REPOSITORY"]


def api(path: str, *extra: str):
    return team_mod.gh_api(path, *extra)


def switch(name: str) -> bool:
    return os.environ.get(name, "") == "on"


def clean(text: str, limit: int = 300) -> str:
    return HTML_COMMENT.sub("", text or "").replace("<!--", "")[:limit]


def say(issue: int, text: str) -> None:
    team_mod.post_comment(repo(), issue, f"{MARK}\n{text}")


def add_label(issue: int, label: str) -> None:
    api(f"repos/{repo()}/issues/{issue}/labels", "-X", "POST", "-f", f"labels[]={label}")


def find_prev(run_id: str, out: Path) -> str | None:
    """직전 성공 실행의 아티팩트를 받고 그 실행의 커밋 sha를 돌려준다. 없으면 None (첫 실행)."""
    out.mkdir(parents=True, exist_ok=True)
    url = f"repos/{repo()}/actions/workflows/{WORKFLOW}/runs?branch=dev&status=success&per_page=30"
    for run in (api(url) or {}).get("workflow_runs", []):
        if str(run["id"]) == str(run_id):
            continue
        listing = api(f"repos/{repo()}/actions/runs/{run['id']}/artifacts") or {}
        names = [a["name"] for a in listing.get("artifacts", []) if not a.get("expired")]
        if ARTIFACT in names:
            cmd = ["gh", "run", "download", str(run["id"]), "-R", repo(), "-n", ARTIFACT]
            subprocess.run([*cmd, "-D", str(out)], check=True, capture_output=True)
            return run["head_sha"]
    return None


def group_regressions(detail: dict, team) -> list[dict]:
    """같은 단계의 단독·연결 회귀를 한 건으로 묶는다. 담당 부서는 team.yml 부서 id로 바꾼다."""
    folders = {s["name"]: s.get("folder") for s in detail.get("stages", [])}
    grouped: dict[str, dict] = {}
    for reg in detail.get("regressions", []):
        entry = grouped.setdefault(reg["name"], {**reg, "views": [], "notes": []})
        entry["views"].append(reg["view"])
        entry["notes"].append(f"{reg['view']}: {reg.get('note') or '사유 없음'}")
        dept = team.resolve_dept(folders.get(reg["name"]) or "") or team.resolve_dept(reg["dept"])
        entry["team_dept"] = dept
    return list(grouped.values())


def marker_of(name: str) -> str:
    return f"<!-- e2e-regression:{name} -->"


def issue_body(reg: dict, sha: str, prev_sha: str | None) -> str:
    req = ", ".join(reg.get("req_ids") or []) or "-"
    views = " · ".join(reg["views"])
    lines = [
        marker_of(reg["name"]),
        MARK,
        "",
        f"시나리오 A e2e에서 **{reg['name']}** 단계가 통과하다가 실패했습니다 ({views}).",
        "",
        f"- 담당: {reg['dept']} · 요구사항: {req}",
        f"- 마지막 통과 커밋: `{reg.get('last_pass_commit') or '-'}` → 지금 `{sha[:10]}`",
        f"- 실패 시각(처음): {reg.get('since') or '-'}",
        "",
        "사유:",
        *[f"- {clean(n)}" for n in reg["notes"]],
        "",
        "재현: `AX_E2E_MODE=mock uv run ax-e2e run --out out` (결과의 `e2e-detail.json` 참고)",
    ]
    if prev_sha:
        lines += ["", f"의심 범위: `{prev_sha[:10]}..{sha[:10]}`"]
    return "\n".join(lines)


def find_open_issue(name: str) -> dict | None:
    items = api(f"repos/{repo()}/issues?state=open&labels=from:e2e&per_page=100") or []
    mark = marker_of(name)
    for item in items:
        if "pull_request" not in item and mark in (item.get("body") or ""):
            return item
    return None


def file_regression(reg: dict, sha: str, prev_sha: str | None, go: bool, team) -> tuple[int, str]:
    """(이슈 번호, 처리 요약). 같은 단계의 열린 이슈가 있으면 댓글만 단다."""
    existing = find_open_issue(reg["name"])
    if existing:
        notes = "; ".join(clean(n, 160) for n in reg["notes"])
        say(existing["number"], f"같은 단계가 `{sha[:10]}`에서도 계속 실패합니다: {notes}")
        return existing["number"], "댓글"
    dept = reg.get("team_dept")
    labels = ["bug", "from:e2e", *([f"dept:{dept}"] if dept else [])]
    created = api(
        f"repos/{repo()}/issues",
        "-X", "POST",
        "-f", f"title=[e2e 회귀] {reg['name']}",
        "-f", f"body={issue_body(reg, sha, prev_sha)}",
        *[x for lb in labels for x in ("-f", f"labels[]={lb}")],
    )
    number = created["number"]
    if go and dept:
        team_mod.dispatch_write(repo(), team, number, dept, "task")
        return number, "이슈 + 작성 시작"
    return number, "이슈"


def suspect_prs(prev_sha: str | None, sha: str) -> list[int]:
    """prev_sha..sha 범위 커밋이 속한 PR 번호 (중복 제거, 오래된 순)."""
    if not prev_sha:
        return []
    out = subprocess.run(
        ["git", "rev-list", "--reverse", f"{prev_sha}..{sha}"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    found: list[int] = []
    for commit in out:
        for pr in api(f"repos/{repo()}/commits/{commit}/pulls") or []:
            if pr["number"] not in found:
                found.append(pr["number"])
    return found


def revert_prs(number: int) -> list[dict]:
    owner = repo().split("/")[0]
    return api(f"repos/{repo()}/pulls?state=all&head={owner}:revert/{number}") or []


def decide_revert(team, pr: dict, files: list[str], reverts: list[dict]) -> str:
    """원인 PR을 어떻게 처리할지: revert | human | core | no_rerevert | wait | pause."""
    if pr["head"]["ref"].startswith("revert/"):
        return "no_rerevert"  # 되돌림 PR이 원인이면 다시 되돌리지 않는다
    if any(r.get("merged_at") for r in reverts):
        return "pause"  # 이미 되돌렸는데도 빨간불
    if any(r["state"] == "open" for r in reverts):
        return "wait"
    if any(team.is_core_path(f) for f in files):
        return "core"
    if pr["user"]["login"] == team.bot_login:
        return "revert"
    return "human"


def git_revert_pr(pr: dict, issue: int) -> str:
    """revert/<번호> 브랜치를 만들어 올리고 PR을 연다. 충돌하면 conflict."""
    number, merge_commit = pr["number"], pr["merge_commit_sha"]
    token = os.environ["GH_TOKEN"]
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()

    def git(*args: str) -> None:
        ident = ["-c", "user.name=ax-company-hq-bot[bot]",
                 "-c", "user.email=ax-bot@users.noreply.github.com"]
        subprocess.run(["git", *ident, *args], check=True, capture_output=True, text=True)

    git("fetch", "origin", "dev")
    git("checkout", "-B", f"revert/{number}", "origin/dev")
    parents = len((api(f"repos/{repo()}/commits/{merge_commit}") or {}).get("parents", []))
    try:
        git("revert", "--no-edit", *(["-m", "1"] if parents > 1 else []), merge_commit)
    except subprocess.CalledProcessError:
        subprocess.run(["git", "revert", "--abort"], capture_output=True)
        return "conflict"
    title = clean(pr["title"], 80)
    git("commit", "--amend", "-m", f"revert: #{number} {title}\n\ne2e 회귀 #{issue}")
    header = f"http.extraheader=AUTHORIZATION: basic {auth}"
    git("-c", header, UPLOAD, "origin", f"revert/{number}")
    body = (
        f"{MARK}\ne2e 회귀 #{issue}의 원인으로 보여 #{number}을 되돌립니다 (AX_AUTO_REVERT). "
        "되돌린 뒤에도 빨간불이면 새 작업을 멈춥니다."
    )
    api(
        f"repos/{repo()}/pulls", "-X", "POST",
        "-f", f"head=revert/{number}", "-f", "base=dev",
        "-f", f"title=revert: #{number} {title}", "-f", f"body={body}",
    )
    return "reverted"


def set_pause() -> bool:
    """AX_AUTO_PAUSE=on (새 작업 정지). 권한이 없으면 False."""
    for method, path in (("PATCH", "/AX_AUTO_PAUSE"), ("POST", "")):
        try:
            api(f"repos/{repo()}/actions/variables{path}", "-X", method,
                "-f", "name=AX_AUTO_PAUSE", "-f", "value=on")
            return True
        except subprocess.CalledProcessError:
            continue
    return False


HUMAN_WHY = {
    "human": "사람이 올린 PR이라 자동으로 되돌리지 않습니다",
    "core": "핵심 파일(.github/·.claude/·기준표)을 바꾼 PR이라 자동으로 되돌리지 않습니다",
    "no_rerevert": "되돌림 PR이 원인이라 다시 되돌리지 않습니다",
}


def handle_cause(team, issue: int, pr_number: int) -> str:
    pr = api(f"repos/{repo()}/pulls/{pr_number}")
    listing = api(f"repos/{repo()}/pulls/{pr_number}/files?per_page=100") or []
    action = decide_revert(team, pr, [f["filename"] for f in listing], revert_prs(pr_number))
    author = pr["user"]["login"]
    if action == "revert":
        if git_revert_pr(pr, issue) == "conflict":
            action = "human"
        else:
            say(issue, f"원인 #{pr_number}을 되돌리는 PR을 열었습니다.")
            return "revert"
    if action in HUMAN_WHY:
        mention = f"@{author} " if author != team.bot_login else ""
        say(issue, f"원인 후보 #{pr_number}: {HUMAN_WHY[action]}. {mention}확인해 주세요.")
        add_label(issue, NEEDS_HUMAN)
    elif action == "pause":
        ok = set_pause()
        note = "새 작업을 멈췄습니다 (AX_AUTO_PAUSE=on)."
        if not ok:
            note = "AX_AUTO_PAUSE를 켜지 못했습니다 (변수 쓰기 권한 확인)."
        say(issue, f"#{pr_number}을 되돌린 뒤에도 회귀가 남아 있습니다. {note}")
        add_label(issue, NEEDS_HUMAN)
    return action


def act(result_path: Path, detail_path: Path, sha: str, prev_sha: str | None) -> list[str]:
    """회귀 후속 처리. 스위치 꺼짐·첫 실행(기준선)·회귀 없음이면 아무것도 하지 않는다."""
    if not switch("AX_E2E"):
        return ["AX_E2E가 on이 아님 - 보고만 함"]
    detail = json.loads(detail_path.read_text(encoding="utf-8"))
    if detail.get("baseline"):
        return ["첫 실행 - 기준선만 기록"]
    team = team_mod.Team.load()
    regs = group_regressions(detail, team)
    if not regs:
        return ["회귀 없음"]
    log, issues = [], []
    for reg in regs:
        number, how = file_regression(reg, sha, prev_sha, switch("AX_GO"), team)
        issues.append(number)
        log.append(f"{reg['name']}: #{number} {how}")
    if switch("AX_AUTO_REVERT"):
        prs = suspect_prs(prev_sha, sha)
        if len(prs) == 1:
            log.append(f"원인 PR #{prs[0]}: {handle_cause(team, issues[0], prs[0])}")
        else:
            names = ", ".join(f"#{n}" for n in prs) or "없음"
            say(issues[0], f"원인 PR을 하나로 정하지 못해 revert하지 않습니다 (후보: {names}).")
            log.append("원인 PR 불명 - revert 안 함")
    return log


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="e2e_follow.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prev")
    p.add_argument("--run-id", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("act")
    p.add_argument("--result", required=True)
    p.add_argument("--detail", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--prev-sha")
    args = ap.parse_args(argv)
    if args.cmd == "prev":
        print(find_prev(args.run_id, Path(args.out)) or "")
        return 0
    for line in act(Path(args.result), Path(args.detail), args.sha, args.prev_sha):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
