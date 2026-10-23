#!/usr/bin/env python3
"""관문 (docs/automation-spec.md 5장): folder-guard · test-guard · dev-health 판정과 게시.

guards.yml에서 두 단계로 쓴다.
  plan  (GITHUB_TOKEN, 읽기만): PR 정보·변경을 읽고 판정해 plan.json을 쓴다.
        PR 코드는 실행하지 않는다.
  apply (ax-bot 토큰): plan.json대로 커밋 상태·코멘트·hotfix 라벨 제거를 한다.

  guards.py plan --pr <번호> --action <이벤트 action> --dev-red <on|off> --out plan.json
  guards.py apply plan.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import team as team_mod  # noqa: E402

CONTEXTS = ("folder-guard", "test-guard", "dev-health", "agent-review")
MARKER = "<!-- guards:{sha} -->"
NOT_REQUIRED = "not required — 부서 폴더 안 변경 (리뷰 생략)"
HINT_LINES = 8  # 재대기 힌트에 넣을 위반 줄 수


@dataclass
class PullRequest:
    number: int
    head_sha: str
    branch: str
    base: str
    author: str
    state: str = "open"
    is_fork: bool = False
    labels: list[str] = field(default_factory=list)
    issue: int | None = None  # 봇 PR 본문의 `Closes #N`


@dataclass
class Plan:
    sha: str
    number: int
    skip_reason: str | None = None
    statuses: list[dict] = field(default_factory=list)
    comment: str | None = None
    remove_hotfix: bool = False
    requeue: dict | None = None  # 봇 PR이 관문에 걸리면 {issue, reason}: PR 닫고 재대기

    def status(self, context: str, state: str, description: str) -> None:
        self.statuses.append(
            {"context": context, "state": state, "description": description[:140]}
        )


def decide(
    team: team_mod.Team,
    pr: PullRequest,
    action: str,
    changes: list[team_mod.Change],
    test_violations: list[str],
    dev_red: bool,
    has_hotfix_status: bool,
) -> Plan:
    plan = Plan(sha=pr.head_sha, number=pr.number)
    plan.remove_hotfix = action == "synchronize" and "hotfix" in pr.labels
    if pr.base != "dev" or pr.state != "open":
        plan.skip_reason = f"대상 아님 (base={pr.base}, state={pr.state})"
        return plan
    if has_hotfix_status:
        plan.skip_reason = "현재 head에 유효한 hotfix 상태가 있음"
        return plan

    if pr.is_fork:
        plan.status("folder-guard", "failure", "external: 포크 PR은 받지 않음")
        return plan
    if not team.is_trusted(pr.author):
        plan.status("folder-guard", "failure", "external: 팀원이 아님")
        return plan

    is_bot = pr.author == team.bot_login
    rule = team.bot_branch_rule(pr.branch) if is_bot else None
    problems: list[str] = []

    # folder-guard
    if is_bot:
        dept = team.dept_of_branch(pr.branch)
        if not rule and not dept:
            problems = [f"브랜치 이름에서 부서를 알 수 없음: {pr.branch}"]
        else:
            problems = team.check_diff(changes, [dept] if dept else [], "bot", pr.branch)
    else:
        problems = team.check_diff(changes, team.depts_of(pr.author), "human")
    if problems:
        plan.status("folder-guard", "failure", f"규칙 위반 {len(problems)}건 — PR 코멘트 참고")
    else:
        plan.status("folder-guard", "success", "허용된 폴더만 변경")

    # test-guard
    if rule:
        plan.status("test-guard", "success", f"{rule[0]} 봇 브랜치")
        test_violations = []
    elif test_violations:
        n = len(test_violations)
        plan.status("test-guard", "failure", f"테스트 약화 {n}건 — PR 코멘트 참고")
    else:
        plan.status("test-guard", "success", "테스트 삭제·감소 없음")

    # dev-health
    if is_bot and pr.branch.startswith("revert/"):
        plan.status("dev-health", "success", "revert 브랜치")
    elif dev_red:
        plan.status("dev-health", "pending", "dev 빨간불 — 복구 후 자동 재개")
    else:
        plan.status("dev-health", "success", "dev 초록")

    # 리뷰 생략 대상: 봇 브랜치(review: skip), human_paths만 바꾼 사람 PR,
    # 부서 하나 안에서만 바뀐 PR (공유 부서·부서 2개 이상이 아니면 CI·관문만으로 머지)
    c = team.classify([ch.path for ch in changes])
    if rule and rule[1].get("review") == "skip":
        plan.status("agent-review", "success", f"리뷰 생략 ({rule[0]})")
    elif not is_bot and not c.depts and not c.shared:
        note = "리뷰 생략 (human_paths만 변경, code owner 리뷰)"
        plan.status("agent-review", "success", note)
    elif c.depts and not team.review_required(c):
        plan.status("agent-review", "success", NOT_REQUIRED)

    if is_bot and not rule and (problems or test_violations) and pr.issue:
        reason = (
            "다른 폴더 변경은 to:<부서> 요청으로 분리" if problems else "테스트 삭제·약화"
        )
        hint = [*problems, *test_violations][:HINT_LINES]
        detail = "\n".join(f"- {h}" for h in hint)
        plan.requeue = {"issue": pr.issue, "reason": f"관문 실패 — {reason}\n\n힌트:\n{detail}"}

    lines = []
    if problems:
        lines += ["**folder-guard**", *[f"- {p}" for p in problems], ""]
    if test_violations:
        lines += ["**test-guard**", *[f"- {v}" for v in test_violations], ""]
    if lines:
        plan.comment = "\n".join(
            [MARKER.format(sha=pr.head_sha), f"관문 실패 ({pr.head_sha[:7]})", "", *lines]
        ).rstrip()
    return plan


# ── GitHub 입출력 ─────────────────────────────────────────
def gh_json(path: str, *extra: str):
    out = subprocess.run(
        ["gh", "api", path, *extra], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout
    return json.loads(out) if out.strip() else None


def load_pr(repo: str, number: int) -> PullRequest:
    # PR 이벤트면 페이로드를 쓴다. workflow_dispatch(sweeper 재요청 등)만 API로 읽는다.
    # head가 이벤트 뒤에 바뀌었으면 cmd_plan의 fetch 비교에서 걸러진다 (새 이벤트가 다시 돈다)
    d = team_mod.event_pull_request(number) or gh_json(f"repos/{repo}/pulls/{number}")
    return PullRequest(
        number=number,
        head_sha=d["head"]["sha"],
        branch=d["head"]["ref"],
        base=d["base"]["ref"],
        author=d["user"]["login"],
        state=d["state"],
        is_fork=(d["head"]["repo"] or {}).get("full_name") != repo,
        labels=[label["name"] for label in d["labels"]],
        issue=closes_issue(d.get("body") or ""),
    )


def closes_issue(body: str) -> int | None:
    """봇 PR의 연결 이슈. 봇만 쓰는 표시 <!-- ax-issue:N -->만 믿는다 (Claude가 쓴 본문 무시)."""
    m = re.search(r"<!-- ax-issue:(\d+) -->", body)
    return int(m.group(1)) if m else None


def hotfix_status_present(team: team_mod.Team, repo: str, sha: str) -> bool:
    statuses = gh_json(f"repos/{repo}/commits/{sha}/statuses", "--paginate") or []
    return any(
        s["context"] == "folder-guard"
        and (s.get("creator") or {}).get("login") == team.bot_login
        and s["state"] == "success"
        and s.get("description", "").startswith("hotfix")
        for s in statuses
    )


def cmd_plan(args) -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    team = team_mod.Team.load()
    pr = load_pr(repo, args.pr)
    changes: list[team_mod.Change] = []
    test_violations: list[str] = []
    if pr.base == "dev" and pr.state == "open" and not pr.is_fork:
        # PR head를 받기만 한다 (checkout·실행 없음). 그 사이 head가 바뀌면 다음 실행이 처리한다
        refspec = f"+refs/pull/{pr.number}/head:refs/ax/pr"
        team_mod.git("fetch", "--no-tags", "--quiet", "origin", refspec)
        fetched = team_mod.git("rev-parse", "refs/ax/pr").strip()
        if fetched != pr.head_sha:
            print(f"head가 바뀜 ({pr.head_sha[:7]} → {fetched[:7]}), 이번 실행은 건너뜀")
            Path(args.out).write_text(json.dumps(asdict(Plan(pr.head_sha, pr.number, "head 변경"))))
            return 0
        mb = team_mod.merge_base("HEAD", pr.head_sha)
        changes = team_mod.diff_changes(mb, pr.head_sha)
        test_violations = team_mod.find_test_weakening(
            changes, lambda p: team_mod.show(mb, p), lambda p: team_mod.show(pr.head_sha, p),
            team,
        )
    plan = decide(
        team, pr, args.action, changes, test_violations,
        dev_red=args.dev_red == "on",
        has_hotfix_status=hotfix_status_present(team, repo, pr.head_sha),
    )
    text = json.dumps(asdict(plan), ensure_ascii=False, indent=2)
    Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


def cmd_apply(args) -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    run_url = (
        f"{os.environ['GITHUB_SERVER_URL']}/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    )
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    n, sha = plan["number"], plan["sha"]
    team = team_mod.Team.load()
    if plan["remove_hotfix"]:
        subprocess.run(["gh", "api", "-X", "DELETE", f"repos/{repo}/issues/{n}/labels/hotfix"])
    if plan["skip_reason"]:
        print(f"건너뜀: {plan['skip_reason']}")
        return 0
    # plan 이후 hotfix가 붙었으면 덮어쓰지 않는다 (hotfix.yml과 경쟁)
    if hotfix_status_present(team, repo, sha):
        print("건너뜀: 그 사이 hotfix 상태가 생김")
        return 0
    for s in plan["statuses"]:
        assert s["context"] in CONTEXTS, s
        subprocess.run(
            ["gh", "api", "-X", "POST", f"repos/{repo}/statuses/{sha}",
             "-f", f"state={s['state']}", "-f", f"context={s['context']}",
             "-f", f"description={s['description']}", "-f", f"target_url={run_url}"],
            check=True, capture_output=True,
        )
        print(f"{s['context']}: {s['state']} — {s['description']}")
    if plan["comment"]:
        bodies = subprocess.run(
            ["gh", "api", "--paginate", f"repos/{repo}/issues/{n}/comments", "--jq",
             f'.[] | select(.user.login == "{team.bot_login}") | .body'],
            check=True, capture_output=True, text=True, encoding="utf-8",
        ).stdout
        if MARKER.format(sha=sha) not in bodies:
            # 봇 PR은 잠겨 있어 봇도 바로는 못 쓴다 → 잠금 해제·코멘트·재잠금 (team.post_comment).
            # 코멘트가 실패해도 아래 requeue(PR 닫기·브랜치 삭제)는 해야 한다
            try:
                team_mod.post_comment(repo, n, plan["comment"])
            except subprocess.CalledProcessError:
                print("::warning::관문 실패 코멘트를 남기지 못함 (상태는 기록됨)")
    if plan.get("requeue"):
        rq = plan["requeue"]
        result = team_mod.requeue(repo, team, rq["issue"], rq["reason"], pr=n, head_sha=sha)
        print(f"requeue #{rq['issue']}: {result}")
        # 첫 실패(retry-1)면 힌트를 읽고 다시 작성하게 바로 실행한다.
        # 두 번째면 needs-human에서 멈춘다
        if result == "retry-1":
            print(team_mod.dispatch_write(repo, team, rq["issue"]))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="guards.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--action", default="")
    p.add_argument("--dev-red", default="off")
    p.add_argument("--out", required=True)
    sub.add_parser("apply").add_argument("plan")
    args = ap.parse_args(argv)
    return cmd_plan(args) if args.cmd == "plan" else cmd_apply(args)


if __name__ == "__main__":
    sys.exit(main())
