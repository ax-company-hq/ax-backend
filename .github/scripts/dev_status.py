#!/usr/bin/env python3
"""AX 개발실 `status.json` 생성 (team.py dev-status).

시나리오 A(`e2e-result.json`), 실서버(`real-result.json`), 아침 리포트(`morning.json`)와
GitHub 이슈·PR을 모아 DevStatus 형식(contracts `dev_status.py`)의 JSON 하나로 합친다.
스위치(repo 변수 AX_DEVROOM)가 on이 아니면 아무것도 읽거나 쓰지 않는다.

비밀·고객 원문 규칙:
  - 읽는 환경변수는 AX_DEVROOM, GITHUB_REPOSITORY뿐이고 (GH_TOKEN은 gh가 읽는다) 출력에 쓰지 않는다
  - 이슈·PR은 번호·제목·라벨·작성자·시각만 쓴다. 이슈·PR 본문은 읽지 않는다
  - 댓글은 `<!-- chat: ... -->` 한 줄만 꺼내고, 비밀 모양 문자열을 가린 뒤 120자로 자른다
  - 사람 활동은 "누가 어느 이슈에 움직였다"는 사실(번호·제목)만 남기고 댓글 내용은 쓰지 않는다
쓰기는 같은 폴더의 임시 파일에 쓴 뒤 rename한다 (읽는 중에 반쯤 쓴 파일이 보이지 않게).
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import team as team_mod  # noqa: E402

KST = timezone(timedelta(hours=9))
SWITCH = "AX_DEVROOM"
FEED_LIMIT = 50
MAX_ARTIFACT_BYTES = 20_000_000  # 내려받는 zip 상한
ALLOWED_RUN_EVENTS = ("push", "schedule", "workflow_dispatch")
URL_PREFIXES = ("https://github.com/", "https://api.github.com/")
HISTORY_LIMIT = 120
TEXT_LIMIT = 120
CHAT = re.compile(r"<!--\s*chat:\s*(.*?)\s*-->", re.S)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
# 비밀 모양 문자열 (토큰·키·이메일·Authorization·긴 난수). 걸리면 가린다
SECRET = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,}|sk-[A-Za-z0-9_-]{10,}"
    r"|AIza[0-9A-Za-z_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"
    r"|(?i:bearer)\s+\S+|[\w.+-]+@[\w-]+\.[\w.-]+|[A-Za-z0-9+/_-]{32,})"
)
URL = re.compile(r"https?://\S+")

# 라벨 -> 막힘 종류 (BlockedKind)
BLOCK_LABELS = {"needs-human": "decision", "needs-split": "decision", "wip-wait": "wip",
                "blocked": "decision", "waiting": "decision"}
# artifact가 있는 워크플로: (워크플로 파일, artifact 이름, 안의 파일)
SOURCES = {
    "e2e": ("scenario-a.yml", "e2e-result", "e2e-result.json"),
    "real": ("real-run.yml", "real-result", "real-result.json"),
    "morning": ("morning.yml", "morning-json", "morning.json"),
}


def switch_on() -> bool:
    return os.environ.get(SWITCH, "") == "on"


def clean(text: object, limit: int = TEXT_LIMIT) -> str:
    """한 줄 문장으로: HTML 주석·URL 제거, 비밀 모양 가림, 공백 정리, 길이 제한."""
    s = HTML_COMMENT.sub(" ", str(text or ""))
    s = URL.sub("", s)
    s = SECRET.sub("***", s)
    s = " ".join(s.split())
    return s[: limit - 1] + "…" if len(s) > limit else s


def chat_line(body: str | None) -> str | None:
    m = CHAT.search(body or "")
    text = clean(m.group(1)) if m else ""
    return text or None


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- 입력: artifact와 GitHub ------------------------------------------
def read_json(path: Path | None) -> dict:
    if not path or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def trusted_run(run: dict, repo: str) -> bool:
    """같은 레포 dev의 push·schedule·수동 실행만 믿는다 (PR·포크 실행 artifact는 쓰지 않음)."""
    head_repo = (run.get("head_repository") or {}).get("full_name")
    return (head_repo == repo and run.get("event") in ALLOWED_RUN_EVENTS
            and run.get("head_branch") == "dev")


def safe_url(url: object) -> str | None:
    return url if isinstance(url, str) and url.startswith(URL_PREFIXES) else None


def scrub(node: object, limit_text: bool = False) -> object:
    """url 키는 GitHub 주소만 남기고 나머지는 None (javascript: 등 차단)."""
    if isinstance(node, dict):
        return {k: safe_url(v) if k == "url" else scrub(v) for k, v in node.items()}
    if isinstance(node, list):
        return [scrub(v) for v in node]
    return node


def clean_fields(items: object, keys: tuple[str, ...], limit: int = TEXT_LIMIT) -> list[dict]:
    out = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict):
            out.append({k: (clean(v, limit) if k in keys and v is not None else v)
                        for k, v in it.items()})
    return out


def sanitize_inputs(e2e: dict, morning: dict) -> None:
    """artifact에서 온 글자 필드도 clean()을 거친다 (제자리 수정)."""
    if isinstance(e2e.get("stages"), list):
        e2e["stages"] = [
            {**s, **{k: {**v, "note": clean(v["note"])} for k, v in s.items()
                     if k in ("unit", "chain", "real") and isinstance(v, dict) and v.get("note")}}
            for s in e2e["stages"] if isinstance(s, dict)]
    if "people" in morning:
        morning["people"] = clean_fields(morning["people"], ("who", "task", "detail"))
    if "orc" in morning:
        morning["orc"] = clean_fields(morning["orc"], ("title", "note"))
    bn = morning.get("bottlenecks")
    if isinstance(bn, dict):
        bn["blocked"] = clean_fields(bn.get("blocked"), ("title", "detail"))
        bn["caps"] = clean_fields(bn.get("caps"), ("name",))


def fetch_artifact_json(repo: str, source: str) -> dict:
    """dev의 가장 최근 성공 실행의 artifact에서 JSON 한 개를 읽는다 (gh 읽기 전용). 없으면 {}."""
    workflow, name, member = SOURCES[source]
    try:
        runs = team_mod.gh_api(
            f"repos/{repo}/actions/workflows/{workflow}/runs"
            "?status=success&branch=dev&per_page=20"
        ) or {}
        run = next((r for r in runs.get("workflow_runs") or [] if trusted_run(r, repo)), None)
        if not run:
            return {}
        arts = team_mod.gh_api(f"repos/{repo}/actions/runs/{run['id']}/artifacts") or {}
        art = next((a for a in arts.get("artifacts", [])
                    if a.get("name") == name and not a.get("expired")), None)
        if not art or int(art.get("size_in_bytes") or 0) > MAX_ARTIFACT_BYTES:
            return {}
        raw = subprocess.run(
            ["gh", "api", f"repos/{repo}/actions/artifacts/{art['id']}/zip"],
            check=True, capture_output=True,
        ).stdout
        if len(raw) > MAX_ARTIFACT_BYTES:
            return {}
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            hit = next((n for n in zf.namelist() if n.rsplit("/", 1)[-1] == member), None)
            if not hit or zf.getinfo(hit).file_size > 5_000_000:
                return {}
            data = json.loads(zf.read(hit).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (subprocess.CalledProcessError, OSError, ValueError, zipfile.BadZipFile, KeyError):
        print(f"::warning::{source} artifact를 읽지 못함 - 건너뜀", file=sys.stderr)
        return {}


def gh_list(path: str) -> list[dict]:
    try:
        data = team_mod.gh_api(path)
    except (subprocess.CalledProcessError, OSError):
        return []
    return data if isinstance(data, list) else []


def load_github(repo: str, now: datetime) -> dict:
    since = iso(now - timedelta(hours=24))
    return {
        "issues": gh_list(f"repos/{repo}/issues?state=open&per_page=100&sort=updated"),
        "prs": gh_list(f"repos/{repo}/pulls?state=open&per_page=100&sort=updated"),
        "comments": gh_list(
            f"repos/{repo}/issues/comments?since={since}&per_page=100&sort=created&direction=desc"),
    }


# -- 합치기 ----------------------------------------------------------
def labels_of(item: dict) -> list[str]:
    return [(x.get("name") if isinstance(x, dict) else str(x)) for x in item.get("labels", [])]


def issue_number(comment: dict) -> int | None:
    m = re.search(r"/issues/(\d+)$", comment.get("issue_url") or "")
    return int(m.group(1)) if m else None


def target_dept(team: team_mod.Team, labels: list[str]) -> str | None:
    for lb in labels:
        if lb.startswith("to:"):
            return team.resolve_dept(lb[3:])
    return None


def build_feed(team: team_mod.Team, gh: dict) -> list[dict]:
    items_by_no = {i["number"]: i for i in [*gh.get("issues", []), *gh.get("prs", [])]}
    feed: list[tuple[datetime, dict]] = []

    def add(at: str | None, kind: str, who: str, to: str | None, text: str, url: str | None):
        t = parse_time(at)
        if t and text:
            feed.append((t, {"at": iso(t), "kind": kind, "from": who, "to": to,
                             "text": text, "url": url}))

    for c in gh.get("comments", []):
        login = (c.get("user") or {}).get("login", "")
        no = issue_number(c)
        base = items_by_no.get(no or -1, {})
        title = clean(base.get("title", ""), 60)
        url = c.get("html_url") if str(c.get("html_url", "")).startswith("https://") else None
        dept = target_dept(team, labels_of(base))
        line = chat_line(c.get("body")) if login == team.bot_login else None
        if line:
            add(c.get("created_at"), "agent", f"{dept or 'ax'} 에이전트", None, line, url)
        elif team.is_member(login):
            what = f"#{no} {title}".strip() if no else title
            add(c.get("created_at"), "human", team.members[login]["name"], None,
                f"{what} 에 의견을 남김", url)
    for pr in gh.get("prs", []):
        login = (pr.get("user") or {}).get("login", "")
        title = clean(pr.get("title"), 70)
        if login == team.bot_login:
            dept = team.dept_of_branch((pr.get("head") or {}).get("ref", ""))
            add(pr.get("created_at"), "bot", "ax-bot", f"{dept or '-'} 에이전트",
                f"#{pr['number']} {title} PR을 올림", pr.get("html_url"))
        elif team.is_member(login):
            add(pr.get("created_at"), "human", team.members[login]["name"], None,
                f"#{pr['number']} {title} PR을 올림", pr.get("html_url"))
    feed.sort(key=lambda x: x[0], reverse=True)
    return [f for _, f in feed[:FEED_LIMIT]]


def build_blocked(gh: dict) -> list[dict]:
    out = []
    for it in gh.get("issues", []):
        if it.get("pull_request"):
            continue
        for lb in labels_of(it):
            if lb in BLOCK_LABELS:
                since = parse_time(it.get("updated_at"))
                out.append({"kind": BLOCK_LABELS[lb],
                            "title": clean(f"#{it['number']} {it.get('title')}", 90),
                            "detail": lb, "url": it.get("html_url"),
                            "since": iso(since) if since else None})
                break
    return out


def build_wip(team: team_mod.Team, gh: dict) -> list[dict]:
    counts: dict[str, int] = {}
    for pr in gh.get("prs", []):
        if (pr.get("user") or {}).get("login") != team.bot_login:
            continue
        dept = team.dept_of_branch((pr.get("head") or {}).get("ref", ""))
        if dept:
            counts[dept] = counts.get(dept, 0) + 1
    default = team.wip.get("open_bot_prs", 2)
    return [{"dept": d, "open_bot_prs": n,
             "limit": (team.wip_overrides.get(d) or {}).get("open_bot_prs", default)}
            for d, n in sorted(counts.items())]


def build_caps(team: team_mod.Team, fallback: list[dict]) -> list[dict]:
    try:
        status = team_mod.cap_status(team, list(team.members), "write")
    except Exception as e:  # noqa: BLE001 - 조회 실패는 아침 리포트 값으로 대신한다
        msg = f"::warning::cap 조회 실패({type(e).__name__}) - 아침 리포트 값 사용"
        print(msg, file=sys.stderr)
        return fallback
    caps = []
    for login, m in team.members.items():
        used = status.get(login, {}).get("used")
        if used is None:
            return fallback
        depts = sorted(d for d in team.depts if team.token_owner(d) == login)
        caps.append({"login": login, "name": m["name"], "write_used": used,
                     "write_cap": m["write_cap"], "depts": depts})
    return caps


def build_people(team: team_mod.Team, morning: list[dict], gh: dict) -> list[dict]:
    people = [p for p in morning if isinstance(p, dict)]
    for it in gh.get("issues", []):
        if it.get("pull_request") or "needs-human" not in labels_of(it):
            continue
        dept = target_dept(team, labels_of(it))
        who = team.members[team.owners(dept)[0]]["name"] if dept else "모두"
        people.append({"who": who, "task": clean(f"#{it['number']} {it.get('title')}", 80),
                       "detail": "사람 확인 필요", "state": "problem", "url": it.get("html_url")})
    for pr in gh.get("prs", []):
        login = (pr.get("user") or {}).get("login", "")
        if team.is_member(login):
            people.append({"who": team.members[login]["name"],
                           "task": clean(f"#{pr['number']} {pr.get('title')}", 80),
                           "detail": "열린 PR", "state": "doing", "url": pr.get("html_url")})
    return people


def merge_stages(e2e: dict, real: dict, prev: dict) -> tuple[list[dict], dict]:
    stages = [dict(s) for s in (e2e.get("stages") or prev.get("stages") or [])]
    totals = dict(e2e.get("totals") or prev.get("totals") or {})
    real_by_no = {s.get("no"): s.get("real") for s in real.get("stages", [])}
    if real_by_no:
        for s in stages:
            if s.get("no") in real_by_no:
                s["real"] = real_by_no[s["no"]]
        for key in ("real_pass", "real_runs_consecutive"):
            if key in (real.get("totals") or {}):
                totals[key] = real["totals"][key]
    if stages:
        totals.setdefault("total", len(stages))
        totals["unit_pass"] = sum((s.get("unit") or {}).get("state") == "pass" for s in stages)
        totals["chain_pass"] = sum((s.get("chain") or {}).get("state") == "pass" for s in stages)
        if real_by_no or any(s.get("real") for s in stages):
            totals["real_pass"] = sum((s.get("real") or {}).get("state") == "pass" for s in stages)
    return stages, totals


def merge_history(*sources: list, today: str, chain: int, real: int, have: bool) -> list[dict]:
    by_date: dict[str, dict] = {}
    for src in sources:  # 뒤에 오는 것(기존 status.json)이 앞의 것을 덮는다
        for h in src or []:
            if isinstance(h, dict) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(h.get("date"))):
                by_date[h["date"]] = {"date": h["date"], "chain_pass": int(h.get("chain_pass", 0)),
                                      "real_pass": int(h.get("real_pass", 0))}
    if have:
        by_date[today] = {"date": today, "chain_pass": chain, "real_pass": real}
    return [by_date[d] for d in sorted(by_date)][-HISTORY_LIMIT:]


def build_status(team: team_mod.Team, now: datetime, e2e: dict, real: dict, morning: dict,
                 prev: dict, gh: dict) -> dict:
    stages, totals = merge_stages(e2e, real, prev)
    signals = dict(morning.get("signals") or prev.get("signals") or {})
    blocked = build_blocked(gh)
    signals["needs_human_open"] = sum(1 for b in blocked if b["detail"] == "needs-human")
    morning_bn = morning.get("bottlenecks") or {}
    seen = {b["title"] for b in blocked}
    old_wip = [b for b in morning_bn.get("blocked", [])
               if b.get("kind") == "wip" and b.get("title") not in seen]
    return {
        "generated_at": iso(now), "scenario": "SCN-A", "stages": stages, "totals": totals,
        "history": merge_history(e2e.get("history"), prev.get("history"),
                                 today=now.astimezone(KST).strftime("%Y-%m-%d"),
                                 chain=totals.get("chain_pass", 0), real=totals.get("real_pass", 0),
                                 have=bool(stages)),
        "forecast": morning.get("forecast", prev.get("forecast")),
        "orc": morning.get("orc") or prev.get("orc") or [],
        "bottlenecks": {
            "caps": build_caps(team, morning_bn.get("caps", [])),
            "wip": build_wip(team, gh),
            "blocked": [*blocked, *old_wip],
        },
        "people": build_people(team, morning.get("people", []), gh),
        "feed": build_feed(team, gh),
        "signals": signals,
    }


def validate(status: dict) -> dict:
    """DevStatus로 검증·정규화한다. 모델이 없으면 그대로 두고, 맞지 않으면 예외."""
    try:
        from ax_contracts.data_access.dev_status import DevStatus
    except ImportError:
        print("::warning::ax_contracts 없음 - DevStatus 검증 건너뜀", file=sys.stderr)
        return status
    return DevStatus.model_validate(status).model_dump(by_alias=True, mode="json")


def write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".status-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # mkstemp는 0600 - nginx 컨테이너가 읽을 수 있어야 한다
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def run(team: team_mod.Team, out_dir: Path, e2e_dir: Path | None = None,
        real_dir: Path | None = None, morning_dir: Path | None = None,
        now: datetime | None = None, gh: dict | None = None, repo: str | None = None) -> int:
    if not switch_on():
        print(f"{SWITCH}이 on이 아님 - 무시")
        return 0
    now = now or datetime.now(UTC)
    repo = repo or os.environ.get("GITHUB_REPOSITORY", "")

    def source(name: str, directory: Path | None, member: str) -> dict:
        if directory is not None:
            return read_json(directory / member)
        return fetch_artifact_json(repo, name) if repo else {}

    e2e = source("e2e", e2e_dir, "e2e-result.json")
    real = source("real", real_dir, "real-result.json")
    morning = source("morning", morning_dir, "morning.json")
    sanitize_inputs(e2e, morning)
    sanitize_inputs(real, {})
    if gh is None:
        gh = load_github(repo, now) if repo else {}
    target = out_dir / "status.json"
    status = build_status(team, now, e2e, real, morning, read_json(target), gh)
    status = scrub(status)
    try:
        status = validate(status)
    except Exception as e:  # noqa: BLE001 - 잘못된 값으로 기존 파일을 덮지 않는다
        msg = f"::error::DevStatus 검증 실패, status.json 유지: {type(e).__name__}"
        print(msg, file=sys.stderr)
        return 1
    write_atomic(target, status)
    t = status["totals"]
    print(f"status.json 갱신: 단독 {t['unit_pass']} / 연결 {t['chain_pass']} / "
          f"실서버 {t['real_pass']} (총 {t['total']}), 피드 {len(status['feed'])}건")
    return 0
