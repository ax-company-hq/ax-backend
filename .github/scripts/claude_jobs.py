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
  max-turns     --file EXECUTION_JSON --limit N     (claude 잡: 턴 한도에 걸렸는지 → max_turns 출력)
  ci-failed     --run-id R --head-sha S --branch B  (bot: 봇 PR ci 실패 → 힌트 남기고 닫고 재대기)
  wip-resume    [--branch B]                        (bot: 자리가 난 부서의 wip-wait 이슈 1개 재개)
  stale-sweep                                       (bot: 48시간 멈춘 봇 PR·이슈 정리 + report 이슈)
  core-merged   --pr N                              (bot: 핵심 파일 PR 머지 → report에 멘션 한 줄)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import team as team_mod  # noqa: E402

MODES_WRITE = ("task", "request")  # address·ci-fix·arbiter-fix는 2단계
BLOCK_LABELS = {"external", "blocked", "obsolete", "in-progress", "quota", "needs-human",
                "needs-split"}
WIP_LABEL = "wip-wait"
SPLIT_LABEL = "needs-split"
STALE_LABEL = "stale"
MAX_WRITE_TURNS = 30  # _claude-write.yml의 --max-turns와 같게
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
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
CHAT_COMMENT = re.compile(r"<!--\s*chat:\s*(.*?)\s*-->", re.S)
MAX_CHAT = 120


def chat_line(text: str) -> str:
    """Claude가 남긴 마지막 chat 주석의 내용을 한 줄·120자로 정제한다 (없으면 빈 문자열)."""
    found = CHAT_COMMENT.findall(text or "")
    if not found:
        return ""
    line = re.sub(r"\s+", " ", HTML_COMMENT.sub("", found[-1]).replace("<!--", "")).strip()
    return line.replace("-->", "").replace("@", "")[:MAX_CHAT]


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
    chat = chat_line(out["body"]) or chat_line(out["summary"])
    # 봇 표시(<!-- ax-issue:N --> 등)를 흉내 내지 못하게 Claude가 쓴 HTML 주석은 지운다
    out["body"] = HTML_COMMENT.sub("", out["body"]).replace("<!--", "")
    if chat:  # 개발실 피드용 한 줄만 다시 붙인다 (정제한 한 줄, 다른 주석은 그대로 지워짐)
        out["body"] = out["body"].rstrip() + "\n\n<!-- chat: " + chat + " -->"
    if not out["title"].strip():
        raise JobError("result.json title이 비어 있음")
    if any(ord(ch) < 32 for ch in out["title"]):
        raise JobError("result.json title에 줄바꿈·제어 문자가 있음")
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
    open_prs = api(f"repos/{repo()}/pulls?state=open&per_page=100") or []
    linked = linked_prs(open_prs, issue_no, team)
    if linked:
        raise JobError(f"#{issue_no}에 연결된 열린 PR이 있음: #{linked[0]}")
    busy = api(f"repos/{repo()}/issues?state=open&labels=in-progress&per_page=100") or []
    wait = wip_check(team, dept, open_prs, busy)
    if wait:
        raise JobError(f"wip: {wait}")
    token_secret = team.owner_token(dept)
    owner = team.token_owner(dept)
    st = team_mod.cap_status(team, [owner], "write")[owner]  # run 목록 3회 조회
    if st["used"] is None:
        raise JobError("cap을 확인하지 못해 실행하지 않음 (fail-closed)")
    if not st["ok"]:
        raise JobError(f"quota: {st['reason']}")

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


ISSUE_MARKER = "<!-- ax-issue:{n} -->"
CLOSING = re.compile(r"(?i)\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)(\s*:?\s+)(#\d+)")


def strip_closing_keywords(text: str) -> str:
    """Claude가 쓴 'Fixes #7' 같은 문구가 다른 이슈를 닫지 않게 '관련 #7'로 바꾼다."""
    return CLOSING.sub(lambda m: f"관련 {m.group(3)}", text)


def linked_prs(prs: list[dict], issue_no: int, team: team_mod.Team) -> list[int]:
    """그 이슈에 연결된 열린 봇 PR (봇이 본문 끝에 붙인 표시로 찾는다)."""
    return [p["number"] for p in prs
            if p["user"]["login"] == team.bot_login and pr_issue(p) == issue_no]


def pr_issue(pr: dict) -> int | None:
    """봇 PR의 연결 이슈. 봇이 본문 끝에 붙이는 <!-- ax-issue:N -->의 마지막 것만 믿는다."""
    found = re.findall(r"<!-- ax-issue:(\d+) -->", pr.get("body") or "")
    return int(found[-1]) if found else None


def pr_mode(pr: dict) -> str | None:
    found = re.findall(r"<!-- by:[a-z0-9-]+ mode:([a-z-]+) -->", pr.get("body") or "")
    return found[-1] if found else None


def is_dept_bot_pr(team: team_mod.Team, pr: dict) -> str | None:
    """봇이 연 부서 브랜치 PR이면 그 부서 (revert/·openapi/sync- 같은 봇 브랜치는 제외)."""
    branch = (pr.get("head") or {}).get("ref", "")
    if (pr.get("user") or {}).get("login") != team.bot_login or team.bot_branch_rule(branch):
        return None
    return team.dept_of_branch(branch)


# ── WIP 제한 ─────────────────────────────────────────────
STALE_IN_PROGRESS_HOURS = 2  # 작성 한 번은 40분 안에 끝난다


def wip_check(team: team_mod.Team, dept: str, open_prs: list[dict],
              in_progress: list[dict], now: datetime | None = None) -> str | None:
    """새 작성을 시작하면 부서 한도를 넘는지. 넘으면 이유, 아니면 None.

    열린 봇 PR(부서 브랜치) 수가 open_bot_prs 이상이거나, 진행 중(in-progress) to:/dept: 이슈 수가
    in_progress_issues 이상이면 기다린다.
    """
    max_prs = team.wip_limit(dept, "open_bot_prs")
    max_issues = team.wip_limit(dept, "in_progress_issues")
    prs = [p for p in open_prs if is_dept_bot_pr(team, p) == dept]
    with_pr = {pr_issue(p) for p in prs}
    cutoff = (now or datetime.now(UTC)) - timedelta(hours=STALE_IN_PROGRESS_HOURS)
    mine = {f"to:{dept}", f"dept:{dept}"}
    # 열린 PR도 없이 오래 멈춘 in-progress(PR을 사람이 닫는 등으로 라벨만 남은 것)는 세지 않는다
    issues = [i for i in in_progress
              if "pull_request" not in i and mine & set(labels_of(i))
              and (i["number"] in with_pr or "updated_at" not in i
                   or parse_time(i["updated_at"]) >= cutoff)]
    if max_prs and len(prs) >= max_prs:
        return f"{dept} 열린 봇 PR {len(prs)}/{max_prs} — 하나가 머지·닫히면 자동으로 시작합니다."
    if max_issues and len(issues) >= max_issues:
        return (f"{dept} 진행 중 이슈 {len(issues)}/{max_issues} — 하나가 끝나면 자동으로 "
                "시작합니다.")
    return None


def release_closed_pr(team: team_mod.Team, pr_no: int) -> str:
    """머지되지 않고 사람이 닫은 봇 PR → 연결 이슈의 in-progress를 뗀다 (WIP 칸 비우기).

    봇이 닫은 경우(requeue·정리)는 라벨을 이미 정리했고 재시도가 막 시작됐을 수 있어
    건드리지 않는다.
    """
    pr = api(f"repos/{repo()}/pulls/{pr_no}")
    if pr.get("merged_at") or not is_dept_bot_pr(team, pr):
        return "머지됐거나 봇 부서 PR이 아님"
    issue_no = pr_issue(pr)
    events = api(f"repos/{repo()}/issues/{pr_no}/events?per_page=100") or []
    closer = next((e.get("actor") or {} for e in reversed(events) if e.get("event") == "closed"),
                  {}).get("login")
    if not issue_no or closer in (None, team.bot_login):
        return "봇이 닫았거나 연결 이슈 없음"
    remove_label(issue_no, "in-progress")
    return f"#{issue_no} in-progress 해제 (PR #{pr_no}을 {closer}가 닫음)"


def wip_resume(team: team_mod.Team, depts: list[str] | None = None) -> list[str]:
    """자리가 난 부서마다 wip-wait 이슈 1개를 재개한다 (priority 먼저, 그다음 번호 순)."""
    waiting = api(f"repos/{repo()}/issues?state=open&labels={WIP_LABEL}&per_page=100") or []
    waiting = [i for i in waiting if "pull_request" not in i]
    if not waiting:
        return []
    open_prs = api(f"repos/{repo()}/pulls?state=open&per_page=100") or []
    busy = api(f"repos/{repo()}/issues?state=open&labels=in-progress&per_page=100") or []
    by_dept: dict[str, list[dict]] = {}
    for i in waiting:
        if BLOCK_LABELS & set(labels_of(i)):
            continue  # quota·needs-human 등은 재개해도 prep에서 막힌다 (다음 이슈를 가로막지 않게)
        target = team_mod.write_target(team, labels_of(i))
        if target and (depts is None or target[0] in depts):
            by_dept.setdefault(target[0], []).append(i)
    done = []
    for dept, items in sorted(by_dept.items()):
        if wip_check(team, dept, open_prs, busy):
            continue
        items.sort(key=lambda i: ("priority" not in labels_of(i), i["number"]))
        # wip-wait는 작성이 실제로 시작될 때(_claude-write in-progress 단계) 뗀다.
        # 실행이 대기열에서 취소돼도 라벨이 남아 다음 재개 때 다시 잡힌다
        dept_, mode = team_mod.write_target(team, labels_of(items[0]))  # type: ignore[misc]
        done.append(team_mod.dispatch_write(repo(), team, items[0]["number"], dept_, mode))
    return done


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
    keep = [p for p in paths if p.startswith(prefixes) and not team.forbidden_name(p)
            and not team.is_human_file(p)]
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


def ruff_targets(team: team_mod.Team, dept: str, changes: list[tuple[str, str]]) -> list[str]:
    """(status, path) 중 이번 패치에서 추가·수정된 부서 폴더 안 .py 파일."""
    folder = team.depts[dept]["folder"].rstrip("/") + "/"
    return sorted(p for st, p in changes
                  if st[:1] in ("A", "M") and p.startswith(folder) and p.endswith(".py"))


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
        return team.token_owner(dept) if dept else None
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
DENY = ["Read(//proc)", "Read(//proc/**)", "Read(**/.git/**)", "Read(**/.env)",
        "Read(**/.env.*)", "Read(//home/runner/work/_temp/_runner_file_commands/**)",
        "Read(//home/runner/runners/**)", "Bash", "WebFetch", "WebSearch"]


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
    team_mod.post_comment(repo(), issue_no, body)


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
    if any(n.startswith("hop") for n in notes):
        team_mod.requeue(repo(), team, issue_no, f"요청 hop이 {MAX_HOP}을 넘음",
                         run_id=os.environ.get("GITHUB_RUN_ID"))
        return "requeued (hop)"

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
    title = strip_closing_keywords(result["title"])
    body = (f"{strip_closing_keywords(result['body'])}\n\nCloses #{issue_no}\n\n"
            f"{ISSUE_MARKER.format(n=issue_no)}\n<!-- by:{dept} mode:{mode} -->")
    pr = api(f"repos/{repo()}/pulls", "-X", "POST", "-f", f"title={title}",
             "-f", f"head={branch}", "-f", "base=dev", "-f", f"body={body}")
    # 여기부터는 실패해도 PR은 이미 있다. 잡을 실패시키면 finalize가 이슈를 잘못 재대기한다
    report = ""
    if report_file and report_file.exists():
        report = report_file.read_text(encoding="utf-8")[:MAX_BODY]
    steps = [
        lambda: add_labels(pr["number"], [f"by:{dept}"]),
        lambda: lock(pr["number"]),
        lambda: lock(issue_no),
    ]
    if report:
        steps.append(lambda: comment(pr["number"], "<!-- test-report -->\n" + report))
    for step in steps:
        try:
            step()
        except (subprocess.CalledProcessError, OSError) as e:
            print(f"::warning::PR #{pr['number']} 후속 단계 실패: {e}")
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
    # PR 이벤트면 페이로드, 수동 실행(workflow_dispatch)만 API
    pr = team_mod.event_pull_request(pr_no) or api(f"repos/{repo()}/pulls/{pr_no}")
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
            ("hotfix", "리뷰 생략", "not required")
        ):
            return {"skip": f"{s['context']}: {s['description']}"}
    team_mod.git("fetch", "--no-tags", "-q", "origin", f"+refs/pull/{pr_no}/head:refs/ax/pr")
    mb = team_mod.merge_base("HEAD", head)
    changes = team_mod.diff_changes(mb, head)
    classified = team.classify([c.path for c in changes])
    if not classified.depts and not classified.shared:
        return {"skip": "human_paths만 변경 (guards가 처리)"}
    if classified.depts and not team.review_required(classified):
        return {"skip": "not required — 부서 폴더 안 변경 (guards가 처리)"}
    person = review_author_person(team, pr["user"]["login"], pr["head"]["ref"])
    if not person:
        return {"skip": "브랜치에서 부서를 알 수 없음", "fail": True, "head_sha": head}
    depts = team.reviewers_for(list(classified.depts), person, classified.depts.get("contracts"))
    if not depts:
        return {"skip": "리뷰 부서 없음", "fail": True, "head_sha": head}
    # cap은 여기서 한 번만 계산해 matrix로 넘긴다 (리뷰어 잡마다 다시 조회하지 않음)
    caps = team_mod.cap_status(team, sorted({team.token_owner(d) for d in depts}), "review")
    reviewers = []
    for d in depts:
        owner = team.token_owner(d)
        reviewers.append({"dept": d, "token_secret": team.owner_token(d),
                          "model_var": review_model_var(d), "name": team.members[owner]["name"],
                          "owner": owner, "cap_ok": caps[owner]["ok"],
                          "cap_reason": caps[owner]["reason"]})
    return {"head_sha": head, "base_sha": mb, "reviewers": reviewers,
            "changed": list(classified.depts), "labels": labels}


def review_apply(team: team_mod.Team, pr_no: int, plan: dict, verdict_dir: Path) -> str:
    # API 유지: 리뷰가 도는 몇 분 사이에 새 커밋이 올라왔을 수 있다. 페이로드의 head는
    # 이벤트 시점 값이라, 지금 head와 같은지(오래된 결과를 새 head에 쓰지 않는지)
    # 확인하려면 최신 값이 필요하다
    pr = api(f"repos/{repo()}/pulls/{pr_no}")
    head = plan["head_sha"]
    if pr["head"]["sha"] != head:
        return "head 변경 — 새 실행이 처리"
    if has_hotfix(team, head):
        return "hotfix 상태가 있어 쓰지 않음"
    states: dict[str, str] = {}
    verdicts: dict[str, tuple[str, dict]] = {}
    for r in plan["reviewers"]:
        d = r["dept"]
        f = verdict_dir / f"verdict-{d}" / "verdict.json"
        quota = verdict_dir / f"verdict-{d}" / "quota.txt"
        if quota.exists():
            states[d] = "error"
            reason = quota.read_text(encoding="utf-8").strip()[:100] or "quota"
            set_status(head, f"agent-review/{d}", "error", f"quota — {reason}")
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
        verdicts[d] = (r["name"], v)
    overall = aggregate(states)
    summary = ", ".join(f"{d}={s}" for d, s in states.items())
    if overall == "pending":
        set_status(head, "agent-review", "pending", f"재시도 필요 ({summary})")
    else:
        set_status(head, "agent-review", overall, summary)
    # 코멘트는 상태 뒤에. 실패해도 상태는 이미 남아 있다
    bodies = bot_bodies(pr_no, team)
    for d, (name, v) in verdicts.items():
        if f"<!-- agent-review:{d}:{head} -->" in "\n".join(bodies):
            continue
        try:
            comment(pr_no, review_comment(d, name, v))
        except subprocess.CalledProcessError:
            print(f"::warning::agent-review/{d} 코멘트를 남기지 못함 (상태는 기록됨)")
    if overall == "failure":
        labels = [lb["name"] for lb in pr["labels"]]
        add_labels(pr_no, [review_round_label(labels)])
        # 1단계 임시 동작: address(수정)는 2단계. 사람이 보고 닫는다
    return overall


# ── auto-merge 판정 (fail-closed) ─────────────────────────
def automerge_decision(
    team: team_mod.Team, pr: dict, required: list[str] | None
) -> tuple[bool, str]:
    """자동 머지 예약 여부. required는 dev 필수 체크 이름 목록, 읽지 못했으면 None."""
    if required is None:
        return False, "dev 필수 체크를 읽지 못해 예약하지 않음 (fail-closed)"
    if "agent-review" not in required:
        return False, "agent-review가 아직 dev 필수 체크가 아님"
    if pr.get("state") != "open" or pr.get("draft"):
        return False, "열린 PR이 아니거나 draft"
    if (pr.get("base") or {}).get("ref") != "dev":
        return False, "base가 dev가 아님"
    if ((pr.get("head") or {}).get("repo") or {}).get("full_name") != pr.get("_repo"):
        return False, "포크 PR"
    author = (pr.get("user") or {}).get("login", "")
    if not team.is_trusted(author):
        return False, f"팀원·봇이 아님: {author}"
    branch = (pr.get("head") or {}).get("ref", "")
    labels = {lb["name"] for lb in pr.get("labels", [])}
    if branch.startswith("gen/") or "generated-pr" in labels:
        return False, "generate_pr이 만든 PR은 사람이 머지"
    if "external" in labels:
        return False, "external"
    return True, "예약"


def required_checks() -> list[str] | None:
    """봇 토큰으로 dev 필수 체크 이름을 읽는다. 어떤 실패든 None."""
    try:
        data = api(f"repos/{repo()}/branches/dev")
        contexts = data["protection"]["required_status_checks"]["contexts"]
    except (subprocess.CalledProcessError, KeyError, TypeError, ValueError):
        return None
    return list(contexts) if isinstance(contexts, list) else None


# ── 턴 한도 → 작업 분할 ───────────────────────────────────
def hit_max_turns(path: Path | None, limit: int = MAX_WRITE_TURNS) -> bool:
    """claude-code-action 실행 파일(execution_file)의 result가 턴 한도에 걸렸는지."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))  # type: ignore[arg-type]
    except (OSError, TypeError, ValueError):
        return False
    items = data if isinstance(data, list) else [data]
    for it in reversed(items):
        if isinstance(it, dict) and it.get("type") == "result":
            try:
                turns = int(it.get("num_turns") or 0)
            except (TypeError, ValueError):
                turns = 0
            return it.get("subtype") == "error_max_turns" or turns > limit
    return False


SPLIT_MARKER = "<!-- needs-split:{run} -->"


def mark_needs_split(team: team_mod.Team, issue_no: int) -> str:
    """재대기하지 않고 사람이 작업을 나누게 한다."""
    run = os.environ.get("GITHUB_RUN_ID", "")
    remove_label(issue_no, "in-progress")
    add_labels(issue_no, [SPLIT_LABEL])
    comment(issue_no, f"{SPLIT_MARKER.format(run=run)}\n**작업 분할 필요** — "
            f"작성 에이전트가 턴 한도({MAX_WRITE_TURNS})를 넘었습니다. "
            "한 번에 하기에 큰 작업입니다.\n"
            "이 이슈를 더 작은 작업 여러 개로 나눠 각각 to:<부서> 이슈로 올려 주세요. "
            "나눈 뒤 이 이슈는 닫으면 됩니다. (자동 재시도하지 않음)")
    return "needs-split"


# ── 봇 PR ci 실패 → 힌트 남기고 닫고 재대기 ─────────────────
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LOG_PREFIX = re.compile(r"^\S+\t[^\t]*\t")  # gh run view --log-failed: "잡\t단계\t"
STAMP = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT[\d:.]+Z ?")
LOG_NOISE = ("##[group]", "##[endgroup]", "[command]", "shell:", "env:", "Post job", "Cleaning up")
LOG_KEEP = re.compile(
    r"FAILED|^E\s|Error|error|-->|^\s*\d+ \||assert|Found \d+ error|깨짐|^\s+- |"
    r"^[A-Z]\d{3}\s|Traceback|failed|ERROR")
MAX_HINT_LINES = 40
MAX_HINT_CHARS = 3000


def summarize_ci_log(text: str) -> str:
    """실패한 ci 단계 로그에서 고칠 때 필요한 줄만 뽑는다 (실패 테스트, 오류 위치, ruff 코드)."""
    lines = []
    for raw in text.splitlines():
        line = STAMP.sub("", ANSI.sub("", LOG_PREFIX.sub("", raw))).rstrip()
        if not line.strip() or line.lstrip().startswith(LOG_NOISE) or "DeprecationWarning" in line:
            continue
        if "^[[" in line:  # 단계 스크립트 원문 출력 (색 코드가 글자로 찍힌 줄)
            continue
        lines.append(line)
    keep = [x for x in lines if LOG_KEEP.search(x)] or lines[-20:]
    seen, out = set(), []
    for x in keep:
        if x not in seen:
            seen.add(x)
            out.append(x)
    body = "\n".join(out[-MAX_HINT_LINES:]).replace("`" * 3, "'" * 3)
    return body[-MAX_HINT_CHARS:] or "(실패 로그 요약을 만들지 못함 — ci 실행 로그를 보세요)"


def dev_ci_red() -> bool:
    """dev가 초록이라고 확신할 수 없으면 True (그때는 봇 PR을 닫지 않는다).

    가장 최근 dev push ci가 실행·대기 중이면(방금 머지가 dev를 깼을 수 있음) True.
    cancelled·skipped는 건너뛰고, 그다음 완료 실행이 success가 아니면 True.
    """
    data = api(f"repos/{repo()}/actions/workflows/ci.yml/runs?branch=dev&event=push"
               "&per_page=10") or {}
    for run in data.get("workflow_runs", []):
        if run.get("status") != "completed":
            return True
        if run.get("conclusion") in ("cancelled", "skipped"):
            continue
        return run.get("conclusion") != "success"
    return True


def ci_failed(team: team_mod.Team, run_id: str, head_sha: str, branch: str,
              dev_red_var: bool) -> str:
    """봇 부서 PR의 ci가 실패하면 실패 요약을 힌트로 남기고 PR을 닫고 재대기한다.

    dev 자체가 빨간불이면(모든 PR이 같이 실패) 아무것도 하지 않는다.
    첫 실패는 retry-1로 바로 다시 작성, 같은 이슈 두 번째 실패는 needs-human.
    """
    if dev_red_var or dev_ci_red():
        return "dev 빨간불 — 봇 PR을 닫지 않음 (dev가 고쳐지면 ci를 다시 돌린다)"
    owner = repo().split("/")[0]
    prs = api(f"repos/{repo()}/pulls?state=open&head={owner}:{branch}&per_page=10") or []
    pr = next((x for x in prs if x["head"]["sha"] == head_sha), None)
    if not pr:
        return "열린 PR이 없거나 head가 바뀜"
    if not is_dept_bot_pr(team, pr):
        return "봇 부서 PR이 아님"
    issue_no = pr_issue(pr)
    if not issue_no:
        return "연결 이슈 표시가 없음"
    log = gh("run", "view", run_id, "-R", repo(), "--log-failed", check=False)
    hint = summarize_ci_log(log)
    reason = f"ci 실패 (PR #{pr['number']}) — 힌트:\n```\n{hint}\n```"
    result = team_mod.requeue(repo(), team, issue_no, reason, pr=pr["number"], head_sha=head_sha)
    if result == "retry-1":
        dept = team.dept_of_branch(branch)
        return f"{result}; {team_mod.dispatch_write(repo(), team, issue_no, dept, pr_mode(pr))}"
    return result


# ── 매일 정리: 48시간 멈춘 봇 PR·이슈 ───────────────────────
# 일부러 기다리거나 사람이 볼 이슈는 닫지 않는다
STALE_SKIP_LABELS = {"report", "incident", "in-progress", "wip-wait", "needs-human",
                     "needs-split", "delegated", "blocked", "external", "quota", "priority"}
REPORT_PREFIX = "[report] 정리"


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def list_pages(path: str, pages: int = 10) -> list[dict]:
    out: list[dict] = []
    for page in range(1, pages + 1):
        items = api(f"{path}&per_page=100&page={page}") or []
        out += items
        if len(items) < 100:
            break
    return out


def stale_targets(team: team_mod.Team, prs: list[dict], issues: list[dict],
                  now: datetime) -> tuple[list[dict], list[dict]]:
    """stale_hours보다 오래 변화가 없는 봇 부서 PR, 봇이 만든 이슈."""
    cutoff = now - timedelta(hours=team.stale_hours)
    old_prs = [p for p in prs if is_dept_bot_pr(team, p) and parse_time(p["updated_at"]) < cutoff]
    old_issues = [
        i for i in issues
        if "pull_request" not in i and (i.get("user") or {}).get("login") == team.bot_login
        and not STALE_SKIP_LABELS & set(labels_of(i)) and parse_time(i["updated_at"]) < cutoff
    ]
    return old_prs, old_issues


GREEN = {"success", "skipped", "neutral"}


def pr_all_green(sha: str) -> bool:
    """그 커밋의 커밋 상태와 check run이 전부 초록인지 (승인만 기다리는 PR). 못 읽으면 False."""
    try:
        status = api(f"repos/{repo()}/commits/{sha}/status") or {}
        runs = (api(f"repos/{repo()}/commits/{sha}/check-runs?per_page=100") or {}).get(
            "check_runs", [])
    except subprocess.CalledProcessError:
        return False
    if status.get("state") != "success":
        return False
    return all(r.get("status") == "completed" and r.get("conclusion") in GREEN for r in runs)


CORE_LABEL = "core-change"


def find_or_create_report(team: team_mod.Team, now: datetime) -> dict:
    """열린 봇 report 이슈 (없으면 오늘 것을 만든다. 매일 정리가 다음 날 새 것으로 바꾼다)."""
    issues = api(f"repos/{repo()}/issues?state=open&labels=report&per_page=100") or []
    report = next((i for i in issues if i.get("title", "").startswith(REPORT_PREFIX)
                   and (i.get("user") or {}).get("login") == team.bot_login), None)
    if report:
        return report
    return api(f"repos/{repo()}/issues", "-X", "POST", "-f",
               f"title={REPORT_PREFIX} {now.strftime('%Y-%m-%d')}", "-f",
               "body=핵심 파일 변경 기록 (매일 정리가 하루치를 모아 새 report를 만든다)",
               "-f", "labels[]=report")


def core_merged(team: team_mod.Team, pr_no: int, now: datetime | None = None) -> str:
    """핵심 파일을 바꾼 PR이 머지되면 core-change 라벨 + report 이슈에 멘션 한 줄."""
    now = now or datetime.now(UTC)
    pr = api(f"repos/{repo()}/pulls/{pr_no}")
    if not pr.get("merged_at") or pr["base"]["ref"] != "dev":
        return "dev 머지 아님"
    files = [f["filename"] for f in list_pages(f"repos/{repo()}/pulls/{pr_no}/files?", 30)]
    core = [p for p in files if team.is_core_path(p)]
    if not core:
        return "핵심 파일 변경 없음"
    add_labels(pr_no, [CORE_LABEL])
    report = find_or_create_report(team, now)
    shown = ", ".join(f"`{p}`" for p in core[:5]) + (
        f" 외 {len(core) - 5}개" if len(core) > 5 else "")
    who = team.data.get("core_notify", "")
    mention = f"@{who} " if who else ""
    author = (pr.get("user") or {}).get("login", "")
    title = (pr.get("title") or "")[:100]
    line = f"{mention}핵심 파일 변경 머지: #{pr_no} {title} (@{author}) — {shown}"
    comment(report["number"], line)
    return f"report #{report['number']}에 기록"


def labeled_merges(label: str, now: datetime) -> list[dict]:
    """지난 24시간 머지된 PR 중 그 라벨이 붙은 것."""
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = api(f"repos/{repo()}/issues?state=closed&labels={label}&since={since}"
                "&per_page=100") or []
    out = []
    for i in items:
        merged = (i.get("pull_request") or {}).get("merged_at")
        if merged and parse_time(merged) >= now - timedelta(hours=24):
            out.append(i)
    return out


def stale_sweep(team: team_mod.Team, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    prs = api(f"repos/{repo()}/pulls?state=open&per_page=100") or []
    issues = list_pages(f"repos/{repo()}/issues?state=open")
    old_prs, old_issues = stale_targets(team, prs, issues, now)
    # 체크가 모두 초록이고 사람 승인만 기다리는 PR(계약 PR 등)은 닫지 않는다
    waiting = [p for p in old_prs if pr_all_green(p["head"]["sha"])]
    old_prs = [p for p in old_prs if p not in waiting]
    hours = team.stale_hours
    lines = []
    for pr in old_prs:
        n = pr["number"]
        comment(n, f"{hours}시간 넘게 변화가 없어 닫습니다 (매일 정리).")
        api(f"repos/{repo()}/pulls/{n}", "-X", "PATCH", "-f", "state=closed")
        gh("api", "-X", "DELETE", f"repos/{repo()}/git/refs/heads/{pr['head']['ref']}",
           check=False)
        issue_no = pr_issue(pr)
        if issue_no:
            remove_label(issue_no, "in-progress")
            add_labels(issue_no, [team_mod.NEEDS_HUMAN])
            comment(issue_no, f"연결된 PR #{n}이 {hours}시간 넘게 멈춰 닫았습니다. "
                    "사람이 확인한 뒤 needs-human 라벨을 떼고 to: 라벨을 다시 붙이면 재개합니다.")
        lines.append(f"- PR #{n} {pr['title']}" + (f" (이슈 #{issue_no} → needs-human)"
                                                    if issue_no else ""))
    for i in old_issues:
        n = i["number"]
        comment(n, f"{hours}시간 넘게 변화가 없어 닫습니다 (매일 정리). "
                "필요하면 다시 열고 to: 라벨을 붙여 주세요.")
        add_labels(n, [STALE_LABEL])
        api(f"repos/{repo()}/issues/{n}", "-X", "PATCH", "-f", "state=closed",
            "-f", "state_reason=not_planned")
        lines.append(f"- 이슈 #{n} {i['title']}")
    resumed = wip_resume(team)
    core = labeled_merges(CORE_LABEL, now)
    day = now.strftime("%Y-%m-%d")
    body = [f"{hours}시간 넘게 변화가 없던 봇 PR·이슈 정리 ({day} UTC)", ""]
    body += lines or ["- 닫은 것 없음"]
    if waiting:
        body += ["", "**승인 대기 (체크 초록, 닫지 않음)**",
                 *[f"- PR #{p['number']} {p['title']}" for p in waiting]]
    if resumed:
        body += ["", "**wip-wait 재개**", *[f"- {r}" for r in resumed]]
    body += ["", "**지난 24시간 핵심 파일 변경** (.github/·.claude/·CODEOWNERS·기준표 toml)"]
    body += [f"- #{p['number']} {p['title']} (@{(p.get('user') or {}).get('login', '')})"
             for p in core] or ["- 없음"]
    for old in issues:
        if old.get("title", "").startswith(REPORT_PREFIX) and (old.get("user") or {}).get(
                "login") == team.bot_login:
            api(f"repos/{repo()}/issues/{old['number']}", "-X", "PATCH", "-f", "state=closed")
    new = api(f"repos/{repo()}/issues", "-X", "POST", "-f", f"title={REPORT_PREFIX} {day}",
              "-f", "body=" + "\n".join(body), "-f", "labels[]=report")
    return (f"PR {len(old_prs)}·이슈 {len(old_issues)} 닫음, 재개 {len(resumed)}, "
            f"report #{new['number']}")


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
    p.add_argument("--max-turns", default="false")
    p.add_argument("--dept")
    p.add_argument("--mode")
    p = sub.add_parser("max-turns")
    p.add_argument("--file", type=Path)
    p.add_argument("--limit", type=int, default=MAX_WRITE_TURNS)
    p = sub.add_parser("ci-failed")
    p.add_argument("--run-id", required=True)
    p.add_argument("--head-sha", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--dev-red", default="off")
    p = sub.add_parser("wip-resume")
    p.add_argument("--branch", default="")
    p.add_argument("--pr", type=int)
    sub.add_parser("stale-sweep")
    sub.add_parser("core-merged").add_argument("--pr", type=int, required=True)
    p = sub.add_parser("claude-settings")
    p.add_argument("--dept", required=True)
    p.add_argument("--trusted", type=Path, required=True)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--file", type=Path, required=True)
    p = sub.add_parser("ruff-targets")
    p.add_argument("--dept", required=True)
    p.add_argument("--base", required=True)
    p = sub.add_parser("automerge-check")
    p.add_argument("--pr", type=int, required=True)
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
        if args.cmd == "ruff-targets":
            out = team_mod.git("diff", "--name-status", "-z", "--no-renames", f"{args.base}..HEAD")
            parts = [x for x in out.split("\0") if x]
            changes = list(zip(parts[0::2], parts[1::2], strict=False))
            targets = ruff_targets(team, args.dept, changes)
            if targets:  # 대상이 없으면 아무것도 출력하지 않는다 (빈 줄은 빈 파일 이름이 됨)
                print("\n".join(targets))
            return 0
        if args.cmd == "automerge-check":
            try:
                pr = team_mod.event_pull_request(args.pr) or api(f"repos/{repo()}/pulls/{args.pr}")
                pr = {**pr, "_repo": repo()}
            except subprocess.CalledProcessError:
                pr = None
            ok, reason = (False, "PR을 읽지 못함") if pr is None else automerge_decision(
                team, pr, required_checks())
            set_output(ok="true" if ok else "false", reason=reason)
            return 0
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
            if args.max_turns == "true":
                print(mark_needs_split(team, args.issue))
                return 0
            result = team_mod.requeue(repo(), team, args.issue, args.reason,
                                      run_id=os.environ.get("GITHUB_RUN_ID"))
            print(result)
            if result == "retry-1":  # 첫 실패는 한 번 더 시도, 두 번째는 needs-human
                print(team_mod.dispatch_write(repo(), team, args.issue, args.dept, args.mode))
            return 0
        if args.cmd == "max-turns":
            hit = hit_max_turns(args.file, args.limit)
            set_output(max_turns="true" if hit else "false")
            return 0
        if args.cmd == "ci-failed":
            print(ci_failed(team, args.run_id, args.head_sha, args.branch, args.dev_red == "on"))
            return 0
        if args.cmd == "wip-resume":
            if args.pr:
                print(release_closed_pr(team, args.pr))
            dept = team.dept_of_branch(args.branch) if args.branch else None
            if args.branch and not dept:
                print("부서 브랜치가 아님")
                return 0
            for line in wip_resume(team, [dept] if dept else None) or ["재개할 이슈 없음"]:
                print(line)
            return 0
        if args.cmd == "core-merged":
            print(core_merged(team, args.pr))
            return 0
        if args.cmd == "stale-sweep":
            print(stale_sweep(team))
            return 0
        if args.cmd == "review-prep":
            plan = review_prep(team, args.pr)
            args.out.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            matrix = [{"dept": r["dept"], "token_secret": r["token_secret"],
                       "model_var": r["model_var"], "owner": r["owner"],
                       "cap_ok": r["cap_ok"], "cap_reason": r["cap_reason"]}
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
