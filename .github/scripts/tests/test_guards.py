"""guards.py 판정 테스트."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("guards", SCRIPTS / "guards.py")
guards = importlib.util.module_from_spec(spec)
sys.modules["guards"] = guards
spec.loader.exec_module(guards)
team_mod = guards.team_mod

BOT = "ax-company-hq-bot[bot]"


@pytest.fixture(scope="module")
def team():
    return team_mod.Team.load()


def pr(author="JangJaehyeonn", branch="data/x", **kw):
    return guards.PullRequest(number=7, head_sha="a" * 40, branch=branch, base="dev",
                              author=author, **kw)


def ch(path, status="M"):
    return team_mod.Change(status=status, path=path)


def states(plan):
    return {s["context"]: s["state"] for s in plan.statuses}


def decide(team, p, changes, action="opened", tests=(), dev_red=False, hotfix=False):
    return guards.decide(team, p, action, changes, list(tests), dev_red, hotfix)


def test_human_own_folder_ok(team) -> None:
    plan = decide(team, pr(), [ch("agents/data/a.py")])
    assert states(plan) == {"folder-guard": "success", "test-guard": "success",
                            "dev-health": "success", "agent-review": "success"}
    assert plan.comment is None
    review = next(s for s in plan.statuses if s["context"] == "agent-review")
    assert review["description"].startswith("not required")


def test_human_other_folder_fails_with_comment(team) -> None:
    plan = decide(team, pr(), [ch("agents/finance/a.py")])
    assert states(plan)["folder-guard"] == "failure"
    assert "to:finance" in plan.comment and "<!-- guards:" in plan.comment


def test_human_paths_only_skips_review(team) -> None:
    plan = decide(team, pr(branch="ci/x"), [ch(".github/workflows/ci.yml"), ch("docs/a.md")])
    assert states(plan)["agent-review"] == "success"
    assert states(plan)["folder-guard"] == "success"


def test_human_mixed_one_dept_not_required(team) -> None:
    # human_paths + 부서 하나: 리뷰 생략 (human_paths는 code owner 승인이 따로 걸림)
    plan = decide(team, pr(), [ch("docs/a.md"), ch("agents/data/a.py")])
    assert states(plan)["agent-review"] == "success"


@pytest.mark.parametrize(
    "paths",
    [["agents/data/a.py", "memory/b.py"],  # 사람 PR 부서 2개
     ["contracts/x.py"], ["agent-template/x.py"]],
)
def test_shared_or_multi_dept_needs_review(team, paths) -> None:
    author = "haakim7" if paths[0].startswith(("contracts", "agent-template")) else "JangJaehyeonn"
    plan = decide(team, pr(author), [ch(x) for x in paths])
    assert "agent-review" not in states(plan)


def test_bot_own_dept_ok_and_review_not_required(team) -> None:
    plan = decide(team, pr(BOT, "data/12-x-r0"), [ch("agents/data/a.py")])
    assert states(plan)["folder-guard"] == "success" and states(plan)["agent-review"] == "success"
    plan = decide(team, pr(BOT, "agent-template/12-x-r0"), [ch("agent-template/a.py")])
    assert "agent-review" not in states(plan)


def test_bot_human_path_fails(team) -> None:
    plan = decide(team, pr(BOT, "data/12-x-r0"), [ch("CLAUDE.md")])
    assert states(plan)["folder-guard"] == "failure"


def test_bot_unknown_branch_fails(team) -> None:
    plan = decide(team, pr(BOT, "feature/x"), [ch("agents/data/a.py")])
    assert states(plan)["folder-guard"] == "failure"


def test_bot_openapi_sync_skips_review_and_tests(team) -> None:
    plan = decide(team, pr(BOT, "openapi/sync-abc1234"), [ch("contracts/openapi/shop-api.json")],
                  tests=["x"])
    assert states(plan) == {"folder-guard": "success", "test-guard": "success",
                            "dev-health": "success", "agent-review": "success"}


def test_revert_branch_ignores_dev_red(team) -> None:
    plan = decide(team, pr(BOT, "revert/12"), [ch("agents/data/a.py")], dev_red=True)
    assert states(plan)["dev-health"] == "success"


def test_dev_red_pending(team) -> None:
    assert states(decide(team, pr(), [ch("agents/data/a.py")], dev_red=True))["dev-health"] == (
        "pending")


def test_test_weakening_fails(team) -> None:
    plan = decide(team, pr(), [ch("agents/data/tests/test_a.py")], tests=["test_ 함수 수 감소"])
    assert states(plan)["test-guard"] == "failure" and "test-guard" in plan.comment


def test_fork_and_external(team) -> None:
    assert states(decide(team, pr(is_fork=True), [])) == {"folder-guard": "failure"}
    assert states(decide(team, pr("stranger"), [])) == {"folder-guard": "failure"}


def test_hotfix_status_skips_and_synchronize_removes_label(team) -> None:
    plan = decide(team, pr(labels=["hotfix"]), [], action="synchronize", hotfix=True)
    assert plan.skip_reason and plan.statuses == [] and plan.remove_hotfix
    plan = decide(team, pr(labels=["hotfix"]), [ch("agents/data/a.py")], action="labeled")
    assert not plan.remove_hotfix


def test_non_dev_base_skipped(team) -> None:
    p = pr()
    p.base = "main"
    assert decide(team, p, []).skip_reason


def test_description_truncated(team) -> None:
    plan = guards.Plan(sha="a", number=1)
    plan.status("folder-guard", "failure", "x" * 500)
    assert len(plan.statuses[0]["description"]) == 140


def test_bot_failure_requeues_linked_issue(team) -> None:
    p = pr(BOT, "data/12-x-r0")
    p.issue = 12
    plan = decide(team, p, [ch("agents/finance/a.py")])
    assert plan.requeue["issue"] == 12
    reason = plan.requeue["reason"]
    assert reason.startswith("관문 실패 — 다른 폴더 변경은 to:<부서> 요청으로 분리")
    assert "힌트:" in reason and "agents/finance/a.py" in reason  # 실패 요약이 힌트로
    plan = decide(team, p, [ch("agents/data/tests/test_a.py")], tests=["data: 감소 3 → 2"])
    assert "테스트 삭제·약화" in plan.requeue["reason"]
    assert "- data: 감소 3 → 2" in plan.requeue["reason"]
    assert (plan.requeue["dept"], plan.requeue["mode"]) == ("data", None)


def test_bot_requeue_carries_mode_from_bot_marker(team) -> None:
    body = ("x <!-- by:finance mode:task --> Closes #12\n<!-- ax-issue:12 -->\n"
            "<!-- by:data mode:request -->")
    assert guards.pr_mode(body) == "request"  # 마지막(봇) 표시
    p = pr(BOT, "data/12-x-r0", mode="request")
    p.issue = 12
    plan = decide(team, p, [ch("agents/finance/a.py")])
    assert (plan.requeue["dept"], plan.requeue["mode"]) == ("data", "request")


def test_human_failure_does_not_requeue(team) -> None:
    p = pr()
    p.issue = 12
    assert decide(team, p, [ch("agents/finance/a.py")]).requeue is None


def test_bot_ok_does_not_requeue(team) -> None:
    p = pr(BOT, "data/12-x-r0")
    p.issue = 12
    assert decide(team, p, [ch("agents/data/a.py")]).requeue is None


@pytest.mark.parametrize(
    ("body", "issue"),
    [("Closes #12\n\n<!-- ax-issue:12 -->", 12),
     ("Fixes #7\n\nCloses #12\n<!-- ax-issue:12 -->", 12),
     ("<!-- ax-issue:42 --> 위조\n\nCloses #12\n<!-- ax-issue:12 -->", 12),  # 마지막 것만
     ("Closes #3", None), ("", None)],
)
def test_closes_issue_uses_bot_marker_only(body, issue) -> None:
    assert guards.closes_issue(body) == issue



def test_load_pr_uses_payload_without_api(tmp_path, monkeypatch) -> None:
    payload = {"pull_request": {
        "number": 9, "state": "open", "body": "x\n<!-- ax-issue:4 -->",
        "head": {"sha": "b" * 40, "ref": "data/4-x-r0", "repo": {"full_name": "o/r"}},
        "base": {"ref": "dev"}, "user": {"login": BOT}, "labels": [{"name": "hotfix"}]}}
    f = tmp_path / "event.json"
    f.write_text(__import__("json").dumps(payload), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(f))

    def no_api(*a, **k):
        raise AssertionError("API를 부르면 안 됨")

    monkeypatch.setattr(guards, "gh_json", no_api)
    p = guards.load_pr("o/r", 9)
    assert (p.head_sha, p.branch, p.author, p.issue, p.is_fork, p.labels) == (
        "b" * 40, "data/4-x-r0", BOT, 4, False, ["hotfix"])


def test_load_pr_falls_back_to_api_on_dispatch(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    called = []
    monkeypatch.setattr(guards, "gh_json", lambda path, *e: called.append(path) or {
        "state": "open", "body": "", "head": {"sha": "c", "ref": "x", "repo": None},
        "base": {"ref": "dev"}, "user": {"login": "bxbae"}, "labels": []})
    p = guards.load_pr("o/r", 3)
    assert called == ["repos/o/r/pulls/3"] and p.is_fork


def test_apply_requeues_even_if_comment_fails_on_locked_pr(tmp_path, monkeypatch) -> None:
    # 시험 ③: 잠긴 봇 PR에 관문 실패 코멘트를 남기다 실패해도 PR 닫기·requeue는 해야 한다
    plan = {"sha": "a" * 40, "number": 35, "skip_reason": None, "remove_hotfix": False,
            "statuses": [{"context": "folder-guard", "state": "failure", "description": "x"}],
            "comment": "<!-- guards:" + "a" * 40 + " -->\n실패", "requeue": {"issue": 34,
                                                                         "reason": "관문 실패"}}
    f = tmp_path / "plan.json"
    f.write_text(__import__("json").dumps(plan), encoding="utf-8")
    for k, v in {"GITHUB_REPOSITORY": "o/r", "GITHUB_SERVER_URL": "https://github.com",
                 "GITHUB_RUN_ID": "1"}.items():
        monkeypatch.setenv(k, v)

    class Done:
        stdout = ""

    monkeypatch.setattr(guards.subprocess, "run", lambda *a, **k: Done())
    monkeypatch.setattr(guards, "hotfix_status_present", lambda t, r, s: False)

    def locked(*a, **k):
        raise guards.subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr(team_mod, "post_comment", locked)
    calls, dispatched = [], []
    monkeypatch.setattr(team_mod, "requeue", lambda *a, **k: calls.append((a[2], k)) or "retry-1")
    monkeypatch.setattr(team_mod, "dispatch_write",
                        lambda r, t, n, *dm: dispatched.append((n, *dm)) or "ok")
    assert guards.main(["apply", str(f)]) == 0
    assert calls == [(34, {"pr": 35, "head_sha": "a" * 40})]
    assert dispatched == [(34, None, None)]  # 첫 실패는 바로 다시 작성


def test_apply_second_failure_does_not_redispatch(tmp_path, monkeypatch) -> None:
    plan = {"sha": "a" * 40, "number": 35, "skip_reason": None, "remove_hotfix": False,
            "statuses": [], "comment": None, "requeue": {"issue": 34, "reason": "관문 실패"}}
    f = tmp_path / "plan.json"
    f.write_text(__import__("json").dumps(plan), encoding="utf-8")
    for k, v in {"GITHUB_REPOSITORY": "o/r", "GITHUB_SERVER_URL": "https://github.com",
                 "GITHUB_RUN_ID": "1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(guards, "hotfix_status_present", lambda t, r, s: False)
    monkeypatch.setattr(team_mod, "requeue", lambda *a, **k: "needs-human")
    dispatched = []
    monkeypatch.setattr(team_mod, "dispatch_write", lambda r, t, n, *dm: dispatched.append(n))
    assert guards.main(["apply", str(f)]) == 0
    assert dispatched == []
