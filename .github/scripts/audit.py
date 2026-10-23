#!/usr/bin/env python3
"""human-path-audit: 감사 경로 PR을 사람 승인 대신 감사 에이전트 2개로.

설계는 docs/automation-spec.md 12장.

  plan    --pr N --out FILE        (bot, 읽기만) 감사 대상인지, 기계 규칙, 감사 토큰 순번
  verdict FILE --head-sha S --role R   (claude 잡) 감사 결과 JSON 검사 → 정규화해 출력
  apply   --pr N --plan FILE --verdict-dir DIR    (bot) 상태 human-path-audit · 코멘트
  merged  --pr N                   (bot) 감사 경로가 머지되면 report 이슈에 한 줄 + audit-path 라벨

PR 코드는 실행하지 않는다 (git으로 읽기만, YAML·TOML은 safe_load·tomllib으로 파싱만).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import team as team_mod  # noqa: E402

CONTEXT = "human-path-audit"
ROLES = ("security", "standards")
ROLE_NAMES = {"security": "보안 감사", "standards": "기준 정합 감사"}
AUDIT_LABEL = "audit-path"
NEEDS_HUMAN = team_mod.NEEDS_HUMAN
MAX_FINDINGS = 20
MAX_TEXT = 2000
MAX_SUMMARY = 4000

# 감사 자체(워크플로·프롬프트·기계 규칙)는 바뀌면 무조건 사람
AUDIT_FILES = (
    ".github/workflows/human-path-audit.yml",
    ".github/claude/audit-security.md",
    ".github/claude/audit-standards.md",
    ".github/scripts/audit.py",
)
CODEOWNERS_FILES = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
# team.yml에서 권한·한도와 관련된 키 (값이 바뀌면 사람)
TEAM_GUARDED_KEYS = (
    "members", "bot_login", "hotfix_labelers", "human_files", "human_path_exceptions",
    "review_required_depts", "wip", "stale_hours", "bot_branches", "forbidden_in_dept",
    "shared_rules", "audit",
)
HEAD_REF = re.compile(
    r"github\.event\.pull_request\.head\.(?:sha|ref)|github\.head_ref|refs/pull/")
PROTECTION = re.compile(r"/protection\b|rulesets|required_status_checks|enforce_admins")
SECRET_REF = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)")
SECRET_DYNAMIC = re.compile(r"secrets\s*\[")
LEVEL = {"none": 0, "read": 1, "write": 2}
WORKFLOW = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")


class AuditError(Exception):
    """감사 결과 형식 오류 (fail-closed)."""


# ── 경로 분류 ─────────────────────────────────────────────
def audit_conf(team: team_mod.Team) -> dict:
    return team.data.get("audit", {})


def is_audit_path(team: team_mod.Team, path: str) -> bool:
    """감사 경로: docs/(부서 notes 제외)·contracts/·scripts/(핵심 제외)·루트 pyproject.toml."""
    conf = audit_conf(team)
    if path in conf.get("core_scripts", []):
        return False
    if path.startswith(team.notes_prefix) and team.dept_of_path(path):
        return False  # docs/notes/<부서>/는 부서 경로
    for p in conf.get("paths", []):
        if path == p or (p.endswith("/") and path.startswith(p)):
            return True
    return False


# ── 기계 규칙 (LLM 없이) ──────────────────────────────────
def _yaml(text: str) -> dict:
    data = yaml.safe_load(text) if text.strip() else {}
    return data if isinstance(data, dict) else {}


def _triggers(wf: dict) -> set[str]:
    on = wf.get("on", wf.get(True))  # YAML 1.1은 on을 True로 읽는다
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(x) for x in on}
    if isinstance(on, dict):
        return {str(k) for k in on}
    return set()


def _perm_levels(value) -> dict[str, int]:
    """permissions 값 → {권한: 수준}. read-all/write-all은 '*'."""
    if isinstance(value, str):
        if value == "write-all":
            return {"*": 2}
        if value == "read-all":
            return {"*": 1}
        return {}
    if isinstance(value, dict):
        return {str(k): LEVEL.get(str(v), 0) for k, v in value.items()}
    return {}


def _scopes(wf: dict) -> dict[str, dict[str, int]]:
    """워크플로·잡·앱 토큰 단계별 권한 수준."""
    out = {"(workflow)": _perm_levels(wf.get("permissions"))}
    for job_id, job in (wf.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        out[f"{job_id}"] = _perm_levels(job.get("permissions"))
        app: dict[str, int] = {}
        for step in job.get("steps") or []:
            with_ = (step or {}).get("with") if isinstance(step, dict) else None
            if isinstance(with_, dict):
                for k, v in with_.items():
                    if str(k).startswith("permission-"):
                        app[str(k)[len("permission-"):]] = max(
                            app.get(str(k)[len("permission-"):], 0), LEVEL.get(str(v), 0))
        if app:
            out[f"{job_id} (앱 토큰)"] = app
    return out


def _environments(wf: dict) -> dict[str, str]:
    out = {}
    for job_id, job in (wf.get("jobs") or {}).items():
        if isinstance(job, dict) and job.get("environment") is not None:
            env = job["environment"]
            out[str(job_id)] = str(env.get("name") if isinstance(env, dict) else env)
    return out


def workflow_findings(path: str, base: str, head: str) -> list[str]:
    try:
        b, h = _yaml(base), _yaml(head)
    except yaml.YAMLError:
        return [f"{path}: YAML을 읽을 수 없음"]
    found = []
    bs, hs = _scopes(b), _scopes(h)
    for scope, perms in hs.items():
        before = bs.get(scope, {})
        for name, level in perms.items():
            old = max(before.get(name, 0), before.get("*", 0))
            if level > old:
                found.append(f"{path}: 권한 상승 {scope} {name} → "
                             f"{[k for k, v in LEVEL.items() if v == level][0]}")
    be, he = _environments(b), _environments(h)
    for job in sorted(set(be) | set(he)):
        if be.get(job) != he.get(job):
            found.append(f"{path}: environment 변경 {job}: {be.get(job)} → {he.get(job)}")
    new = (_triggers(h) - _triggers(b)) & {"pull_request_target", "workflow_run"}
    for t in sorted(new):
        found.append(f"{path}: 트리거 추가 {t}")
    base_lines = set(base.splitlines())
    for line in head.splitlines():
        if line not in base_lines and HEAD_REF.search(line):
            found.append(f"{path}: PR head를 받는 줄 추가 — {line.strip()[:120]}")
    return found


def secret_findings(path: str, base: str, head: str) -> list[str]:
    old = set(SECRET_REF.findall(base))
    found = [f"{path}: 새 secrets 참조 {n}" for n in sorted(set(SECRET_REF.findall(head)) - old)]
    if len(SECRET_DYNAMIC.findall(head)) > len(SECRET_DYNAMIC.findall(base)):
        found.append(f"{path}: 새 secrets[...] 동적 참조")
    return found


def team_findings(base: str, head: str) -> list[str]:
    try:
        b, h = _yaml(base), _yaml(head)
    except yaml.YAMLError:
        return [".github/team.yml: YAML을 읽을 수 없음"]
    found = [f".github/team.yml: 권한·한도 키 변경 {k}" for k in TEAM_GUARDED_KEYS
             if b.get(k) != h.get(k)]
    bd, hd = b.get("depts") or {}, h.get("depts") or {}
    for d in sorted(set(bd) | set(hd)):
        if (bd.get(d) or {}).get("owners") != (hd.get(d) or {}).get("owners"):
            found.append(f".github/team.yml: depts.{d}.owners 변경")
    return found


def _flatten(data, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            out.update(_flatten(v, f"{prefix}{k}."))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            out.update(_flatten(v, f"{prefix}{i}."))
    else:
        out[prefix.rstrip(".")] = data
    return out


def toml_findings(path: str, base: str, head: str) -> list[str]:
    """기준표의 한도 상향: 숫자가 커지거나, false가 true로, 새 키."""
    try:
        b = _flatten(tomllib.loads(base)) if base.strip() else {}
        h = _flatten(tomllib.loads(head)) if head.strip() else {}
    except tomllib.TOMLDecodeError:
        return [f"{path}: TOML을 읽을 수 없음"]
    found = []
    for key, new in h.items():
        if key not in b:
            found.append(f"{path}: 새 키 {key} = {new}")
            continue
        old = b[key]
        if isinstance(new, bool) or isinstance(old, bool):
            if new is True and old is False:
                found.append(f"{path}: {key} false → true")
        elif isinstance(new, int | float) and isinstance(old, int | float) and new > old:
            found.append(f"{path}: 한도 상향 {key} {old} → {new}")
    return found


def mechanical_findings(team: team_mod.Team, changes: list[team_mod.Change],
                        read_base, read_head) -> list[str]:
    """감사 없이 사람에게 넘길 변경 목록 (비어 있으면 감사로 진행)."""
    found: list[str] = []
    for c in changes:
        p = c.path
        if p in AUDIT_FILES:
            found.append(f"{p}: 감사 워크플로·프롬프트·규칙 변경")
        if p in CODEOWNERS_FILES:
            found.append(f"{p}: CODEOWNERS 변경")
        if c.new_mode in (team_mod.SYMLINK_MODE, team_mod.GITLINK_MODE) and c.status != "D":
            found.append(f"{p}: 심볼릭 링크·서브모듈")
            continue
        base = _read(read_base, p) if c.status != "A" else ""
        head = _read(read_head, p) if c.status != "D" else ""
        if base is None or head is None:
            found.append(f"{p}: 읽을 수 없음 (바이너리 등)")
            continue
        if WORKFLOW.match(p):
            found += workflow_findings(p, base, head)
        found += secret_findings(p, base, head)
        if p == ".github/team.yml":
            found += team_findings(base, head)
        if p in team.human_files and p.endswith(".toml"):
            found += toml_findings(p, base, head)
        base_lines = set(base.splitlines())
        for line in head.splitlines():
            if line not in base_lines and PROTECTION.search(line):
                found.append(f"{p}: 브랜치 보호 관련 줄 추가 — {line.strip()[:120]}")
                break
    return found


def _read(reader, path: str) -> str | None:
    try:
        return reader(path)
    except UnicodeDecodeError:
        return None


# ── 감사 토큰 순번 ────────────────────────────────────────
def pick_auditors(team: team_mod.Team, pr_no: int, caps: dict[str, dict]) -> list[dict] | None:
    """PR 번호로 시작점을 돌려 리뷰 한도가 남은 서로 다른 토큰 2개 (없으면 None)."""
    depts = audit_conf(team).get("token_depts", [])
    if not depts:
        return None
    start = pr_no % len(depts)
    order = depts[start:] + depts[:start]
    chosen: list[str] = []
    for d in order:
        owner = team.owners(d)[0]
        if caps.get(owner, {}).get("ok") and owner not in [team.owners(x)[0] for x in chosen]:
            chosen.append(d)
        if len(chosen) == len(ROLES):
            break
    if len(chosen) < len(ROLES):
        return None
    return [{"role": role, "dept": d, "token_secret": team.owner_token(d),
             "owner": team.owners(d)[0], "name": team.members[team.owners(d)[0]]["name"]}
            for role, d in zip(ROLES, chosen, strict=True)]


# ── 감사 결과 검사 (fail-closed) ──────────────────────────
VERDICT_KEYS = {"head_sha", "role", "verdict", "findings", "summary"}
FINDING_KEYS = {"severity", "file", "detail"}


def check_verdict(data: object, head_sha: str, role: str) -> dict:
    """{"head_sha","role","verdict":"approve"|"reject","findings":[{severity,file,detail}],"summary"}.

    정해진 키만, 정해진 형식만 받는다. 조금이라도 다르면 AuditError (→ 통과 아님).
    """
    if not isinstance(data, dict) or set(data) != VERDICT_KEYS:
        raise AuditError("감사 결과 키가 형식과 다름")
    if data["head_sha"] != head_sha:
        raise AuditError("head_sha가 현재 head와 다름")
    if data["role"] != role:
        raise AuditError("role이 다름")
    if data["verdict"] not in ("approve", "reject"):
        raise AuditError("verdict는 approve|reject")
    findings = data["findings"]
    if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
        raise AuditError("findings 형식 오류")
    out_f = []
    for f in findings:
        if not isinstance(f, dict) or set(f) != FINDING_KEYS:
            raise AuditError("finding 키가 형식과 다름")
        if f["severity"] not in ("high", "medium", "low"):
            raise AuditError("severity는 high|medium|low")
        if not all(isinstance(f[k], str) and len(f[k]) <= MAX_TEXT for k in ("file", "detail")):
            raise AuditError("finding 값 형식 오류")
        out_f.append({k: f[k] for k in ("severity", "file", "detail")})
    if not isinstance(data["summary"], str) or len(data["summary"]) > MAX_SUMMARY:
        raise AuditError("summary 형식 오류")
    if data["verdict"] == "reject" and not out_f:
        raise AuditError("reject인데 findings가 비어 있음")
    if data["verdict"] == "approve" and any(f["severity"] == "high" for f in out_f):
        raise AuditError("high finding이 있는데 approve")
    return {"head_sha": head_sha, "role": role, "verdict": data["verdict"],
            "findings": out_f, "summary": data["summary"]}


def decide(plan: dict, verdicts: dict[str, dict | str]) -> tuple[str, str]:
    """역할별 결과(dict 또는 오류 문자열) → (상태, 설명). 둘 다 approve여야 success."""
    if set(verdicts) != set(ROLES):
        return "failure", "감사 결과가 모자람 (fail-closed)"
    bad = [r for r, v in verdicts.items() if not isinstance(v, dict)]
    if bad:
        return "failure", "감사 결과 형식 오류: " + ", ".join(ROLE_NAMES[r] for r in bad)
    rejected = [r for r, v in verdicts.items() if v["verdict"] != "approve"]
    if rejected:
        return "failure", "감사 거절: " + ", ".join(ROLE_NAMES[r] for r in rejected)
    return "success", "감사 2개 승인 (보안·기준 정합)"


# ── GitHub 입출력 ─────────────────────────────────────────
def repo() -> str:
    return os.environ["GITHUB_REPOSITORY"]


def api(path: str, *extra: str):
    return team_mod.gh_api(path, *extra)


def set_output(**kw: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    lines = "".join(f"{k}={v}\n" for k, v in kw.items())
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(lines)
    print(lines, end="")


def compat_result(sha: str) -> str:
    """PR head의 contracts-compat check run 결과 (없으면 none). 감사 입력으로만 쓴다."""
    try:
        runs = (api(f"repos/{repo()}/commits/{sha}/check-runs?check_name=contracts-compat")
                or {}).get("check_runs", [])
    except subprocess.CalledProcessError:
        return "unknown"
    if not runs:
        return "none"
    r = runs[0]
    return r.get("conclusion") or r.get("status") or "unknown"


def cmd_plan(team: team_mod.Team, pr_no: int) -> dict:
    pr = team_mod.event_pull_request(pr_no) or api(f"repos/{repo()}/pulls/{pr_no}")
    head = pr["head"]["sha"]
    if pr["state"] != "open" or pr["base"]["ref"] != "dev":
        return {"skip": "대상 아님 (열린 dev PR이 아님)"}
    if (pr["head"]["repo"] or {}).get("full_name") != repo():
        return {"skip": "포크 PR"}
    if not team.is_trusted(pr["user"]["login"]):
        return {"skip": "external"}
    is_bot = pr["user"]["login"] == team.bot_login
    rule = team.bot_branch_rule(pr["head"]["ref"]) if is_bot else None
    if rule and rule[1].get("review") == "skip":
        return {"skip": f"감사 생략 봇 브랜치 ({rule[0]}, guards가 처리)"}
    team_mod.git("fetch", "--no-tags", "-q", "origin", f"+refs/pull/{pr_no}/head:refs/ax/pr")
    if team_mod.git("rev-parse", "refs/ax/pr").strip() != head:
        return {"skip": "head 변경 — 새 실행이 처리"}
    mb = team_mod.merge_base("HEAD", head)
    changes = team_mod.diff_changes(mb, head)
    audited = sorted(c.path for c in changes if is_audit_path(team, c.path))
    if not audited:
        return {"skip": "감사 경로 변경 없음 (guards가 처리)"}
    found = mechanical_findings(team, changes, lambda p: team_mod.show(mb, p),
                                lambda p: team_mod.show(head, p))
    if found:
        return {"head_sha": head, "base_sha": mb, "audited": audited, "mechanical": found}
    owners = sorted({team.owners(d)[0] for d in audit_conf(team).get("token_depts", [])})
    caps = team_mod.cap_status(team, owners, "review")
    auditors = pick_auditors(team, pr_no, caps)
    if not auditors:
        reasons = ", ".join(c["reason"] for c in caps.values())
        return {"head_sha": head, "base_sha": mb, "audited": audited, "quota": reasons}
    return {"head_sha": head, "base_sha": mb, "audited": audited, "auditors": auditors,
            "compat": compat_result(head),
            "model_var": audit_conf(team).get("model_var", "AX_MODEL_REVIEW_SHARED")}


def set_status(sha: str, state: str, description: str) -> None:
    server, run_id = os.environ["GITHUB_SERVER_URL"], os.environ["GITHUB_RUN_ID"]
    api(f"repos/{repo()}/statuses/{sha}", "-X", "POST", "-f", f"state={state}",
        "-f", f"context={CONTEXT}", "-f", f"description={description[:140]}",
        "-f", f"target_url={server}/{repo()}/actions/runs/{run_id}")


def comment_once(pr_no: int, marker: str, body: str, team: team_mod.Team) -> None:
    comments = api(f"repos/{repo()}/issues/{pr_no}/comments?per_page=100") or []
    if any(marker in (c.get("body") or "") and (c.get("user") or {}).get("login") ==
           team.bot_login for c in comments):
        return
    try:
        team_mod.post_comment(repo(), pr_no, f"{marker}\n{body}")
    except subprocess.CalledProcessError:
        print("::warning::감사 코멘트를 남기지 못함 (상태는 기록됨)")


def cmd_mark(team: team_mod.Team, pr_no: int, plan: dict) -> str:
    """prep 단계 상태: 기계 규칙 → failure + needs-human, quota·감사 중 → pending."""
    head = plan.get("head_sha")
    if not head:
        return "상태 없음"
    if plan.get("mechanical"):
        set_status(head, "failure", f"needs-human — 기계 규칙 {len(plan['mechanical'])}건")
        api(f"repos/{repo()}/issues/{pr_no}/labels", "-X", "POST", "-f", f"labels[]={NEEDS_HUMAN}")
        lines = "\n".join(f"- {f}" for f in plan["mechanical"][:30])
        comment_once(pr_no, f"<!-- audit-mechanical:{head} -->",
                     f"**human-path-audit: 사람 확인 필요** ({head[:7]})\n\n"
                     f"감사 에이전트를 돌리지 않고 사람에게 넘깁니다 (기계 규칙).\n\n{lines}", team)
        return "mechanical"
    if plan.get("quota"):
        set_status(head, "pending", f"quota — {plan['quota']}"[:140])
        return "quota"
    set_status(head, "pending", "감사 중 (보안·기준 정합)")
    return "pending"


def cmd_apply(team: team_mod.Team, pr_no: int, plan: dict, verdict_dir: Path) -> str:
    pr = api(f"repos/{repo()}/pulls/{pr_no}")
    head = plan["head_sha"]
    if pr["head"]["sha"] != head:
        return "head 변경 — 새 실행이 처리"
    verdicts: dict[str, dict | str] = {}
    for a in plan["auditors"]:
        f = verdict_dir / f"audit-{a['role']}" / "verdict.json"
        try:
            verdicts[a["role"]] = check_verdict(json.loads(f.read_text(encoding="utf-8")), head,
                                                a["role"])
        except (OSError, ValueError, AuditError) as e:
            verdicts[a["role"]] = f"{type(e).__name__}: {e}"[:200]
    state, desc = decide(plan, verdicts)
    set_status(head, state, desc)
    lines = [f"**human-path-audit: {'통과' if state == 'success' else '실패'}** ({head[:7]})", ""]
    for a in plan["auditors"]:
        v = verdicts.get(a["role"])
        title = f"{ROLE_NAMES[a['role']]} ({a['name']} 토큰)"
        if not isinstance(v, dict):
            lines += [f"- {title}: 결과 형식 오류 → 통과 아님 ({v})"]
            continue
        lines += [f"- {title}: **{v['verdict']}** — {v['summary'][:500]}"]
        lines += [f"  - [{x['severity']}] {x['file']}: {x['detail'][:500]}" for x in v["findings"]]
    comment_once(pr_no, f"<!-- audit-result:{head} -->", "\n".join(lines), team)
    return state


REPORT_PREFIX = "[report] 정리"


def cmd_merged(team: team_mod.Team, pr_no: int) -> str:
    """감사 경로가 머지되면 audit-path 라벨 + report 이슈에 @알림 한 줄."""
    pr = api(f"repos/{repo()}/pulls/{pr_no}")
    if not pr.get("merged_at") or pr["base"]["ref"] != "dev":
        return "머지 아님"
    files = [f["filename"] for f in
             api(f"repos/{repo()}/pulls/{pr_no}/files?per_page=100") or []]
    audited = [p for p in files if is_audit_path(team, p)]
    if not audited:
        return "감사 경로 없음"
    api(f"repos/{repo()}/issues/{pr_no}/labels", "-X", "POST", "-f", f"labels[]={AUDIT_LABEL}")
    issues = api(f"repos/{repo()}/issues?state=open&labels=report&per_page=100") or []
    report = next((i for i in issues if i.get("title", "").startswith(REPORT_PREFIX)
                   and (i.get("user") or {}).get("login") == team.bot_login), None)
    if not report:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        report = api(f"repos/{repo()}/issues", "-X", "POST", "-f",
                     f"title={REPORT_PREFIX} {day}", "-f",
                     "body=감사 경로 머지 기록 (매일 정리가 하루치를 모아 새 report를 만든다)",
                     "-f", "labels[]=report")
    who = audit_conf(team).get("notify", "")
    shown = ", ".join(f"`{p}`" for p in audited[:5]) + (
        f" 외 {len(audited) - 5}개" if len(audited) > 5 else "")
    title = (pr.get("title") or "")[:100]
    mention = f"@{who} " if who else ""
    team_mod.post_comment(repo(), report["number"],
                          f"{mention}감사 경로 머지: #{pr_no} {title} — {shown}")
    return f"report #{report['number']}에 기록"


# ── CLI ──────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="audit.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("mark")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--plan", type=Path, required=True)
    p = sub.add_parser("verdict")
    p.add_argument("file", type=Path)
    p.add_argument("--head-sha", required=True)
    p.add_argument("--role", required=True, choices=ROLES)
    p = sub.add_parser("apply")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--verdict-dir", type=Path, required=True)
    p = sub.add_parser("merged")
    p.add_argument("--pr", type=int, required=True)
    args = ap.parse_args(argv)
    team = team_mod.Team.load()
    if args.cmd == "plan":
        plan = cmd_plan(team, args.pr)
        args.out.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        matrix = [{k: a[k] for k in ("role", "dept", "token_secret")}
                  for a in plan.get("auditors", [])]
        set_output(skip=plan.get("skip", ""), head_sha=plan.get("head_sha", ""),
                   base_sha=plan.get("base_sha", ""), matrix=json.dumps(matrix),
                   compat=plan.get("compat", ""), model_var=plan.get("model_var", ""))
        print(json.dumps(plan, ensure_ascii=False))
        return 0
    if args.cmd == "mark":
        print(cmd_mark(team, args.pr, json.loads(args.plan.read_text(encoding="utf-8"))))
        return 0
    if args.cmd == "verdict":
        try:
            v = check_verdict(json.loads(args.file.read_text(encoding="utf-8")), args.head_sha,
                              args.role)
        except (OSError, ValueError, AuditError) as e:
            print(f"::error::감사 결과 형식 오류: {e}")
            return 1
        print(json.dumps(v, ensure_ascii=False))
        return 0
    if args.cmd == "apply":
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        print(cmd_apply(team, args.pr, plan, args.verdict_dir))
        return 0
    print(cmd_merged(team, args.pr))
    return 0


if __name__ == "__main__":
    sys.exit(main())

