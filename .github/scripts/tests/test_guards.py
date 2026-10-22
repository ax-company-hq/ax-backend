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
                            "dev-health": "success"}
    assert plan.comment is None


def test_human_other_folder_fails_with_comment(team) -> None:
    plan = decide(team, pr(), [ch("agents/finance/a.py")])
    assert states(plan)["folder-guard"] == "failure"
    assert "to:finance" in plan.comment and "<!-- guards:" in plan.comment


def test_human_paths_only_skips_review(team) -> None:
    plan = decide(team, pr(branch="ci/x"), [ch(".github/workflows/ci.yml"), ch("docs/a.md")])
    assert states(plan)["agent-review"] == "success"
    assert states(plan)["folder-guard"] == "success"


def test_human_mixed_needs_review(team) -> None:
    plan = decide(team, pr(), [ch("docs/a.md"), ch("agents/data/a.py")])
    assert "agent-review" not in states(plan)


def test_bot_own_dept_ok_and_no_review_skip(team) -> None:
    plan = decide(team, pr(BOT, "data/12-x-r0"), [ch("agents/data/a.py")])
    assert states(plan)["folder-guard"] == "success" and "agent-review" not in states(plan)


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
