#!/usr/bin/env python3
"""Claude 워크플로(_claude-write · claude-review)의 규칙 단계 (docs/automation-spec.md 1~3·6장).

Claude가 도는 잡은 결과물(패치·JSON)만 만든다. GitHub에 쓰는 일은 prep·apply 잡이 dev 정의로 한다.
이 스크립트는 레포의 Python 패키지를 import하지 않는다 (team.py만).

  write-prep    --dept D --mode task|request --issue N --actor A --out-dir DIR   (bot)
  write-post    --dept D --workspace W --out-dir DIR      (claude 잡, Claude 실행 뒤)
  write-apply   --dept D --mode M --issue N --patch-dir DIR --report FILE       (bot)
  review-prep   --pr N --out-dir DIR                                            (bot)
  review-apply  --pr N --verdict-dir DIR --plan FILE                            (bot)
  check-result FILE · check-verdict FILE --head-sha S
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import team as team_mod  # noqa: E402

MODES_WRITE = ("task", "request")  # address·ci-fix·arbiter-fix는 2단계
BLOCK_LABELS = {"external", "blocked", "obsolete", "in-progress", "quota"}
MAX_TITLE = 120
MAX_BODY = 20_000
MAX_PATCH_BYTES = 2_000_000
MAX_HOP = 3


class JobError(Exception):
    """검증 실패. 메시지는 이슈·PR 코멘트에 그대로 쓸 수 있게."""


# ── 공통 ─────────────────────────────────────────────────
def repo() -> str:
    return os.environ["GITHUB_REPOSITORY"]


def gh(*args: str, check: bool = True) -> str:
    return subprocess.run(
        ["gh", *args], check=check, capture_output=True, text=True, encoding="utf-8"
    ).stdout


def api(path: str, *extra: str):
    return team_mod.gh_api(path, *extra)


def set_output(**kw: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    lines = "".join(f"{k}={v}\n" for k, v in kw.items())
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(lines)
    print(lines, end="")


def slugify(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:limit].rstrip("-") or "task")


def labels_of(issue: dict) -> list[str]:
    return [lb["name"] for lb in issue.get("labels", [])]


def retry_count(labels: list[str]) -> int:
    for n in (3, 2, 1):
        if f"retry-{n}" in labels:
            return n
    return 0


def hop_of(body: str) -> int:
    m = re.search(r"(?m)^hop:\s*(\d+)\s*$", body or "")
    return int(m.group(1)) if m else 0


# ── 결과 형식 검사 ────────────────────────────────────────
def check_result(data: object, team: team_mod.Team) -> dict:
    """result.json {"title","body","summary","requests":[{"to","text"}]}"""
    if not isinstance(data, dict):
        raise JobError("result.json이 객체가 아님")
    out = {}
    for key, limit in (("title", MAX_TITLE), ("body", MAX_BODY), ("summary", MAX_BODY)):
        v = data.get(key, "")
        if not isinstance(v, str) or len(v) > limit:
            raise JobError(f"result.json {key}가 문자열이 아니거나 너무 김")
        out[key] = v
    if not out["title"].strip():
        raise JobError("result.json title이 비어 있음")
    reqs = data.get("requests", [])
    if not isinstance(reqs, list) or len(reqs) > 5:
        raise JobError("result.json requests는 5개 이하 목록")
    out["requests"] = []
    for r in reqs:
        if not isinstance(r, dict) or not isinstance(r.get("to"), str) or not isinstance(
            r.get("text"), str
        ) or len(r["text"]) > 4000:
            raise JobError("requests 항목 형식 오류")
        out["requests"].append({"to": r["to"], "text": r["text"]})
    return out


def check_verdict(data: object, head_sha: str) -> dict:
    """verdict.json {"head_sha","verdict","blocking","notes","comment"}"""
    if not isinstance(data, dict):
        raise JobError("verdict.json이 객체가 아님")
    if data.get("head_sha") != head_sha:
        raise JobError("verdict.json head_sha가 현재 head와 다름")
    if data.get("verdict") not in ("approve", "changes"):
        raise JobError("verdict는 approve|changes")
    out = {"head_sha": head_sha, "verdict": data["verdict"]}
    for key in ("blocking", "notes"):
        v = data.get(key, [])
        if not isinstance(v, list) or not all(isinstance(x, str) and len(x) < 2000 for x in v):
            raise JobError(f"verdict.json {key} 형식 오류")
        out[key] = v[:20]
    comment = data.get("comment", "")
    if not isinstance(comment, str) or len(comment) > MAX_BODY:
        raise JobError("verdict.json comment 형식 오류")
    out["comment"] = comment
    if out["verdict"] == "changes" and not out["blocking"]:
        raise JobError("changes인데 blocking이 비어 있음")
    return out


# ── write: prep ──────────────────────────────────────────
def write_prep(team: team_mod.Team, dept: str, mode: str, issue_no: int, actor: str,
               out_dir: Path) -> dict:
    if mode not in MODES_WRITE:
        raise JobError(f"1단계 작성 모드는 task|request: {mode}")
    dept_id = team.resolve_dept(dept)
    if not dept_id or dept_id != dept:
        raise JobError(f"team.yml에 없는 부서 id: {dept}")
    if not team.is_trusted(actor):
        raise JobError(f"실행한 사람이 팀원·봇이 아님: {actor}")
    issue = api(f"repos/{repo()}/issues/{issue_no}")
    if "pull_request" in issue:
        raise JobError(f"#{issue_no}는 PR")
    if issue["state"] != "open":
        raise JobError(f"#{issue_no}가 닫혀 있음")
    labels = labels_of(issue)
    bad = BLOCK_LABELS & set(labels)
    if bad:
        raise JobError(f"#{issue_no}에 {', '.join(sorted(bad))} 라벨이 있음")
    want = f"dept:{dept}" if mode == "task" else f"to:{dept}"
    if want not in labels:
        raise JobError(f"#{issue_no}에 {want} 라벨이 없음")
    if not team.is_trusted(issue["user"]["login"]):
        raise JobError("이슈 작성자가 팀원·봇이 아님")
    if hop_of(issue.get("body") or "") > MAX_HOP:
        raise JobError("hop이 3을 넘음")
    linked = open_prs_for_issue(issue_no)
    if linked:
        raise JobError(f"#{issue_no}에 연결된 열린 PR이 있음: #{linked[0]}")
    token_secret = team.owner_token(dept)
    owner = team.owners(dept)[0]
    used = team_mod.count_claude_jobs(
        team_mod.todays_claude_jobs(team_mod.datetime.now(team_mod.UTC).strftime("%Y-%m-%d")),
        team_mod.token_depts_of(team, owner),
    )
    cap = int(team.members[owner].get("daily_cap", 0))
    if used >= cap:
        raise JobError(f"quota: {team.members[owner]['name']} 오늘 {used}/{cap}")

    out_dir.mkdir(parents=True, exist_ok=True)
    # 이슈·코멘트는 데이터다. 팀원·봇 것만 넘긴다
    comments = [
        c for c in api(f"repos/{repo()}/issues/{issue_no}/comments?per_page=100") or []
        if team.is_trusted((c.get("user") or {}).get("login", ""))
    ]
    (out_dir / "issue.md").write_text(
        f"# #{issue_no} {issue['title']}\n\n{issue.get('body') or ''}\n", encoding="utf-8"
    )
    (out_dir / "comments.md").write_text(
        "\n\n---\n\n".join(f"@{c['user']['login']}:\n{c.get('body') or ''}" for c in comments),
        encoding="utf-8",
    )
    meta = {
        "dept": dept, "mode": mode, "issue": issue_no, "title": issue["title"],
        "folder": team.depts[dept]["folder"], "retry": retry_count(labels),
        "hop": hop_of(issue.get("body") or ""),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return {"token_secret": token_secret, "owner": owner, **meta}


def open_prs_for_issue(issue_no: int) -> list[int]:
    """본문에 Closes #N을 가진 열린 PR (봇 PR은 항상 이렇게 연결한다)."""
    prs = api(f"repos/{repo()}/pulls?state=open&per_page=100") or []
    pat = re.compile(rf"(?im)^\s*(?:closes|fixes|resolves)\s+#{issue_no}\b")
    return [p["number"] for p in prs if pat.search(p.get("body") or "")]


# ── write: post (claude 잡 안, Claude 실행 뒤) ──────────────
def allowed_prefixes(team: team_mod.Team, dept: str) -> tuple[str, ...]:
    folder = team.depts[dept]["folder"].rstrip("/") + "/"
    return folder, f"{team.notes_prefix}{dept}/"


def changed_paths(workspace: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        check=True, capture_output=True,
    ).stdout.decode("utf-8", "surrogateescape")
    paths, parts, i = [], out.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if not entry:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            i += 1  # 원래 경로
        paths.append(path)
    return paths


def write_post(team: team_mod.Team, dept: str, workspace: Path, out_dir: Path) -> dict:
    """허용 경로 변경만 커밋 → format-patch. 그 밖의 변경은 버리고 목록만 남긴다."""
    result_path = out_dir / "result.json"
    try:
        result = check_result(json.loads(result_path.read_text(encoding="utf-8")), team)
    except (OSError, ValueError) as e:
        raise JobError(f"result.json을 읽을 수 없음: {e}") from e
    prefixes = allowed_prefixes(team, dept)
    paths = changed_paths(workspace)
    keep = [p for p in paths if p.startswith(prefixes) and not team.forbidden_name(p)]
    dropped = sorted(set(paths) - set(keep))
    git = ["git", "-C", str(workspace)]
    if keep:
        subprocess.run([*git, "add", "--", *keep], check=True)
        msg = f"{result['title']}\n\nRefs: {dept}"
        subprocess.run(
            [*git, "-c", "user.name=ax-agent", "-c", "user.email=ax-agent@users.noreply.github.com",
             "commit", "-q", "--no-verify", "-m", msg],
            check=True,
        )
    patch = subprocess.run(
        [*git, "format-patch", "--stdout", "--binary", "ax-baseline..HEAD"],
        check=True, capture_output=True,
    ).stdout
    if len(patch) > MAX_PATCH_BYTES:
        raise JobError(f"패치가 너무 큼 ({len(patch)} bytes)")
    (out_dir / "changes.patch").write_bytes(patch)
    (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    (out_dir / "dropped.txt").write_text("\n".join(dropped), encoding="utf-8")
    return {"kept": keep, "dropped": dropped, "empty": not keep}


# ── write: apply ─────────────────────────────────────────
def patch_paths(patch: bytes) -> list[str]:
    """format-patch의 diff --git 헤더에서 경로를 모은다 (a/·b/ 모두)."""
    paths = set()
    for m in re.finditer(rb"^diff --git a/(.+?) b/(.+?)$", patch, re.M):
        paths.update(x.decode("utf-8", "surrogateescape") for x in m.groups())
    return sorted(paths)


def request_marker(issue_no: int, to: str, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"<!-- req:{issue_no}:{to}:{digest} -->"


def plan_requests(team: team_mod.Team, issue_no: int, hop: int, requests: list[dict],
                  existing_bodies: list[str]) -> tuple[list[dict], list[str]]:
    """만들 to: 이슈 목록과 원래 이슈에 남길 메모."""
    create, notes = [], []
    for r in requests:
        to = team.resolve_dept(r["to"])
        if not to:
            notes.append(f"요청 대상 `{r['to']}`가 team.yml 부서가 아니라 이슈를 만들지 않음")
            continue
        marker = request_marker(issue_no, to, r["text"])
        if any(marker in b for b in existing_bodies):
            continue
        if hop + 1 > MAX_HOP:
            notes.append(f"hop {hop + 1} > {MAX_HOP}: to:{to} 요청을 만들지 않음")
            continue
        create.append({"to": to, "text": r["text"], "marker": marker, "hop": hop + 1})
    return create, notes


def branch_name(dept: str, issue_no: int, title: str, retry: int) -> str:
    return f"{dept}/{issue_no}-{slugify(title)}-r{retry}"


# ── review ───────────────────────────────────────────────
def review_author_person(team: team_mod.Team, author: str, branch: str) -> str | None:
    """봇 PR이면 브랜치 부서 실행 토큰의 주인, 사람 PR이면 작성자."""
    if author == team.bot_login:
        dept = team.dept_of_branch(branch)
        return team.owners(dept)[0] if dept else None
    return author


def review_model_var(dept: str) -> str:
    return "AX_MODEL_REVIEW_SHARED" if dept in ("contracts", "agent-template", "infra") else (
        "AX_MODEL_REVIEW")


def aggregate(states: dict[str, str]) -> str:
    """부서별 상태 → agent-review. error가 섞이면 pending(sweeper 담당)."""
    vals = set(states.values())
    if not vals:
        return "failure"
    if "failure" in vals:
        return "failure"
    if "error" in vals or "pending" in vals:
        return "pending"
    return "success"


def review_comment(dept: str, name: str, verdict: dict) -> str:
    head = "승인" if verdict["verdict"] == "approve" else "변경 요청"
    lines = [f"<!-- agent-review:{dept}:{verdict['head_sha']} -->",
             f"**[agent:{dept}] {head}** ({name}, {verdict['head_sha'][:7]})", ""]
    if verdict["blocking"]:
        lines += ["**blocking**", *[f"- {b}" for b in verdict["blocking"]], ""]
    if verdict["notes"]:
        lines += ["**notes**", *[f"- {n}" for n in verdict["notes"]], ""]
    if verdict["comment"]:
        lines += [verdict["comment"]]
    return "\n".join(lines).rstrip()


def review_round_label(labels: list[str]) -> str:
    """review-round-1 → 2 → 3 (3에서 멈춤)."""
    current = max((n for n in (1, 2, 3) if f"review-round-{n}" in labels), default=0)
    return f"review-round-{min(current + 1, 3)}"


# ── Claude 실행 설정 (settings 입력) ──────────────────────
DENY = ["Read(//proc/**)", "Read(**/.git/config)", "Read(**/.env)", "Read(**/.env.*)",
        "Bash", "WebFetch", "WebSearch"]


def claude_settings(dept: str, trusted: Path, out_dir: Path) -> dict:
    """deny 규칙 + 폴더 훅. 훅 명령에 부서를 직접 넣는다 (env에 의존하지 않음)."""
    hook = (f'node "{trusted}/.claude/hooks/guard-folder.mjs" --depts {dept} '
            f'--out "{out_dir}"')
    return {
        "permissions": {"deny": DENY},
        "hooks": {"PreToolUse": [{
            "matcher": "Edit|Write|MultiEdit|NotebookEdit",
            "hooks": [{"type": "command", "command": hook}],
        }]},
    }


# ── GitHub 쓰기 (ax-bot 토큰, apply 잡에서만) ───────────────
def add_labels(issue_no: int, labels: list[str]) -> None:
    api(f"repos/{repo()}/issues/{issue_no}/labels", "-X", "POST",
        *[x for lb in labels for x in ("-f", f"labels[]={lb}")])


def remove_label(issue_no: int, label: str) -> None:
    gh("api", "-X", "DELETE", f"repos/{repo()}/issues/{issue_no}/labels/{label}", check=False)


def comment(issue_no: int, body: str) -> None:
    api(f"repos/{repo()}/issues/{issue_no}/comments", "-X", "POST", "-f", f"body={body}")


def lock(issue_no: int) -> None:
    gh("api", "-X", "PUT", f"repos/{repo()}/issues/{issue_no}/lock", "-f", "lock_reason=resolved",
       check=False)


def set_status(sha: str, context: str, state: str, description: str) -> None:
    server, run_id = os.environ["GITHUB_SERVER_URL"], os.environ["GITHUB_RUN_ID"]
    run_url = f"{server}/{repo()}/actions/runs/{run_id}"
    api(f"repos/{repo()}/statuses/{sha}", "-X", "POST", "-f", f"state={state}",
        "-f", f"context={context}", "-f", f"description={description[:140]}",
        "-f", f"target_url={run_url}")


def bot_bodies(issue_no: int, team: team_mod.Team) -> list[str]:
    cs = api(f"repos/{repo()}/issues/{issue_no}/comments?per_page=100") or []
    return [c.get("body") or "" for c in cs if (c.get("user") or {}).get("login") == team.bot_login]


def existing_request_bodies(team: team_mod.Team) -> list[str]:
    issues = api(f"repos/{repo()}/issues?state=all&labels=request&per_page=100") or []
    return [i.get("body") or "" for i in issues if i["user"]["login"] == team.bot_login]


def write_apply(team: team_mod.Team, dept: str, mode: str, issue_no: int, retry: int,
                result_file: Path, patch_file: Path, report_file: Path | None) -> str:
    """패치 검사 → requests → push·PR. 위반이면 아무것도 push하지 않고 requeue."""
    result = check_result(json.loads(result_file.read_text(encoding="utf-8")), team)
    patch = patch_file.read_bytes() if patch_file.exists() else b""
    issue = api(f"repos/{repo()}/issues/{issue_no}")
    hop = hop_of(issue.get("body") or "")

    # requests는 패치가 비어 있어도 먼저 처리한다
    create, notes = plan_requests(team, issue_no, hop, result["requests"],
                                  existing_request_bodies(team))
    made = []
    for r in create:
        body = (f"hop: {r['hop']}\n\n요청 출처: #{issue_no} ({dept})\n\n"
                f"{r['text']}\n\n{r['marker']}")
        new = api(f"repos/{repo()}/issues", "-X", "POST", "-f",
                  f"title=[요청 {dept}→{r['to']}] {result['title'][:80]}", "-f", f"body={body}",
                  "-f", "labels[]=request", "-f", f"labels[]=to:{r['to']}")
        lock(new["number"])
        made.append(new["number"])
    for n in notes:
        comment(issue_no, n)

    if not patch.strip():
        remove_label(issue_no, "in-progress")
        if made:
            add_labels(issue_no, ["delegated"])
            links = ", ".join(f"#{m}" for m in made)
            comment(issue_no, f"다른 부서 작업이 필요해 요청을 넘겼습니다: {links}\n"
                    "위임한 이슈가 모두 닫히면 다시 후보가 됩니다.")
            return "delegated"
        add_labels(issue_no, ["no-op"])
        comment(issue_no, f"변경 없음 (no-op)\n\n{result['summary']}")
        return "no-op"

    branch = branch_name(dept, issue_no, issue["title"], retry)
    # dev 위에 패치를 적용해 check-diff (merge-base 기준)
    team_mod.git("switch", "-q", "-c", branch)
    subprocess.run(["git", "-c", "user.name=ax-agent",
                    "-c", "user.email=ax-agent@users.noreply.github.com",
                    "am", "-q", "--keep-cr", str(patch_file)], check=True)
    changes = team_mod.diff_changes(team_mod.merge_base("origin/dev", "HEAD"), "HEAD")
    violations = team.check_diff(changes, [dept], "bot", branch)
    if violations:
        team_mod.requeue(repo(), team, issue_no,
                         "패치 검사 실패 — " + "; ".join(violations[:5]),
                         run_id=os.environ.get("GITHUB_RUN_ID"))
        return "requeued"

    token = os.environ["GH_TOKEN"]
    team_mod.git("remote", "set-url", "origin",
                 f"https://x-access-token:{token}@github.com/{repo()}.git")
    team_mod.git("push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    body = f"{result['body']}\n\nCloses #{issue_no}\n\n<!-- by:{dept} mode:{mode} -->"
    pr = api(f"repos/{repo()}/pulls", "-X", "POST", "-f", f"title={result['title']}",
             "-f", f"head={branch}", "-f", "base=dev", "-f", f"body={body}")
    add_labels(pr["number"], [f"by:{dept}"])
    lock(pr["number"])
    lock(issue_no)
    if report_file and report_file.exists():
        comment(pr["number"], "<!-- test-report -->\n" + report_file.read_text(encoding="utf-8")[
            :MAX_BODY])
    return f"pr:{pr['number']}"


def has_hotfix(team: team_mod.Team, sha: str) -> bool:
    statuses = api(f"repos/{repo()}/commits/{sha}/statuses?per_page=100") or []
    return any(
        s["context"] == "folder-guard" and s["state"] == "success"
        and (s.get("creator") or {}).get("login") == team.bot_login
        and s.get("description", "").startswith("hotfix")
        for s in statuses
    )


def review_prep(team: team_mod.Team, pr_no: int) -> dict:
    """리뷰 대상이면 {head_sha, reviewers:[{dept, token_secret, model_var, name}]}."""
    pr = api(f"repos/{repo()}/pulls/{pr_no}")
    head = pr["head"]["sha"]
    if pr["state"] != "open" or pr["base"]["ref"] != "dev":
        return {"skip": "대상 아님"}
    if pr.get("draft"):
        return {"skip": "draft"}
    if (pr["head"]["repo"] or {}).get("full_name") != repo():
        return {"skip": "포크 PR"}
    labels = [lb["name"] for lb in pr["labels"]]
    if "external" in labels or not team.is_trusted(pr["user"]["login"]):
        return {"skip": "external"}
    statuses = api(f"repos/{repo()}/commits/{head}/statuses?per_page=100") or []
    for s in statuses:
        if (s.get("creator") or {}).get("login") == team.bot_login and s["context"] in (
            "agent-review", "folder-guard"
        ) and s["state"] == "success" and s.get("description", "").startswith(
            ("hotfix", "리뷰 생략")
        ):
            return {"skip": f"{s['context']}: {s['description']}"}
    team_mod.git("fetch", "--no-tags", "-q", "origin", f"+refs/pull/{pr_no}/head:refs/ax/pr")
    mb = team_mod.merge_base("HEAD", head)
    changes = team_mod.diff_changes(mb, head)
    classified = team.classify([c.path for c in changes])
    if not classified.depts and not classified.shared:
        return {"skip": "human_paths만 변경 (guards가 처리)"}
    person = review_author_person(team, pr["user"]["login"], pr["head"]["ref"])
    if not person:
        return {"skip": "브랜치에서 부서를 알 수 없음", "fail": True, "head_sha": head}
    depts = team.reviewers_for(list(classified.depts), person)
    if not depts:
        return {"skip": "리뷰 부서 없음", "fail": True, "head_sha": head}
    reviewers = []
    for d in depts:
        owner = team.owners(d)[0]
        reviewers.append({"dept": d, "token_secret": team.owner_token(d),
                          "model_var": review_model_var(d), "name": team.members[owner]["name"],
                          "owner": owner})
    return {"head_sha": head, "base_sha": mb, "reviewers": reviewers,
            "changed": list(classified.depts), "labels": labels}


def review_apply(team: team_mod.Team, pr_no: int, plan: dict, verdict_dir: Path) -> str:
    pr = api(f"repos/{repo()}/pulls/{pr_no}")
    head = plan["head_sha"]
    if pr["head"]["sha"] != head:
        return "head 변경 — 새 실행이 처리"
    if has_hotfix(team, head):
        return "hotfix 상태가 있어 쓰지 않음"
    states: dict[str, str] = {}
    for r in plan["reviewers"]:
        d = r["dept"]
        f = verdict_dir / f"verdict-{d}" / "verdict.json"
        quota = verdict_dir / f"verdict-{d}" / "quota.txt"
        if quota.exists():
            states[d] = "error"
            set_status(head, f"agent-review/{d}", "error", "quota")
            continue
        try:
            v = check_verdict(json.loads(f.read_text(encoding="utf-8")), head)
        except (OSError, ValueError, JobError) as e:
            states[d] = "error"
            set_status(head, f"agent-review/{d}", "error", f"결과 없음·형식 오류: {e}"[:140])
            continue
        state = "success" if v["verdict"] == "approve" else "failure"
        states[d] = state
        desc = "승인" if state == "success" else f"변경 요청 {len(v['blocking'])}건"
        set_status(head, f"agent-review/{d}", state, f"{r['name']}: {desc}")
        marker = f"<!-- agent-review:{d}:{head} -->"
        if not any(marker in b for b in bot_bodies(pr_no, team)):
            comment(pr_no, review_comment(d, r["name"], v))
    overall = aggregate(states)
    summary = ", ".join(f"{d}={s}" for d, s in states.items())
    if overall == "pending":
        set_status(head, "agent-review", "pending", f"재시도 필요 ({summary})")
    else:
        set_status(head, "agent-review", overall, summary)
    if overall == "failure":
        labels = [lb["name"] for lb in pr["labels"]]
        add_labels(pr_no, [review_round_label(labels)])
        # 1단계 임시 동작: address(수정)는 2단계. 사람이 보고 닫는다
    return overall


# ── CLI ──────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:  # noqa: C901 - 단계별 분기
    ap = argparse.ArgumentParser(prog="claude_jobs.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("write-prep")
    p.add_argument("--dept", required=True)
    p.add_argument("--mode", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--actor", required=True)
    p.add_argument("--out-dir", type=Path, required=True)
    p = sub.add_parser("write-post")
    p.add_argument("--dept", required=True)
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--out-dir", type=Path, required=True)
    p = sub.add_parser("check-verdict")
    p.add_argument("file", type=Path)
    p.add_argument("--head-sha", required=True)
    p = sub.add_parser("write-apply")
    p.add_argument("--dept", required=True)
    p.add_argument("--mode", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--retry", type=int, default=0)
    p.add_argument("--result", type=Path, required=True)
    p.add_argument("--patch", type=Path, required=True)
    p.add_argument("--report", type=Path)
    p = sub.add_parser("write-finalize")
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--reason", required=True)
    p = sub.add_parser("claude-settings")
    p.add_argument("--dept", required=True)
    p.add_argument("--trusted", type=Path, required=True)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--file", type=Path, required=True)
    p = sub.add_parser("review-prep")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("review-apply")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--verdict-dir", type=Path, required=True)
    args = ap.parse_args(argv)
    team = team_mod.Team.load()
    try:
        if args.cmd == "claude-settings":
            if args.dept not in team.depts:
                raise JobError(f"알 수 없는 부서: {args.dept}")
            data = claude_settings(args.dept, args.trusted, args.out_dir)
            args.file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return 0
        if args.cmd == "write-apply":
            print(write_apply(team, args.dept, args.mode, args.issue, args.retry, args.result,
                              args.patch, args.report))
            return 0
        if args.cmd == "write-finalize":
            print(team_mod.requeue(repo(), team, args.issue, args.reason,
                                   run_id=os.environ.get("GITHUB_RUN_ID")))
            return 0
        if args.cmd == "review-prep":
            plan = review_prep(team, args.pr)
            args.out.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            matrix = [{"dept": r["dept"], "token_secret": r["token_secret"],
                       "model_var": r["model_var"], "owner": r["owner"]}
                      for r in plan.get("reviewers", [])]
            set_output(skip=plan.get("skip", ""), fail="true" if plan.get("fail") else "false",
                       head_sha=plan.get("head_sha", ""), matrix=json.dumps(matrix),
                       base_sha=plan.get("base_sha", ""))
            print(json.dumps(plan, ensure_ascii=False))
            return 0
        if args.cmd == "review-apply":
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            print(review_apply(team, args.pr, plan, args.verdict_dir))
            return 0
        if args.cmd == "write-prep":
            info = write_prep(team, args.dept, args.mode, args.issue, args.actor, args.out_dir)
            set_output(token_secret=info["token_secret"], dept=info["dept"],
                       retry=str(info["retry"]), owner=info["owner"])
        elif args.cmd == "write-post":
            info = write_post(team, args.dept, args.workspace, args.out_dir)
            print(json.dumps(info, ensure_ascii=False))
        elif args.cmd == "check-verdict":
            v = check_verdict(json.loads(args.file.read_text(encoding="utf-8")), args.head_sha)
            print(json.dumps(v, ensure_ascii=False))
    except JobError as e:
        print(f"::error::{e}")
        set_output(error=str(e).replace("\n", " "))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
