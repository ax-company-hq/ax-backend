"""claude_jobs.py 순수 함수 테스트 (GitHub 호출 없음)."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("claude_jobs", SCRIPTS / "claude_jobs.py")
cj = importlib.util.module_from_spec(spec)
sys.modules["claude_jobs"] = cj
spec.loader.exec_module(cj)
BOT = "ax-company-hq-bot[bot]"


@pytest.fixture(scope="module")
def team():
    return cj.team_mod.Team.load()


# ── result.json ──────────────────────────────────────────
def test_check_result_ok(team) -> None:
    r = cj.check_result({"title": "feat(data): x", "body": "b", "summary": "s",
                         "requests": [{"to": "finance", "text": "t"}]}, team)
    assert r["requests"] == [{"to": "finance", "text": "t"}]


@pytest.mark.parametrize(
    "data",
    [[], {"title": ""}, {"title": "x" * 200}, {"title": "t", "requests": "x"},
     {"title": "t", "requests": [{"to": 1, "text": "x"}]},
     {"title": "t", "requests": [{"to": "a", "text": "x"}] * 6}],
)
def test_check_result_rejects(team, data) -> None:
    with pytest.raises(cj.JobError):
        cj.check_result(data, team)


# ── verdict.json ─────────────────────────────────────────
def test_check_verdict() -> None:
    v = cj.check_verdict({"head_sha": "a", "verdict": "changes", "blocking": ["x"]}, "a")
    assert v["blocking"] == ["x"] and v["notes"] == []
    with pytest.raises(cj.JobError):
        cj.check_verdict({"head_sha": "b", "verdict": "approve"}, "a")  # 다른 head
    with pytest.raises(cj.JobError):
        cj.check_verdict({"head_sha": "a", "verdict": "maybe"}, "a")
    with pytest.raises(cj.JobError):
        cj.check_verdict({"head_sha": "a", "verdict": "changes", "blocking": []}, "a")


# ── requests ─────────────────────────────────────────────
def test_plan_requests(team) -> None:
    reqs = [{"to": "finance", "text": "금액 필드 추가"}, {"to": "nowhere", "text": "x"},
            {"to": "infra-dejafix", "text": "y"}]
    create, notes = cj.plan_requests(team, 12, 0, reqs, [])
    assert [c["to"] for c in create] == ["finance", "dejafix"]
    assert all(c["hop"] == 1 and c["marker"].startswith("<!-- req:12:") for c in create)
    assert len(notes) == 1 and "nowhere" in notes[0]
    # 같은 표시가 이미 있으면 다시 만들지 않는다
    again, _ = cj.plan_requests(team, 12, 0, reqs[:1], [f"x {create[0]['marker']} y"])
    assert again == []
    # hop > 3이면 만들지 않는다
    deep, notes = cj.plan_requests(team, 12, 3, reqs[:1], [])
    assert deep == [] and "hop" in notes[0]


def test_hop_and_retry() -> None:
    assert cj.hop_of("hop: 2\n\n본문") == 2 and cj.hop_of("본문") == 0
    assert cj.retry_count(["retry-2", "task"]) == 2 and cj.retry_count([]) == 0


def test_branch_name() -> None:
    assert cj.branch_name("data", 12, "[DEP-DATA-02] 영향 분석 Impact Analysis!", 0) == (
        "data/12-dep-data-02-impact-analysis-r0")
    assert cj.branch_name("data", 3, "한글만", 1) == "data/3-task-r1"


def test_branch_is_recognized(team) -> None:
    assert team.dept_of_branch(cj.branch_name("dejafix", 5, "x", 2)) == "dejafix"


# ── review ───────────────────────────────────────────────
def test_review_author_person(team) -> None:
    assert cj.review_author_person(team, BOT, "data/12-x-r0") == "JangJaehyeonn"
    assert cj.review_author_person(team, BOT, "contracts/3-x-r0") == "haakim7"
    assert cj.review_author_person(team, BOT, "weird") is None
    assert cj.review_author_person(team, "bxbae", "support/x") == "bxbae"


def test_review_model_var() -> None:
    assert cj.review_model_var("infra") == "AX_MODEL_REVIEW_SHARED"
    assert cj.review_model_var("finance") == "AX_MODEL_REVIEW"


@pytest.mark.parametrize(
    ("states", "overall"),
    [({"finance": "success"}, "success"),
     ({"finance": "success", "planning": "failure"}, "failure"),
     ({"finance": "success", "planning": "error"}, "pending"),
     ({"finance": "failure", "planning": "error"}, "failure"),
     ({}, "failure")],
)
def test_aggregate(states, overall) -> None:
    assert cj.aggregate(states) == overall


@pytest.mark.parametrize(
    ("labels", "label"),
    [([], "review-round-1"), (["review-round-1"], "review-round-2"),
     (["review-round-2", "x"], "review-round-3"), (["review-round-3"], "review-round-3")],
)
def test_review_round_label(labels, label) -> None:
    assert cj.review_round_label(labels) == label


def test_review_comment_has_marker_and_blocking() -> None:
    v = {"head_sha": "a" * 40, "verdict": "changes", "blocking": ["입력 검증 없음"],
         "notes": ["n"], "comment": "c"}
    body = cj.review_comment("finance", "태윤", v)
    assert body.startswith("<!-- agent-review:finance:") and "[agent:finance] 변경 요청" in body
    assert "- 입력 검증 없음" in body


# ── 패치 ─────────────────────────────────────────────────
def test_patch_paths() -> None:
    patch = (b"diff --git a/agents/data/x.py b/agents/data/x.py\n+x\n"
             b"diff --git a/old.py b/agents/data/new.py\n")
    assert cj.patch_paths(patch) == ["agents/data/new.py", "agents/data/x.py", "old.py"]


def run(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_write_post_keeps_only_allowed_paths(team, tmp_path: Path) -> None:
    ws, out = tmp_path / "ws", tmp_path / "out"
    ws.mkdir()
    out.mkdir()
    run(ws, "init", "-q", "-b", "dev")
    (ws / "agents/data").mkdir(parents=True)
    (ws / "agents/data/a.py").write_text("a\n")
    (ws / "README.md").write_text("r\n")
    run(ws, "add", "-A")
    run(ws, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "base")
    run(ws, "branch", "ax-baseline")
    (ws / "agents/data/a.py").write_text("a2\n")  # 허용
    (ws / "agents/data/CLAUDE.md").write_text("x\n")  # 부서 폴더 안 금지 이름
    (ws / "docs/notes/data").mkdir(parents=True)
    (ws / "docs/notes/data/memo.md").write_text("m\n")  # 허용
    (ws / "README.md").write_text("changed\n")  # 사람 경로 → 버림
    (ws / "agents/finance").mkdir(parents=True)
    (ws / "agents/finance/b.py").write_text("b\n")  # 남의 부서 → 버림
    (out / "result.json").write_text(json.dumps({"title": "feat(data): t", "body": "",
                                                 "summary": "", "requests": []}))
    info = cj.write_post(team, "data", ws, out)
    assert sorted(info["kept"]) == ["agents/data/a.py", "docs/notes/data/memo.md"]
    assert set(info["dropped"]) == {"README.md", "agents/finance/b.py", "agents/data/CLAUDE.md"}
    patch = (out / "changes.patch").read_bytes()
    assert sorted(cj.patch_paths(patch)) == ["agents/data/a.py", "docs/notes/data/memo.md"]


def test_write_post_without_result_fails(team, tmp_path: Path) -> None:
    with pytest.raises(cj.JobError):
        cj.write_post(team, "data", tmp_path, tmp_path)


def test_claude_settings_has_deny_and_hook(tmp_path: Path) -> None:
    s = cj.claude_settings("data", tmp_path / "trusted", tmp_path / "ax-out")
    assert {"Bash", "Read(**/.git/**)", "Read(//proc)", "Read(**/.env)"} <= set(
        s["permissions"]["deny"])
    hook = s["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
    cmd = hook["hooks"][0]["command"]
    assert "guard-folder.mjs" in cmd and "--depts data" in cmd and "ax-out" in cmd


def test_title_control_chars_rejected(team) -> None:
    with pytest.raises(cj.JobError):
        cj.check_result({"title": "feat\ndiff --git a/x b/x"}, team)


@pytest.mark.parametrize(
    ("text", "out"),
    [("Fixes #7 and closes #8", "관련 #7 and 관련 #8"), ("resolved: #3", "관련 #3"),
     ("see #9", "see #9"), ("prefix", "prefix")],
)
def test_strip_closing_keywords(text, out) -> None:
    assert cj.strip_closing_keywords(text) == out


# ── auto-merge (fail-closed) ─────────────────────────────
REQ = ["ci", "folder-guard", "agent-review"]


def am_pr(**kw):
    pr = {"state": "open", "draft": False, "base": {"ref": "dev"}, "_repo": "o/r",
          "head": {"ref": "data/1-x-r0", "repo": {"full_name": "o/r"}},
          "user": {"login": BOT}, "labels": []}
    pr.update(kw)
    return pr


def test_automerge_ok(team) -> None:
    assert cj.automerge_decision(team, am_pr(), REQ) == (True, "예약")
    assert cj.automerge_decision(team, am_pr(user={"login": "bxbae"}), REQ)[0]


def test_automerge_fail_closed_when_unreadable(team) -> None:
    ok, reason = cj.automerge_decision(team, am_pr(), None)
    assert not ok and "fail-closed" in reason


def test_automerge_waits_for_agent_review_required(team) -> None:
    assert not cj.automerge_decision(team, am_pr(), ["ci", "folder-guard"])[0]


@pytest.mark.parametrize(
    "kw",
    [{"user": {"login": "stranger"}}, {"draft": True}, {"state": "closed"},
     {"base": {"ref": "main"}}, {"head": {"ref": "gen/x", "repo": {"full_name": "o/r"}}},
     {"labels": [{"name": "generated-pr"}]}, {"labels": [{"name": "external"}]},
     {"head": {"ref": "data/x", "repo": {"full_name": "fork/r"}}},
     {"head": {"ref": "data/x", "repo": None}}],
)
def test_automerge_rejects(team, kw) -> None:
    assert not cj.automerge_decision(team, am_pr(**kw), REQ)[0]


def test_required_checks_none_on_api_error(monkeypatch) -> None:
    def boom(*a, **k):
        raise cj.subprocess.CalledProcessError(1, "gh")
    monkeypatch.setattr(cj, "api", boom)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    assert cj.required_checks() is None
    monkeypatch.setattr(cj, "api", lambda *a, **k: {"protection": {}})
    assert cj.required_checks() is None
    monkeypatch.setattr(cj, "api", lambda *a, **k: {
        "protection": {"required_status_checks": {"contexts": ["ci", "agent-review"]}}})
    assert cj.required_checks() == ["ci", "agent-review"]


# ── ruff 대상 · 리뷰 상태 순서 ─────────────────────────────
def test_ruff_targets_only_changed_py_in_dept(team) -> None:
    changes = [("A", "agents/data/src/data_agent/impact.py"), ("M", "agents/data/tests/test_x.py"),
               ("D", "agents/data/old.py"), ("M", "agents/data/README.md"),
               ("M", "agents/finance/x.py"), ("A", "docs/notes/data/n.py")]
    assert cj.ruff_targets(team, "data", changes) == [
        "agents/data/src/data_agent/impact.py", "agents/data/tests/test_x.py"]


def test_review_apply_sets_status_before_comment_and_survives_comment_error(
    team, tmp_path, monkeypatch
) -> None:
    head = "a" * 40
    d = tmp_path / "verdict-finance"
    d.mkdir()
    (d / "verdict.json").write_text(json.dumps({"head_sha": head, "verdict": "approve"}))
    events = []
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(cj, "api", lambda path, *e: {"head": {"sha": head}, "labels": []})
    monkeypatch.setattr(cj, "has_hotfix", lambda t, s: False)
    monkeypatch.setattr(cj, "bot_bodies", lambda n, t: [])
    monkeypatch.setattr(
        cj, "set_status", lambda sha, ctx, st, desc: events.append(("status", ctx, st))
    )

    def failing_comment(n, body):
        events.append(("comment",))
        raise cj.subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr(cj, "comment", failing_comment)
    plan = {"head_sha": head, "reviewers": [{"dept": "finance", "name": "태윤"}]}
    assert cj.review_apply(team, 27, plan, tmp_path) == "success"
    assert events.index(("status", "agent-review", "success")) < events.index(("comment",))


def test_ruff_targets_cli_prints_nothing_when_empty(team, tmp_path, monkeypatch, capsys) -> None:
    # 바뀐 .py가 없으면 빈 줄도 출력하지 않는다
    # (빈 줄이 빈 파일 이름으로 들어가 git diff가 실패했음)
    monkeypatch.setattr(cj.team_mod, "git", lambda *a: "M\0agents/data/README.md\0")
    assert cj.main(["ruff-targets", "--dept", "data", "--base", "origin/dev"]) == 0
    assert capsys.readouterr().out == ""


# ── WIP 제한 ─────────────────────────────────────────────
def bot_pr(branch, number=1, body="", updated="2026-10-20T00:00:00Z", user=BOT, sha="s"):
    return {"number": number, "title": f"t{number}", "body": body, "updated_at": updated,
            "user": {"login": user}, "head": {"ref": branch, "sha": sha}}


def issue(number, labels, user=BOT, updated="2026-10-20T00:00:00Z", **kw):
    return {"number": number, "title": f"i{number}", "updated_at": updated,
            "user": {"login": user}, "labels": [{"name": x} for x in labels], **kw}


def test_wip_check_counts_dept_bot_prs_only(team) -> None:
    prs = [bot_pr("data/1-a-r0"), bot_pr("finance/2-b-r0"), bot_pr("revert/3"),
           bot_pr("data/feature", user="JangJaehyeonn")]
    assert cj.wip_check(team, "data", prs, []) is None  # 봇 data PR 1개뿐
    prs.append(bot_pr("data/4-c-r0"))
    assert "열린 봇 PR 2/2" in cj.wip_check(team, "data", prs, [])
    assert cj.wip_check(team, "finance", prs, []) is None


def test_wip_check_in_progress_issues(team) -> None:
    busy = [issue(1, ["to:data", "in-progress"]), issue(2, ["dept:data", "in-progress"]),
            issue(3, ["to:finance", "in-progress"]),
            issue(4, ["to:data", "in-progress"], pull_request={})]  # PR은 세지 않음
    now = cj.parse_time("2026-10-20T01:00:00Z")
    assert cj.wip_check(team, "data", [], busy, now) is None
    busy.append(issue(5, ["to:data", "in-progress"]))
    assert "진행 중 이슈 3/3" in cj.wip_check(team, "data", [], busy, now)
    # 열린 PR도 없이 2시간 넘게 멈춘 in-progress는 세지 않는다 (라벨만 남은 것)
    later = cj.parse_time("2026-10-20T03:00:00Z")
    assert cj.wip_check(team, "data", [], busy, later) is None
    # 열린 봇 PR이 있는 이슈는 오래돼도 센다
    prs = [bot_pr("finance/1-a-r0", 1, body="<!-- ax-issue:1 -->"),
           bot_pr("finance/2-b-r0", 2, body="<!-- ax-issue:2 -->")]
    busy = [issue(1, ["to:finance", "in-progress"]), issue(2, ["to:finance", "in-progress"]),
            issue(3, ["to:finance", "in-progress"])]
    assert "열린 봇 PR 2/2" in cj.wip_check(team, "finance", prs, busy, later)


def test_wip_resume_picks_priority_then_oldest_per_free_dept(team, monkeypatch) -> None:
    waiting = [issue(9, ["to:data", "wip-wait"]), issue(7, ["to:data", "wip-wait"]),
               issue(8, ["to:data", "wip-wait", "priority"]),
               issue(6, ["to:finance", "wip-wait"])]
    full = [bot_pr("finance/1-a-r0", 1), bot_pr("finance/2-b-r0", 2)]  # finance는 자리 없음

    def fake_api(path, *extra):
        if "labels=wip-wait" in path:
            return waiting
        if "/pulls?" in path:
            return full
        return []

    removed, dispatched = [], []
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(cj, "api", fake_api)
    monkeypatch.setattr(cj, "remove_label", lambda n, lb: removed.append((n, lb)))
    monkeypatch.setattr(cj.team_mod, "dispatch_write",
                        lambda r, t, n, *dm: dispatched.append(n) or f"#{n}")
    waiting.append(issue(5, ["to:data", "wip-wait", "quota"]))  # 막힌 이슈는 건너뜀
    assert cj.wip_resume(team) == ["#8"]
    assert removed == [] and dispatched == [8]  # 라벨은 작성이 시작될 때 뗀다
    dispatched.clear()
    assert cj.wip_resume(team, ["finance"]) == [] and dispatched == []


# ── 턴 한도 ──────────────────────────────────────────────
@pytest.mark.parametrize(
    ("data", "hit"),
    [([{"type": "system"}, {"type": "result", "subtype": "success", "num_turns": 47}], True),
     ([{"type": "result", "subtype": "error_max_turns", "num_turns": 30}], True),
     ([{"type": "result", "subtype": "success", "num_turns": 12}], False),
     ({"type": "result", "subtype": "error_during_execution", "num_turns": 3}, False),
     ([], False)],
)
def test_hit_max_turns(tmp_path, data, hit) -> None:
    f = tmp_path / "exec.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    assert cj.hit_max_turns(f, 30) is hit


def test_hit_max_turns_missing_or_bad_file(tmp_path) -> None:
    assert cj.hit_max_turns(None) is False
    assert cj.hit_max_turns(tmp_path / "none.json") is False
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    assert cj.hit_max_turns(tmp_path / "bad.json") is False


def test_write_finalize_max_turns_marks_needs_split(team, monkeypatch) -> None:
    calls = []
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(cj, "remove_label", lambda n, lb: calls.append(("rm", n, lb)))
    monkeypatch.setattr(cj, "add_labels", lambda n, lbs: calls.append(("add", n, tuple(lbs))))
    monkeypatch.setattr(cj, "comment", lambda n, body: calls.append(("comment", n, body)))

    def no_requeue(*a, **k):
        raise AssertionError("턴 한도는 재대기하지 않음")

    monkeypatch.setattr(cj.team_mod, "requeue", no_requeue)
    assert cj.main(["write-finalize", "--issue", "117", "--reason", "x",
                    "--max-turns", "true"]) == 0
    assert ("rm", 117, "in-progress") in calls and ("add", 117, ("needs-split",)) in calls
    assert any(c[0] == "comment" and "작업 분할 필요" in c[2] for c in calls)


def test_write_finalize_redispatches_first_failure_only(team, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    dispatched = []
    monkeypatch.setattr(cj.team_mod, "dispatch_write",
                        lambda r, t, n, *dm: dispatched.append((n, *dm)))
    monkeypatch.setattr(cj.team_mod, "requeue", lambda *a, **k: "retry-1")
    cj.main(["write-finalize", "--issue", "5", "--reason", "x", "--dept", "data",
             "--mode", "request"])
    monkeypatch.setattr(cj.team_mod, "requeue", lambda *a, **k: "needs-human")
    cj.main(["write-finalize", "--issue", "6", "--reason", "x"])
    assert dispatched == [(5, "data", "request")]


# ── ci 실패 요약 · 재대기 ─────────────────────────────────
CI_LOG = """ci\tUNKNOWN STEP\t2026-10-22T08:55:40.85Z (node:2318) DeprecationWarning: url.parse()
ci\tUNKNOWN STEP\t2026-10-22T08:55:43.24Z ##[group]Run uv run ruff check .
ci\tUNKNOWN STEP\t2026-10-22T08:55:43.24Z \x1b[36;1muv run ruff check .\x1b[0m
ci\tUNKNOWN STEP\t2026-10-22T08:55:43.31Z E501 Line too long (104 > 100)
ci\tUNKNOWN STEP\t2026-10-22T08:55:43.31Z  --> agent-template/src/ax_agent_template/testing.py:1:88
ci\tUNKNOWN STEP\t2026-10-22T08:55:43.31Z Found 1 error.
ci\tUNKNOWN STEP\t2026-10-22T08:55:43.32Z ##[error]Process completed with exit code 1.
ci\tUNKNOWN STEP\t2026-10-22T08:55:44.00Z ```injected```
"""


def test_summarize_ci_log_keeps_actionable_lines() -> None:
    out = cj.summarize_ci_log(CI_LOG)
    assert "E501 Line too long (104 > 100)" in out
    assert "--> agent-template/src/ax_agent_template/testing.py:1:88" in out
    assert "Found 1 error." in out
    assert "Deprecation" not in out and "##[group]" not in out and "\x1b" not in out
    assert "```" not in out  # 코멘트의 코드 블록을 깨지 않게


def test_summarize_ci_log_pytest_failure_and_limits() -> None:
    log = "\n".join(
        [f"ci\tx\t2026-10-23T03:04:56Z E       TypeError: n{i}" for i in range(100)]
        + ["ci\tx\t2026-10-23T03:04:56Z FAILED agents/finance/tests/test_self_test.py::t"])
    out = cj.summarize_ci_log(log)
    assert out.endswith("FAILED agents/finance/tests/test_self_test.py::t")
    assert len(out.splitlines()) <= cj.MAX_HINT_LINES and len(out) <= cj.MAX_HINT_CHARS
    assert cj.summarize_ci_log("")  # 비어도 안내 문구


def ci_env(monkeypatch, prs, red=False):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(cj, "dev_ci_red", lambda: red)
    monkeypatch.setattr(cj, "api", lambda path, *e: prs if "/pulls?" in path else {})
    monkeypatch.setattr(cj, "gh", lambda *a, **k: CI_LOG)
    calls = {"requeue": [], "dispatch": []}

    def fake_requeue(repo, team, issue_no, reason, **kw):
        calls["requeue"].append((issue_no, reason, kw))
        return "retry-1"

    monkeypatch.setattr(cj.team_mod, "requeue", fake_requeue)
    monkeypatch.setattr(cj.team_mod, "dispatch_write",
                        lambda r, t, n, *dm: calls["dispatch"].append((n, *dm)) or "dispatched")
    return calls


def test_ci_failed_requeues_with_hint_and_redispatches(team, monkeypatch) -> None:
    pr = bot_pr("agent-template/140-x-r0", 144,
                body="Closes #140\n<!-- ax-issue:140 -->\n<!-- by:agent-template mode:request -->",
                sha="h1")
    calls = ci_env(monkeypatch, [pr])
    out = cj.ci_failed(team, "99", "h1", "agent-template/140-x-r0", False)
    assert out.startswith("retry-1")
    assert calls["dispatch"] == [(140, "agent-template", "request")]
    issue_no, reason, kw = calls["requeue"][0]
    assert issue_no == 140 and kw == {"pr": 144, "head_sha": "h1"}
    assert "ci 실패 (PR #144)" in reason and "E501" in reason


@pytest.mark.parametrize(
    ("prs", "red", "expect"),
    [([], True, "dev 빨간불"),  # dev가 깨졌으면 PR을 닫지 않음
     ([bot_pr("data/1-x-r0", 1, body="<!-- ax-issue:1 -->", sha="old")], False, "head가 바뀜"),
     ([bot_pr("data/1-x", 1, body="<!-- ax-issue:1 -->", user="JangJaehyeonn", sha="h1")],
      False, "봇 부서 PR이 아님"),
     ([bot_pr("revert/5", 1, body="<!-- ax-issue:1 -->", sha="h1")], False, "봇 부서 PR이 아님"),
     ([bot_pr("data/1-x-r0", 1, body="Closes #1", sha="h1")], False, "연결 이슈 표시가 없음")],
)
def test_ci_failed_skips(team, monkeypatch, prs, red, expect) -> None:
    calls = ci_env(monkeypatch, prs, red)
    assert expect in cj.ci_failed(team, "99", "h1", "data/1-x-r0", False)
    assert calls["requeue"] == []


def test_ci_failed_respects_dev_red_variable(team, monkeypatch) -> None:
    calls = ci_env(monkeypatch, [])
    assert "dev 빨간불" in cj.ci_failed(team, "99", "h1", "data/1-x-r0", True)
    assert calls["requeue"] == []


# ── 매일 정리 ────────────────────────────────────────────
def test_stale_targets(team) -> None:
    now = cj.parse_time("2026-10-23T03:00:00Z")
    old, new = "2026-10-21T02:00:00Z", "2026-10-22T03:00:00Z"
    prs = [bot_pr("data/1-a-r0", 1, updated=old), bot_pr("data/2-b-r0", 2, updated=new),
           bot_pr("revert/3", 3, updated=old), bot_pr("data/x", 4, updated=old, user="bxbae")]
    issues = [issue(10, ["to:support", "request"], updated=old),
              issue(11, ["to:support"], updated=new),
              issue(12, ["report"], updated=old),
              issue(13, ["in-progress", "to:data"], updated=old),
              issue(14, ["to:data"], updated=old, user="bxbae"),
              issue(15, [], updated=old, pull_request={})]
    old_prs, old_issues = cj.stale_targets(team, prs, issues, now)
    assert [p["number"] for p in old_prs] == [1]
    assert [i["number"] for i in old_issues] == [10]


def test_stale_sweep_closes_and_reports(team, monkeypatch) -> None:
    now = cj.parse_time("2026-10-23T03:00:00Z")
    old = "2026-10-20T00:00:00Z"
    prs = [bot_pr("data/7-a-r0", 30, body="<!-- ax-issue:7 -->", updated=old)]
    issues = [issue(10, ["to:support", "request"], updated=old),
              {**issue(20, ["report"]), "title": "[report] 정리 2026-10-22"}]
    writes = []

    def fake_api(path, *extra):
        if extra:
            writes.append((path, extra))
            return {"number": 99}
        if "/pulls?" in path:
            return prs
        if "/issues?" in path:
            return issues
        return []

    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(cj, "api", fake_api)
    monkeypatch.setattr(cj, "gh", lambda *a, **k: "")
    monkeypatch.setattr(cj, "comment", lambda n, b: writes.append((f"comment {n}", b)))
    monkeypatch.setattr(cj, "remove_label", lambda n, lb: writes.append((f"rm {n}", lb)))
    monkeypatch.setattr(cj, "add_labels", lambda n, lbs: writes.append((f"add {n}", tuple(lbs))))
    monkeypatch.setattr(cj, "wip_resume", lambda t: [])
    monkeypatch.setattr(cj, "pr_all_green", lambda sha: False)
    out = cj.stale_sweep(team, now)
    assert out.startswith("PR 1·이슈 1 닫음") and "report #99" in out
    paths = [w[0] for w in writes]
    assert "repos/o/r/pulls/30" in paths  # PR 닫기
    assert ("add 7", ("needs-human",)) in writes and ("rm 7", "in-progress") in writes
    assert ("add 10", ("stale",)) in writes and "repos/o/r/issues/10" in paths
    assert "repos/o/r/issues/20" in paths  # 전날 report 닫기
    new_report = next(w for w in writes if w[0] == "repos/o/r/issues")
    assert "title=[report] 정리 2026-10-23" in new_report[1]
    body = next(x for x in new_report[1] if x.startswith("body="))
    assert "PR #30" in body and "이슈 #10" in body



@pytest.mark.parametrize(
    ("runs", "red"),
    [([{"status": "completed", "conclusion": "success"}], False),
     ([{"status": "completed", "conclusion": "failure"}], True),
     ([{"status": "in_progress", "conclusion": None},
       {"status": "completed", "conclusion": "success"}], True),  # 방금 머지가 깼을 수 있음
     ([{"status": "completed", "conclusion": "cancelled"},
       {"status": "completed", "conclusion": "failure"}], True),
     ([{"status": "completed", "conclusion": "cancelled"},
       {"status": "completed", "conclusion": "success"}], False),
     ([], True)],
)
def test_dev_ci_red(monkeypatch, runs, red) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(cj, "api", lambda *a, **k: {"workflow_runs": runs})
    assert cj.dev_ci_red() is red


def test_check_result_strips_html_comments(team) -> None:
    out = cj.check_result({"title": "feat(data): x", "body": "a <!-- ax-issue:42 --> b <!--x",
                           "summary": "", "requests": []}, team)
    assert "ax-issue" not in out["body"] and "<!--" not in out["body"]


def test_linked_prs_uses_last_bot_marker(team) -> None:
    prs = [bot_pr("data/1-a-r0", 1, body="<!-- ax-issue:42 -->\nCloses #1\n<!-- ax-issue:1 -->")]
    assert cj.linked_prs(prs, 1, team) == [1] and cj.linked_prs(prs, 42, team) == []


def test_stale_skips_waiting_and_escalated_issues(team) -> None:
    now = cj.parse_time("2026-10-23T03:00:00Z")
    old = "2026-10-20T00:00:00Z"
    labels = ["wip-wait", "needs-human", "needs-split", "delegated", "quota", "priority"]
    issues = [issue(n, ["to:data", lb], updated=old) for n, lb in enumerate(labels, 1)]
    assert cj.stale_targets(team, [], issues, now) == ([], [])


@pytest.mark.parametrize(
    ("merged", "closer", "removed"),
    [(None, "JangJaehyeonn", True), ("2026-10-23T00:00:00Z", "JangJaehyeonn", False),
     (None, BOT, False)],
)
def test_release_closed_pr(team, monkeypatch, merged, closer, removed) -> None:
    pr = {**bot_pr("data/7-a-r0", 30, body="<!-- ax-issue:7 -->"), "merged_at": merged}

    def fake_api(path, *extra):
        if path.endswith("/pulls/30"):
            return pr
        return [{"event": "labeled", "actor": {"login": BOT}},
                {"event": "closed", "actor": {"login": closer}}]

    got = []
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(cj, "api", fake_api)
    monkeypatch.setattr(cj, "remove_label", lambda n, lb: got.append((n, lb)))
    cj.release_closed_pr(team, 30)
    assert (got == [(7, "in-progress")]) is removed


def test_stale_sweep_keeps_green_prs_waiting_for_approval(team, monkeypatch) -> None:
    # 준하 리뷰: 체크가 모두 초록이고 code owner 승인만 기다리는 봇 계약 PR은 닫지 않는다
    now = cj.parse_time("2026-10-23T03:00:00Z")
    prs = [bot_pr("contracts/86-x-r0", 40, body="<!-- ax-issue:86 -->",
                  updated="2026-10-20T00:00:00Z", sha="green")]
    writes = []

    def fake_api(path, *extra):
        if extra:
            writes.append((path, extra))
            return {"number": 99}
        return prs if "/pulls?" in path else []

    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(cj, "api", fake_api)
    monkeypatch.setattr(cj, "comment", lambda n, b: writes.append((f"comment {n}", b)))
    monkeypatch.setattr(cj, "wip_resume", lambda t: [])
    monkeypatch.setattr(cj, "pr_all_green", lambda sha: sha == "green")
    out = cj.stale_sweep(team, now)
    assert out.startswith("PR 0·이슈 0 닫음")
    assert "repos/o/r/pulls/40" not in [w[0] for w in writes]
    body = next(x for x in writes[-1][1] if x.startswith("body="))
    assert "승인 대기" in body and "PR #40" in body


@pytest.mark.parametrize(
    ("status", "runs", "green"),
    [("success", [{"status": "completed", "conclusion": "success"},
                  {"status": "completed", "conclusion": "skipped"}], True),
     ("pending", [], False),
     ("success", [{"status": "completed", "conclusion": "failure"}], False),
     ("success", [{"status": "in_progress", "conclusion": None}], False)],
)
def test_pr_all_green(monkeypatch, status, runs, green) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(cj, "api", lambda path, *e: {"state": status} if path.endswith("/status")
                        else {"check_runs": runs})
    assert cj.pr_all_green("s") is green
