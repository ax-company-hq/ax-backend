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
