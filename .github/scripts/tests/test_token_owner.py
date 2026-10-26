"""token_owner(실행 토큰·cap 주인)와 wip_overrides (PR-A). 값이 없으면 기존 동작 그대로여야 한다."""

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name: str, file: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


team_mod = _load("team", "team.py")
cj = _load("claude_jobs", "claude_jobs.py")
Team = team_mod.Team
BOT = "ax-company-hq-bot[bot]"


@pytest.fixture
def base() -> dict:
    return copy.deepcopy(Team.load().data)


def with_token_owner(base: dict, dept: str, login: str) -> Team:
    base["depts"][dept]["token_owner"] = login
    return Team(base)


def test_token_owner_falls_back_to_first_owner(base: dict) -> None:
    team = Team(base)
    for dept in team.depts:
        assert team.token_owner(dept) == team.owners(dept)[0]


def test_token_owner_overrides_token_and_cap_owner_only(base: dict) -> None:
    team = with_token_owner(base, "orchestrator", "JangJaehyeonn")
    assert team.token_owner("orchestrator") == "JangJaehyeonn"
    assert team.owner_token("orchestrator") == "CLAUDE_TOKEN_JAEHYEON"
    assert team.owners("orchestrator") == ["haakim7"]  # 담당(리뷰어 배정)은 그대로
    assert "orchestrator" in team_mod.token_depts_of(team, "JangJaehyeonn")
    assert "orchestrator" not in team_mod.token_depts_of(team, "haakim7")
    assert team.depts_of("haakim7").count("orchestrator") == 1


def test_token_owner_must_be_a_member(base: dict) -> None:
    base["depts"]["orchestrator"]["token_owner"] = "stranger"
    with pytest.raises(team_mod.TeamError):
        Team(base)


def test_cap_usage_follows_token_owner(base: dict) -> None:
    runs = [("r", 1, "claude:orchestrator:task #5", "success"),
            ("r", 2, "claude-request to:orchestrator by 2taeyoon", "success")]
    assert team_mod.usage_by_owner(Team(base), runs) == {"haakim7": 2}
    team = with_token_owner(base, "orchestrator", "JangJaehyeonn")
    assert team_mod.usage_by_owner(team, runs) == {"JangJaehyeonn": 2}


def test_review_author_person_uses_token_owner_for_bot_pr(base: dict) -> None:
    assert cj.review_author_person(Team(base), BOT, "orchestrator/1-x-r0") == "haakim7"
    team = with_token_owner(base, "orchestrator", "JangJaehyeonn")
    assert cj.review_author_person(team, BOT, "orchestrator/1-x-r0") == "JangJaehyeonn"
    assert cj.review_author_person(team, "haakim7", "orchestrator/x") == "haakim7"  # 사람 PR


def test_reviewer_assignment_still_uses_owners(base: dict) -> None:
    team = with_token_owner(base, "data", "haakim7")
    assert team.owners("data") == ["JangJaehyeonn"]
    assert team.reviewers_for(["data"], "JangJaehyeonn") == ["finance"]


def test_wip_limit_default_and_override(base: dict) -> None:
    team = Team(base)
    assert team.wip_overrides == {}
    assert team.wip_limit("orchestrator", "open_bot_prs") == 2
    assert team.wip_limit("orchestrator", "in_progress_issues") == 3
    base["wip_overrides"] = {"orchestrator": {"open_bot_prs": 3}}
    team = Team(base)
    assert team.wip_limit("orchestrator", "open_bot_prs") == 3
    assert team.wip_limit("orchestrator", "in_progress_issues") == 3  # 적지 않은 키는 기본값
    assert team.wip_limit("data", "open_bot_prs") == 2  # 다른 부서는 그대로


@pytest.mark.parametrize("bad", [{"nowhere": {"open_bot_prs": 3}},
                                 {"orchestrator": {"open_prs": 3}},
                                 {"orchestrator": {"open_bot_prs": -1}},
                                 {"orchestrator": {"open_bot_prs": "3"}}])
def test_wip_overrides_validated(base: dict, bad: dict) -> None:
    base["wip_overrides"] = bad
    with pytest.raises(team_mod.TeamError):
        Team(base)


def pr(branch: str, number: int) -> dict:
    return {"number": number, "user": {"login": BOT}, "head": {"ref": branch},
            "body": "", "labels": []}


def test_wip_check_uses_override(base: dict) -> None:
    prs = [pr(f"orchestrator/{n}-x-r0", n) for n in (1, 2)]
    assert "열린 봇 PR 2/2" in cj.wip_check(Team(base), "orchestrator", prs, [])
    base["wip_overrides"] = {"orchestrator": {"open_bot_prs": 3}}
    team = Team(base)
    assert cj.wip_check(team, "orchestrator", prs, []) is None
    prs.append(pr("orchestrator/3-x-r0", 3))
    assert "열린 봇 PR 3/3" in cj.wip_check(team, "orchestrator", prs, [])
    other = [pr(f"data/{n}-x-r0", n) for n in (1, 2)]
    assert "열린 봇 PR 2/2" in cj.wip_check(team, "data", other, [])
