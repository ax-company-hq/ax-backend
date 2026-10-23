#!/usr/bin/env python3
"""자동 개발 진행 (아침 리포트 · /go · relay).

스위치(repo 변수)가 on일 때만 동작한다. 꺼져 있으면 아무것도 읽거나 쓰지 않는다.
  AX_MORNING  아침 리포트 이슈 `[morning] YYYY-MM-DD` (08:30 KST)
  AX_GO       아침 이슈 댓글 `/go [N]` · `/stop` (code owner만), relay가 풀어 준 이슈의 작성 시작
  AX_RELAY    머지·주기마다 blocked-by 해제, request 이슈 답글 처리, 같은 요청 합치기

명령 (GITHUB_REPOSITORY, GH_TOKEN 필요):
  morning-prep  --e2e DIR --out DIR    후보·신호를 모으고 규모 판정(Claude 1회)이 가능한지 정한다
  morning-apply --in DIR --sizes FILE --out DIR
                                       아침 이슈를 만들고 morning.json을 쓴다
  go            이벤트 페이로드(GITHUB_EVENT_PATH)의 댓글을 처리한다 (/go, /stop)
  go-check      같은 댓글이 처리 대상(code owner의 명령)인지만 본다 (토큰 없이, 출력 ok=true|false)
  relay         blocked-by 해제 · answered · 중복 합치기

규칙: LLM은 규모 판정 한 번뿐이고 그 실행은 cap에 잡힌다(morning.yml run 제목). 작성 시작은
team.dispatch_write를 거친다. 이슈·댓글에는 `<!-- autopilot -->` 표시를 붙이고 이 표시가 있는
댓글은 무시한다. 이슈 라벨 `autopilot-test`가 붙은 이슈는 리허설이라 작성을 시작하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import team as team_mod  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
MARK = "<!-- autopilot -->"
KST = timezone(timedelta(hours=9))
MORNING_PREFIX = "[morning] "
TEST_LABEL = "autopilot-test"
FROM_LABEL = "from:morning"
SIZING_DEPT = "data"  # 규모 판정 Claude 1회의 토큰·cap 주인 부서 (core_notify 소유자의 부서)
CAP_LOW = 3  # 부서 토큰 주인의 남은 작성 cap이 이 값 이하면 체크하지 않는다
DEFAULT_CHECKED = 6  # 기본으로 체크해 두는 후보 수의 상한 (cap이 넉넉한 것만)
MAX_NAME = 80
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
TASK_LINE = re.compile(
    r"^- \[(?P<x>[ xX])\] (?P<text>.*?)<!-- task:(?P<no>\d+):(?P<dept>[a-z0-9-]+) -->\s*$"
)
TASK_NAME = re.compile(r"\*\*\d+\.\s*(?P<name>.+?)\*\*")
TASK_MARKER = re.compile(r"<!-- morning-task:(\d+) -->")
BLOCKED_BY = re.compile(r"(?i)\bblocked-by\s+#(\d+)")
RELAY_TO = re.compile(r"<!-- relay-to: (to:[a-z0-9-]+) -->")
GO_CMD = re.compile(r"^/go(?:\s+(\d+))?\s*$")
SIZES = ("S", "M", "L")
HANDLED = "<!-- go-handled:{id} -->"


def repo() -> str:
    return os.environ["GITHUB_REPOSITORY"]


def api(path: str, *extra: str):
    return team_mod.gh_api(path, *extra)


def switch(name: str) -> bool:
    return os.environ.get(name, "") == "on"


def say(issue: int, text: str) -> None:
    team_mod.post_comment(repo(), issue, f"{MARK}\n{text}")


def labels_of(issue: dict) -> list[str]:
    return [lb["name"] if isinstance(lb, dict) else lb for lb in issue.get("labels", [])]


def add_labels(issue: int, labels: list[str]) -> None:
    api(f"repos/{repo()}/issues/{issue}/labels", "-X", "POST",
        *[x for lb in labels for x in ("-f", f"labels[]={lb}")])


def remove_label(issue: int, label: str) -> None:
    try:
        api(f"repos/{repo()}/issues/{issue}/labels/{label}", "-X", "DELETE")
    except subprocess.CalledProcessError:
        pass  # 이미 없음


def clean(text: str, limit: int = MAX_NAME) -> str:
    """사람이 읽는 한 줄로: 주석·줄바꿈·마크다운 강조·@ 멘션을 없앤다 (데이터는 지시가 아님)."""
    text = HTML_COMMENT.sub("", text or "").replace("<!--", "").replace("-->", "")
    text = re.sub(r"[\r\n*`@]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def list_open_issues(label: str) -> list[dict]:
    items = api(f"repos/{repo()}/issues?state=open&labels={label}&per_page=100") or []
    return [i for i in items if "pull_request" not in i]


def code_owners(team: team_mod.Team, path: Path | None = None) -> set[str]:
    """CODEOWNERS의 `*`·`/.github/` 줄에 적힌 사람 중 team.yml 팀원 (소문자)."""
    owners: set[str] = set()
    text = (path or ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    for line in text.splitlines():
        parts = line.split("#", 1)[0].split()
        if parts and parts[0] in ("*", "/.github/"):
            owners |= {p[1:] for p in parts[1:] if p.startswith("@")}
    members = {m.lower() for m in team.members}
    return {o.lower() for o in owners if o.lower() in members}


# ── 아침 리포트 ───────────────────────────────────────────
def kst_day(now: datetime) -> str:
    return now.astimezone(KST).strftime("%Y-%m-%d")


def yesterday_window(now: datetime) -> tuple[datetime, datetime]:
    """어제(KST) 0시 ~ 오늘(KST) 0시를 UTC로."""
    today = now.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    return (today - timedelta(days=1)).astimezone(UTC), today.astimezone(UTC)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def pr_signals(team: team_mod.Team, prs: list[dict], now: datetime) -> dict:
    """어제 닫힌 PR로: 되돌림 수 · 부서별 머지율 (봇 부서 PR) · 전체 머지율."""
    start, end = yesterday_window(now)
    reverts, total, merged = 0, 0, 0
    by_dept: dict[str, list[int]] = {}
    for pr in prs:
        closed = parse_time(pr.get("closed_at"))
        if not closed or not (start <= closed < end):
            continue
        ok = bool(pr.get("merged_at"))
        if ok and pr["head"]["ref"].startswith("revert/"):
            reverts += 1
        total += 1
        merged += ok
        is_bot = pr["user"]["login"] == team.bot_login
        dept = team.dept_of_branch(pr["head"]["ref"]) if is_bot else None
        if dept:
            row = by_dept.setdefault(dept, [0, 0])
            row[0] += ok
            row[1] += 1
    return {
        "reverts_yesterday": reverts,
        "merge_rate": {
            "overall": round(merged / total, 3) if total else None,
            "by_dept": {d: round(m / t, 3) for d, (m, t) in sorted(by_dept.items())},
        },
    }


def load_stages(e2e_dir: Path | None, team: team_mod.Team) -> tuple[list[dict], dict, dict]:
    """(단계 목록, totals, history 입력). e2e 결과가 없으면 ([], {}, {})."""
    path = e2e_dir / "e2e-result.json" if e2e_dir else None
    if not path or not path.exists():
        return [], {}, {}
    data = json.loads(path.read_text(encoding="utf-8"))
    stages = []
    for s in data.get("stages", []):
        dept = team.resolve_dept(s.get("dept", ""))
        stages.append({
            "no": int(s["no"]), "name": clean(s.get("name", "")), "dept": dept,
            "req_ids": [clean(r, 30) for r in s.get("req_ids", [])],
            "unit": (s.get("unit") or {}).get("state", "pending"),
            "chain": (s.get("chain") or {}).get("state", "pending"),
        })
    return stages, data.get("totals", {}), {"history": data.get("history", [])}


def pick_candidates(stages: list[dict]) -> list[dict]:
    """연결·단독 중 pending/broken인 단계. 부서를 돌아가며(round-robin) 한 단계씩 뽑는다."""
    todo = [s for s in stages if s["dept"] and
            ({s["unit"], s["chain"]} & {"pending", "broken"})]
    queues: dict[str, list[dict]] = {}
    for s in sorted(todo, key=lambda s: s["no"]):
        queues.setdefault(s["dept"], []).append(s)
    ordered: list[dict] = []
    while any(queues.values()):
        for dept in sorted(queues):  # 부서 id 순으로 한 바퀴씩
            if queues[dept]:
                ordered.append(queues[dept].pop(0))
    return ordered


def remaining_caps(team: team_mod.Team, depts: list[str]) -> dict[str, int | None]:
    """부서 → 그 부서 토큰 주인의 오늘 남은 작성 cap (모르면 None, fail-closed로 취급)."""
    owners = sorted({team.token_owner(d) for d in depts})
    status = team_mod.cap_status(team, owners, "write") if owners else {}
    out: dict[str, int | None] = {}
    for d in depts:
        st = status.get(team.token_owner(d)) or {}
        out[d] = None if st.get("used") is None else st["cap"] - st["used"]
    return out


def active_tasks() -> dict[int, int]:
    """단계 번호 → 열린 morning 작업 이슈 번호."""
    found: dict[int, int] = {}
    for issue in list_open_issues(FROM_LABEL):
        m = TASK_MARKER.search(issue.get("body") or "")
        if m:
            found.setdefault(int(m.group(1)), issue["number"])
    return found


def forecast_from(history: list[dict], total: int, now: datetime) -> list[dict] | None:
    """최근 기록의 하루 평균 증가량으로 5일치 연결 통과 수를 예상한다 (2점 미만이면 None)."""
    pts = sorted((h for h in history if "date" in h), key=lambda h: h["date"])[-7:]
    if len(pts) < 2:
        return None
    span = datetime.fromisoformat(pts[-1]["date"]) - datetime.fromisoformat(pts[0]["date"])
    rate = (pts[-1]["chain_pass"] - pts[0]["chain_pass"]) / max(span.days, 1)
    out = []
    for i in range(1, 6):
        d = (now.astimezone(KST) + timedelta(days=i)).strftime("%Y-%m-%d")
        out.append({"date": d, "chain_pass": min(total, round(pts[-1]["chain_pass"] + rate * i)),
                    "real_pass": pts[-1].get("real_pass", 0)})
    return out


def morning_prep(team: team_mod.Team, e2e_dir: Path | None, out: Path,
                 now: datetime | None = None) -> dict:
    """후보·신호를 out/state.json에 모은다. LLM은 부르지 않는다. 출력할 값을 돌려준다."""
    now = now or datetime.now(UTC)
    out.mkdir(parents=True, exist_ok=True)
    stages, totals, extra = load_stages(e2e_dir, team)
    cands = pick_candidates(stages)
    caps = remaining_caps(team, sorted(team.depts))
    active = active_tasks()
    prs = api(f"repos/{repo()}/pulls?state=closed&base=dev&sort=updated&direction=desc"
              "&per_page=100") or []
    signals = pr_signals(team, prs, now)
    signals["needs_human_open"] = len(list_open_issues("needs-human"))
    waiting = list_open_issues("wip-wait")
    state = {
        "date": kst_day(now), "generated_at": now.isoformat(), "have_e2e": bool(stages),
        "totals": totals, "history": extra.get("history", []), "stages": stages,
        "candidates": cands, "caps": caps, "active": {str(k): v for k, v in active.items()},
        "signals": signals,
        "waiting": [{"number": i["number"], "title": clean(i["title"], 100)} for i in waiting],
    }
    (out / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    # LLM 입력은 단계 이름·부서·요구사항뿐 (이슈·고객 원문 없음)
    (out / "candidates.json").write_text(json.dumps(
        [{k: c[k] for k in ("no", "name", "dept", "req_ids", "unit", "chain")} for c in cands],
        ensure_ascii=False), encoding="utf-8")
    owner = team.token_owner(SIZING_DEPT)
    st = team_mod.cap_status(team, [owner], "write")[owner]
    can_size = bool(cands) and st.get("used") is not None and bool(st.get("ok"))
    return {"size": "true" if can_size else "false", "candidates": str(len(cands)),
            "token_secret": team.owner_token(SIZING_DEPT)}


def read_sizes(path: Path | None, cands: list[dict]) -> dict[int, str]:
    """Claude가 쓴 규모 파일을 검증한다: 후보 번호에 대한 S|M|L만 받는다 (나머지는 버림)."""
    if not path or not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    if not isinstance(raw, dict):
        return {}
    allowed = {c["no"] for c in cands}
    return {int(k): v for k, v in raw.items()
            if str(k).isdigit() and int(k) in allowed and v in SIZES}


def render_report(state: dict, sizes: dict[int, str]) -> str:
    sig, totals, date = state["signals"], state["totals"], state["date"]
    total = totals.get("total", 0)
    if state["have_e2e"]:
        score = (f"- 연결 {totals.get('chain_pass', 0)}/{total} (완성 기준) · "
                 f"단독 {totals.get('unit_pass', 0)}/{total}")
    else:
        score = "- 연결·단독: 없음 (시나리오 A e2e 결과가 아직 없음)"
    rate = sig["merge_rate"]
    by = ", ".join(f"{d} {round(v * 100)}%" for d, v in rate["by_dept"].items()) or "없음"
    overall = "없음" if rate["overall"] is None else f"{round(rate['overall'] * 100)}%"
    lines = [
        MARK, f"<!-- morning:{date} -->", "",
        f"## 신호 ({date})", score,
        "- 실서버 결과: 없음 (실서버 실행 연결 전)",
        f"- 어제 revert {sig['reverts_yesterday']}건 · "
        f"열린 needs-human {sig['needs_human_open']}건",
        f"- 머지율(어제, 봇 PR): 전체 {overall} · 부서별 {by}",
        "", "## 오늘 할 일 후보",
        "체크한 항목을 코드 오너가 `/go` (또는 `/go 3`: 앞에서 3개만)로 시작합니다. "
        "`/stop`은 대기 중인 작성(wip-wait)을 멈춥니다.", "",
    ]
    active = {int(k): v for k, v in state["active"].items()}
    checked = 0
    for c in state["candidates"]:
        size_txt = "규모 " + sizes[c["no"]] if c["no"] in sizes else "규모 -"
        desc = f"**{c['no']}. {c['name']}** — {c['dept']} · {size_txt}"
        if c["no"] in active:
            lines.append(f"- 진행 중 #{active[c['no']]}: {desc}")
            continue
        remain = state["caps"].get(c["dept"])
        low = remain is None or remain <= CAP_LOW
        on = (not low) and checked < DEFAULT_CHECKED
        checked += on
        box = "x" if on else " "
        note = "남은 cap 알 수 없음" if remain is None else f"남은 cap {remain}"
        if low:
            note += " — 부족해서 체크 안 함"
        lines.append(f"- [{box}] {desc} · {note} <!-- task:{c['no']}:{c['dept']} -->")
    if not state["candidates"]:
        lines.append("- 후보 없음 (pending·broken 단계가 없거나 e2e 결과가 없음)")
    return "\n".join(lines) + "\n"


def build_morning_json(team: team_mod.Team, state: dict, issue_url: str | None) -> dict:
    """DevStatus의 일부(people, bottlenecks, signals, forecast, orc)."""
    now = datetime.fromisoformat(state["generated_at"])
    total = state["totals"].get("total", 0)
    caps = []
    for login, m in team.members.items():
        depts = sorted(d for d in team.depts if team.token_owner(d) == login)
        remain = min((state["caps"][d] for d in depts
                      if state["caps"].get(d) is not None), default=None)
        used = m["write_cap"] - remain if remain is not None else 0
        caps.append({"login": login, "name": m["name"], "write_used": max(used, 0),
                     "write_cap": m["write_cap"], "depts": depts})
    blocked = [{"kind": "wip", "title": w["title"], "detail": f"#{w['number']} 부서 한도 대기",
                "url": None, "since": None} for w in state["waiting"]]
    orc = []
    for s in state["stages"]:
        if s["dept"] != "orchestrator":
            continue
        ids = [r for r in s["req_ids"] if r.startswith("ORC")]
        doing = str(s["no"]) in state["active"]
        if s["chain"] == "pass":
            st = "done"
        elif doing:
            st = "doing"
        else:
            st = "problem" if "broken" in (s["unit"], s["chain"]) else "todo"
        orc.append({"id": ids[0] if ids else f"ORC-S{s['no']:02d}", "title": s["name"],
                    "state": st, "note": None, "issue": state["active"].get(str(s["no"]))})
    people = [{"who": "모두", "task": f"오늘 후보 {len(state['candidates'])}건 확인 · /go",
               "detail": None, "state": "todo" if state["candidates"] else "done",
               "url": issue_url}]
    for stage_no, _num in sorted((int(k), v) for k, v in state["active"].items()):
        cand = next((c for c in state["candidates"] if c["no"] == stage_no), None)
        if cand:
            who = team.members[team.owners(cand["dept"])[0]]["name"]
            people.append({"who": who, "task": cand["name"], "detail": cand["dept"],
                           "state": "doing", "url": None})
    return {
        "generated_at": now.isoformat(), "scenario": "SCN-A",
        "signals": state["signals"],
        "bottlenecks": {"caps": caps, "wip": [], "blocked": blocked},
        "people": people, "orc": orc,
        "forecast": forecast_from(state.get("history", []), total, now),
    }


def morning_apply(team: team_mod.Team, in_dir: Path, sizes_path: Path | None,
                  out: Path) -> str:
    state = json.loads((in_dir / "state.json").read_text(encoding="utf-8"))
    sizes = read_sizes(sizes_path, state["candidates"])
    title = f"{MORNING_PREFIX}{state['date']}"
    existing = api(f"repos/{repo()}/issues?state=all&labels={FROM_LABEL}&per_page=100") or []
    found = next((i for i in existing if i["title"] == title and "pull_request" not in i), None)
    if found:
        url, result = found["html_url"], f"이미 있음 #{found['number']} (다시 만들지 않음)"
    else:
        body = render_report(state, sizes)
        created = api(f"repos/{repo()}/issues", "-X", "POST", "-f", f"title={title}",
                      "-f", f"body={body}", "-f", f"labels[]={FROM_LABEL}")
        url, result = created.get("html_url"), f"만듦 #{created['number']}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "morning.json").write_text(
        json.dumps(build_morning_json(team, state, url), ensure_ascii=False, indent=1),
        encoding="utf-8")
    return result


# ── /go · /stop ──────────────────────────────────────────
def task_items(body: str) -> list[dict]:
    """아침 이슈 본문의 후보 줄 → [{no, dept, name, checked}] (본문 순서)."""
    items = []
    for line in (body or "").splitlines():
        m = TASK_LINE.match(line.strip())
        if not m:
            continue
        name = TASK_NAME.search(m["text"])
        items.append({"no": int(m["no"]), "dept": m["dept"], "checked": m["x"] in "xX",
                      "name": clean(name["name"]) if name else f"단계 {m['no']}"})
    return items


def parse_command(body: str) -> tuple[str, int | None] | None:
    first = next((ln.strip() for ln in (body or "").splitlines() if ln.strip()), "")
    if first == "/stop":
        return "stop", None
    m = GO_CMD.match(first)
    return ("go", int(m.group(1)) if m.group(1) else None) if m else None


def eligible_comment(event: dict, team: team_mod.Team, path: Path | None = None) -> str | None:
    """처리할 댓글이면 None, 아니면 무시하는 이유. API 호출 없음 (페이로드와 CODEOWNERS만).

    code owner가 아니면 가장 먼저 걸러낸다. 작성자는 이벤트의 댓글 작성자만 쓴다
    (재실행한 사람인 github.actor가 아니다).
    """
    comment, issue = event.get("comment") or {}, event.get("issue") or {}
    login = (comment.get("user") or {}).get("login", "")
    if (comment.get("user") or {}).get("type") == "Bot" or login == team.bot_login:
        return "봇 댓글"
    if login.lower() not in code_owners(team, path):
        return "code owner가 아님"
    if MARK in (comment.get("body") or ""):
        return "autopilot 표시가 있는 댓글"
    if "pull_request" in issue or not (issue.get("title") or "").startswith(MORNING_PREFIX):
        return "아침 이슈가 아님"
    if (issue.get("user") or {}).get("login") != team.bot_login:
        return "봇이 만든 이슈가 아님"
    if issue.get("state") != "open":
        return "닫힌 이슈"
    if parse_command(comment.get("body") or "") is None:
        return "명령이 아님"
    return None


def create_task_issue(item: dict, morning: int, test: bool) -> int:
    body = "\n".join([
        MARK, f"<!-- morning-task:{item['no']} -->", "",
        f"시나리오 A 단계 **{item['no']}. {item['name']}** ({item['dept']}) 구현·보완 작업입니다.",
        f"출처: 아침 리포트 #{morning}. 단계의 요구사항 ID와 통과 조건은 `e2e/` 단계 표와 "
        "`e2e-detail.json`을 봅니다.",
    ])
    labels = [f"dept:{item['dept']}", "task", FROM_LABEL]
    if test:
        labels.append(TEST_LABEL)
    created = api(f"repos/{repo()}/issues", "-X", "POST",
                  "-f", f"title=[SCN-A] {item['name']} 단계 구현", "-f", f"body={body}",
                  *[x for lb in labels for x in ("-f", f"labels[]={lb}")])
    return created["number"]


def handle_go(event: dict, team: team_mod.Team, path: Path | None = None) -> list[str]:
    if not switch("AX_GO"):
        return ["AX_GO가 on이 아님 - 무시"]
    why = eligible_comment(event, team, path)
    if why:
        return [f"무시: {why}"]
    issue, comment = event["issue"], event["comment"]
    number, cid = issue["number"], comment["id"]
    cmd, limit = parse_command(comment["body"])  # type: ignore[misc]
    seen = HANDLED.format(id=cid)
    comments = api(f"repos/{repo()}/issues/{number}/comments?per_page=100") or []
    if any(seen in (c.get("body") or "") for c in comments):
        return [f"이미 처리한 댓글 {cid}"]
    test = TEST_LABEL in labels_of(issue)
    if cmd == "stop":
        return stop_waiting(number, seen)
    items = [i for i in task_items(issue.get("body") or "") if i["checked"]]
    if limit is not None:
        items = items[:limit]
    if not items:
        say(number, f"{seen}\n체크된 후보가 없습니다.")
        return ["체크된 후보 없음"]
    active = active_tasks()
    lines: list[str] = []
    for item in items:
        head = f"- {item['no']}. {item['name']}:"
        if item["no"] in active:
            lines.append(f"{head} 이미 진행 중 #{active[item['no']]} (건너뜀)")
            continue
        if item["dept"] not in team.depts:
            lines.append(f"{head} 알 수 없는 부서 (건너뜀)")
            continue
        num = create_task_issue(item, number, test)
        active[item["no"]] = num
        if test:
            lines.append(f"{head} #{num} (리허설 - 시작 안 함)")
        else:
            started = team_mod.dispatch_write(repo(), team, num, item["dept"], "task")
            lines.append(f"{head} #{num} {started}")
    say(number, f"{seen}\n/go 처리 ({comment['user']['login']}):\n" + "\n".join(lines))
    return lines


def stop_waiting(morning: int, seen: str) -> list[str]:
    """대기 중(wip-wait)인 작성을 blocked로 바꾼다 (이미 실행 중인 것은 못 멈춘다)."""
    stopped = []
    for issue in list_open_issues("wip-wait"):
        add_labels(issue["number"], ["blocked"])
        remove_label(issue["number"], "wip-wait")
        stopped.append(issue["number"])
    names = ", ".join(f"#{n}" for n in stopped) or "없음"
    say(morning, f"{seen}\n/stop: 대기 중이던 작성을 blocked로 바꿨습니다: {names}\n"
        "이미 실행 중인 작성은 멈추지 않습니다. 다시 시작하려면 blocked를 떼고 "
        "라벨을 다시 붙입니다.")
    return [f"stop: {names}"]


# ── relay ────────────────────────────────────────────────
def is_done(number: int) -> bool:
    """닫힌 이슈, 또는 머지된 PR이면 True (머지 없이 닫은 PR은 아님)."""
    try:
        info = api(f"repos/{repo()}/issues/{number}") or {}
        if info.get("state") != "closed":
            return False
        if "pull_request" in info:
            return bool((api(f"repos/{repo()}/pulls/{number}") or {}).get("merged_at"))
        return True
    except subprocess.CalledProcessError:
        return False  # 못 읽으면 풀지 않는다


def stored_to(team: team_mod.Team, issue: dict) -> str | None:
    """본문(또는 봇·팀원 댓글)에 저장된 `<!-- relay-to: to:<부서> -->`의 라벨."""
    texts = [issue.get("body") or ""]
    if not RELAY_TO.search(texts[0]):
        for c in api(f"repos/{repo()}/issues/{issue['number']}/comments?per_page=100") or []:
            if team.is_trusted((c.get("user") or {}).get("login", "")):
                texts.append(c.get("body") or "")
    for text in texts:
        m = RELAY_TO.search(text)
        if m and m.group(1)[3:] in team.depts:
            return m.group(1)
    return None


def relay_unblock(team: team_mod.Team) -> list[str]:
    done = []
    for issue in list_open_issues("blocked"):
        labels = labels_of(issue)
        if "needs-human" in labels:
            continue
        refs = sorted({int(n) for n in BLOCKED_BY.findall(issue.get("body") or "")})
        if not refs or not all(is_done(n) for n in refs):
            continue
        num = issue["number"]
        remove_label(num, "blocked")
        add = ["ready"]
        to = stored_to(team, issue)
        if to and to not in labels:
            add.append(to)
        add_labels(num, add)
        note = ", ".join(f"#{n}" for n in refs)
        if TEST_LABEL in labels:
            say(num, f"blocked-by {note}이 모두 닫혔습니다. 리허설이라 작성은 시작하지 않습니다.")
            done.append(f"#{num}: 해제 (리허설)")
            continue
        started = ""
        if switch("AX_GO"):
            started = " " + team_mod.dispatch_write(repo(), team, num)
        say(num, f"blocked-by {note}이 모두 닫혀 blocked를 풀었습니다 (ready).{started}")
        done.append(f"#{num}: 해제{started}")
    return done


def relay_answered(team: team_mod.Team) -> list[str]:
    """request 이슈에 팀원(사람)이 답하면 answered를 붙이고 대기·요청 라벨을 거둔다.

    거두는 to: 라벨은 `<!-- relay-to: -->` 댓글로 남겨 둔다 (사람이 되돌릴 수 있게).
    """
    done = []
    for issue in list_open_issues("request"):
        labels = labels_of(issue)
        if "answered" in labels or "in-progress" in labels:
            continue
        comments = api(f"repos/{repo()}/issues/{issue['number']}/comments?per_page=100") or []
        replied = [c for c in comments
                   if (c.get("user") or {}).get("login") in team.members
                   and MARK not in (c.get("body") or "")]
        if not replied:
            continue
        num = issue["number"]
        to = next((lb for lb in labels if lb.startswith("to:")), None)
        add_labels(num, ["answered"])
        for lb in [x for x in (to, "wip-wait") if x]:
            remove_label(num, lb)
        keep = f"\n<!-- relay-to: {to} -->" if to else ""
        say(num, f"사람이 답해 answered로 표시했습니다. 자동 작성은 멈춥니다.{keep}")
        done.append(f"#{num}: answered")
    return done


SOURCE = re.compile(r"요청 출처: #(\d+) \(([a-z0-9-]+)\)")


def request_key(issue: dict) -> tuple | None:
    src = SOURCE.search(issue.get("body") or "")
    to = next((lb for lb in labels_of(issue) if lb.startswith("to:")), None)
    if not src or not to:
        return None
    title = re.sub(r"^\[요청 [^\]]*\]\s*", "", issue["title"]).strip().lower()
    return (src.group(1), src.group(2), to, title)


def relay_dedupe(team: team_mod.Team) -> list[str]:
    groups: dict[tuple, list[dict]] = {}
    for issue in list_open_issues("request"):
        key = request_key(issue)
        if key:
            groups.setdefault(key, []).append(issue)
    done = []
    for issues in groups.values():
        if len(issues) < 2:
            continue
        issues.sort(key=lambda i: ("in-progress" not in labels_of(i), i["number"]))
        keep = issues[0]["number"]
        for dup in issues[1:]:
            if "in-progress" in labels_of(dup):
                continue
            say(dup["number"], f"같은 출처·제목의 요청 #{keep}과 합칩니다 (중복).")
            add_labels(dup["number"], ["obsolete"])
            api(f"repos/{repo()}/issues/{dup['number']}", "-X", "PATCH", "-f", "state=closed",
                "-f", "state_reason=not_planned")
            done.append(f"#{dup['number']}: #{keep}에 합침")
    return done


def relay(team: team_mod.Team) -> list[str]:
    if not switch("AX_RELAY"):
        return ["AX_RELAY가 on이 아님 - 무시"]
    out = relay_dedupe(team) + relay_answered(team) + relay_unblock(team)
    return out or ["처리할 것 없음"]


# ── CLI ──────────────────────────────────────────────────
def set_output(values: dict) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for k, v in values.items():
                fh.write(f"{k}={v}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="autopilot.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("morning-prep")
    p.add_argument("--e2e", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("morning-apply")
    p.add_argument("--in", dest="in_dir", type=Path, required=True)
    p.add_argument("--sizes", type=Path)
    p.add_argument("--out", type=Path, required=True)
    sub.add_parser("go")
    sub.add_parser("go-check")
    sub.add_parser("relay")
    args = ap.parse_args(argv)
    team = team_mod.Team.load()
    if args.cmd == "morning-prep":
        if not switch("AX_MORNING"):
            print("AX_MORNING이 on이 아님 - 무시")
            return 0
        set_output(morning_prep(team, args.e2e, args.out))
        return 0
    if args.cmd == "morning-apply":
        if not switch("AX_MORNING"):
            print("AX_MORNING이 on이 아님 - 무시")
            return 0
        print(morning_apply(team, args.in_dir, args.sizes, args.out))
        return 0
    if args.cmd in ("go", "go-check"):
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        if args.cmd == "go-check":
            why = eligible_comment(event, team) if switch("AX_GO") else "AX_GO off"
            set_output({"ok": "true" if why is None else "false"})
            print(why or "처리 대상")
            return 0
        for line in handle_go(event, team):
            print(line)
        return 0
    for line in relay(team):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
