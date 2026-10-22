"""team.py 단위 테스트 (docs/automation-spec.md 완료 기준)."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "team.py"
spec = importlib.util.spec_from_file_location("team", SCRIPT)
team_mod = importlib.util.module_from_spec(spec)
sys.modules["team"] = team_mod
spec.loader.exec_module(team_mod)

Change = team_mod.Change
Team = team_mod.Team


@pytest.fixture(scope="module")
def team() -> Team:
    return Team.load()


def owner(team: Team, dept: str) -> str:
    return team.owners(dept)[0]


def ch(path: str, status: str = "M", mode: str = "100644") -> Change:
    return Change(status=status, path=path, new_mode=mode)


# ── 설정 ─────────────────────────────────────────────────
def test_team_json_in_sync(team: Team) -> None:
    assert team_mod.TEAM_JSON.read_text(encoding="utf-8") == team.to_json_text()


def test_every_dept_has_reviewers_and_owner_token(team: Team) -> None:
    for dept in team.depts:
        assert team.reviewers[dept], dept
        assert team.owner_token(dept).startswith("CLAUDE_TOKEN_")


def test_invalid_config_rejected(team: Team) -> None:
    data = dict(team.data)
    data["depts"] = {**team.depts, "x": {"folder": "x", "owners": ["nobody"]}}
    with pytest.raises(team_mod.TeamError):
        Team(data)


# ── 사람·부서 ─────────────────────────────────────────────
def test_membership(team: Team) -> None:
    assert team.is_member("JangJaehyeonn")
    assert not team.is_member("ax-company-hq-bot[bot]")
    assert team.is_trusted("ax-company-hq-bot[bot]")
    assert not team.is_trusted("stranger")


@pytest.mark.parametrize(
    ("branch", "dept"),
    [
        ("data/impact-analysis", "data"),
        ("data/12-impact-analysis-r0", "data"),
        ("agent-template/execute-wrapper", "agent-template"),
        ("infra-dejafix/rollback", "dejafix"),  # 폴더 이름 별칭
        ("dejafix/rollback", "dejafix"),
        ("infra/compose", "infra"),
        ("ci/automation-stage-1", None),
        ("docs/typo", None),
        ("main", None),
    ],
)
def test_dept_of_branch(team: Team, branch: str, dept: str | None) -> None:
    assert team.dept_of_branch(branch) == dept


def test_depts_of(team: Team) -> None:
    assert set(team.depts_of("JangJaehyeonn")) == {"data", "memory"}
    assert set(team.depts_of("haakim7")) == {
        "hr", "orchestrator", "agent-template", "infra", "contracts"
    }
    assert team.depts_of("stranger") == []


def test_owner_token(team: Team) -> None:
    assert team.owner_token("contracts") == "CLAUDE_TOKEN_JUNHA"  # 첫 번째 owner
    assert team.owner_token("dejafix") == "CLAUDE_TOKEN_YEJIN"


# ── 리뷰어 배정 ───────────────────────────────────────────
# docs/automation-spec.md "리뷰어 배정" 표 그대로 (작성자 = 그 부서 담당)
REVIEW_TABLE = {
    "data": ["finance"],
    "memory": ["support"],
    "finance": ["data"],
    "safety": ["agent-template"],
    "hr": ["memory"],
    "orchestrator": ["dejafix"],
    "agent-template": ["finance", "planning"],
    "infra": ["finance", "planning"],
    "planning": ["orchestrator"],
    "dejafix": ["orchestrator"],
    "support": ["marketing"],
    "dashboard-api": ["safety"],
    "marketing": ["support"],
    "shop-api": ["data"],
}


@pytest.mark.parametrize(("dept", "expected"), sorted(REVIEW_TABLE.items()))
def test_review_table_rows(team: Team, dept: str, expected: list[str]) -> None:
    assert team.reviewers_for([dept], owner(team, dept)) == expected


def test_contracts_reviewers_by_third_person(team: Team) -> None:
    # 작성자가 두 리뷰 부서 담당이 아니면 표 그대로
    assert team.reviewers_for(["contracts"], "NYejin") == ["agent-template", "safety"]


def test_contracts_author_junha_replaced_by_data(team: Team) -> None:
    assert team.reviewers_for(["contracts"], "haakim7") == ["data", "safety"]


def test_contracts_author_taeyoon_replaced_by_data(team: Team) -> None:
    assert team.reviewers_for(["contracts"], "2taeyoon") == ["agent-template", "data"]


def test_same_person_replaced_by_fallback(team: Team) -> None:
    # memory 리뷰어 support(병환). 병환이 memory를 바꾸면(사람 PR 가정) fallback 첫 부서
    assert team.reviewers_for(["memory"], "bxbae") == ["orchestrator"]
    assert team.reviewers_for(["hr"], "JangJaehyeonn") == ["orchestrator"]


def test_fallback_skips_author_and_existing_reviewers(team: Team) -> None:
    # 준하가 safety를 바꾸면(가정) agent-template(준하) 자리 → orchestrator(준하) 건너뜀 → data
    assert team.reviewers_for(["safety"], "haakim7") == ["data"]


def test_human_multi_dept_union(team: Team) -> None:
    # 재현이 data + memory를 함께 바꾸면 두 리뷰 부서 모두
    assert team.reviewers_for(["data", "memory"], "JangJaehyeonn") == ["finance", "support"]
    # agent-template + infra는 같은 리뷰 부서 → 중복 없음
    assert team.reviewers_for(["agent-template", "infra"], "haakim7") == ["finance", "planning"]


def test_empty_reviewers(team: Team) -> None:
    assert team.reviewers_for([], "JangJaehyeonn") == []


def test_arbiter_is_third_dept(team: Team) -> None:
    arb = team.arbiter_for("data", ["finance"])
    assert arb not in {"data", "finance"}
    assert not set(team.owners(arb)) & {"JangJaehyeonn", "2taeyoon"}
    assert arb == "orchestrator"
    assert team.arbiter_for("orchestrator", ["dejafix"]) == "data"


# ── check-diff ───────────────────────────────────────────
def test_bot_own_dept_ok(team: Team) -> None:
    assert team.check_diff([ch("agents/data/src/data_agent/x.py")], ["data"], "bot") == []


def test_notes_exception(team: Team) -> None:
    assert team.check_diff([ch("docs/notes/data/memo.md", "A")], ["data"], "bot") == []
    assert team.check_diff([ch("docs/notes/finance/memo.md", "A")], ["data"], "bot")


def test_other_dept_rejected(team: Team) -> None:
    v = team.check_diff([ch("agents/finance/x.py")], ["data"], "bot")
    assert len(v) == 1 and "to:finance" in v[0]


@pytest.mark.parametrize(
    "path", ["CLAUDE.md", ".github/workflows/ci.yml", "pyproject.toml", "docs/automation-spec.md",
             ".env.example", "scripts/safe-test.sh"],
)
def test_human_paths_bot_rejected_human_allowed(team: Team, path: str) -> None:
    assert team.check_diff([ch(path)], ["data"], "bot")
    assert team.check_diff([ch(path)], ["data"], "human") == []


def test_uv_lock_requires_own_pyproject(team: Team) -> None:
    lock = ch("uv.lock")
    assert team.check_diff([lock], ["data"], "bot")
    assert team.check_diff([lock, ch("agents/data/pyproject.toml")], ["data"], "bot") == []
    # 다른 부서 pyproject로는 허용 안 됨 (그 파일 자체도 위반)
    assert team.check_diff([lock, ch("memory/pyproject.toml")], ["data"], "bot")
    # 사람 PR도 같은 규칙
    assert team.check_diff([lock], ["data"], "human")


def test_symlink_and_submodule_rejected(team: Team) -> None:
    assert team.check_diff([ch("agents/data/link", "A", "120000")], ["data"], "bot")
    assert team.check_diff([ch("agents/data/sub", "A", "160000")], ["data"], "human")
    # 심볼릭 링크 삭제는 허용
    assert team.check_diff([ch("agents/data/link", "D", "000000")], ["data"], "bot") == []


@pytest.mark.parametrize(
    "path", ["agents/data/CLAUDE.md", "agents/data/src/.claude/settings.json",
             "memory/.mcp.json", "agents/data/deep/CLAUDE.local.md"],
)
def test_forbidden_names_in_dept(team: Team, path: str) -> None:
    assert team.check_diff([ch(path, "A")], ["data", "memory"], "human")


def test_human_multi_dept_ok_but_not_others(team: Team) -> None:
    changes = [ch("agents/data/a.py"), ch("memory/b.py"), ch("docs/README.md")]
    assert team.check_diff(changes, team.depts_of("JangJaehyeonn"), "human") == []
    assert team.check_diff([ch("safety/c.py")], team.depts_of("JangJaehyeonn"), "human")


def test_bot_branches(team: Team) -> None:
    ok = [ch("contracts/openapi/shop-api.json")]
    assert team.check_diff(ok, [], "bot", "openapi/sync-abc1234") == []
    bad = [ch("contracts/src/ax_contracts/x.py")]
    assert team.check_diff(bad, [], "bot", "openapi/sync-abc1234")
    assert team.check_diff([ch("agents/data/x.py")], [], "bot", "revert/12") == []
    assert team.check_diff([ch(".github/workflows/ci.yml")], [], "bot", "revert/12")
    # 사람이 같은 이름의 브랜치를 써도 봇 규칙은 적용되지 않는다 (contracts는 그 사람 부서가 아님)
    assert team.check_diff(bad, [], "human", "openapi/sync-abc1234")


def test_bad_author_kind(team: Team) -> None:
    with pytest.raises(team_mod.TeamError):
        team.check_diff([], [], "robot")


def test_classify(team: Team) -> None:
    c = team.classify(["agents/data/a.py", "uv.lock", "CLAUDE.md", "docs/notes/hr/x.md"])
    assert c.depts == {"data": ["agents/data/a.py"], "hr": ["docs/notes/hr/x.md"]}
    assert c.shared == ["uv.lock"] and c.human == ["CLAUDE.md"]


# ── test-guard ───────────────────────────────────────────
BASE_TEST = """
def test_a():
    assert 1 == 1
    assert 2 == 2

def test_b():
    assert True

class TestC:
    def test_c(self):
        self.assertEqual(1, 1)
"""


def guard(changes, base_src, head_src):
    return team_mod.find_test_weakening(changes, lambda p: base_src, lambda p: head_src)


def test_guard_unchanged_ok() -> None:
    assert guard([ch("agents/data/tests/test_x.py")], BASE_TEST, BASE_TEST) == []


def test_guard_added_test_ok() -> None:
    head = BASE_TEST + "\ndef test_d():\n    assert 1\n"
    assert guard([ch("agents/data/tests/test_x.py")], BASE_TEST, head) == []


def test_guard_deleted_file() -> None:
    v = guard([ch("agents/data/tests/test_x.py", "D")], BASE_TEST, "")
    assert any("삭제" in x for x in v) and any("감소" in x for x in v)


def test_guard_fewer_tests() -> None:
    head = BASE_TEST.replace("def test_b():\n    assert True\n", "")
    assert any("test_ 함수 수 감소" in x for x in guard(
        [ch("agents/data/tests/test_x.py")], BASE_TEST, head))


def test_guard_assert_halved() -> None:
    head = BASE_TEST.replace("    assert 2 == 2\n", "    pass\n").replace(
        "    assert True\n", "    pass\n")
    v = guard([ch("agents/data/tests/test_x.py")], BASE_TEST, head)
    assert any("50%" in x for x in v)


def test_guard_ignores_non_test_files() -> None:
    assert guard([ch("agents/data/src/data_agent/x.py", "D")], "", "") == []


def test_guard_syntax_error() -> None:
    assert guard([ch("agents/data/tests/test_x.py")], BASE_TEST, "def test_(:\n")


# ── git 통합 (임시 레포) ──────────────────────────────────
def run(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    run(tmp_path, "init", "-q", "-b", "dev")
    run(tmp_path, "config", "user.email", "t@example.com")
    run(tmp_path, "config", "user.name", "t")
    run(tmp_path, "config", "core.symlinks", "true")
    (tmp_path / "agents/data/tests").mkdir(parents=True)
    (tmp_path / "agents/data/tests/test_x.py").write_text(BASE_TEST)
    (tmp_path / "README.md").write_text("x")
    run(tmp_path, "add", "-A")
    run(tmp_path, "commit", "-q", "-m", "base")
    run(tmp_path, "switch", "-q", "-c", "data/feature")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_cli_check_diff_and_reviewers(repo: Path, capsys) -> None:
    (repo / "agents/data/new.py").write_text("x = 1\n")
    (repo / "agents/finance").mkdir(parents=True)
    (repo / "agents/finance/y.py").write_text("y = 1\n")
    run(repo, "add", "-A")
    run(repo, "commit", "-q", "-m", "change")
    rc = team_mod.main(["check-diff", "dev", "HEAD", "--depts", "data", "--author-kind", "bot"])
    out = capsys.readouterr().out
    assert rc == 1 and "agents/finance/y.py" in out and "agents/data/new.py" not in out
    rc = team_mod.main(["reviewers-for", "dev", "HEAD", "JangJaehyeonn"])
    # data → finance, finance → data(재현 본인) 대신 fallback orchestrator
    assert rc == 0 and capsys.readouterr().out.strip() == "finance,orchestrator"


def test_cli_rename_counts_both_paths(repo: Path, capsys) -> None:
    run(repo, "mv", "agents/data/tests/test_x.py", "agents/data/test_moved.py")
    run(repo, "commit", "-q", "-m", "move")
    rc = team_mod.main(["test-guard", "dev", "HEAD"])
    assert rc == 1 and "삭제" in capsys.readouterr().out


def test_cli_symlink_detected(repo: Path, capsys) -> None:
    link = repo / "agents/data/link"
    try:
        link.symlink_to("../../README.md")
    except OSError:
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없음")
    run(repo, "add", "-A")
    run(repo, "commit", "-q", "-m", "link")
    rc = team_mod.main(["check-diff", "dev", "HEAD", "--depts", "data", "--author-kind", "human"])
    assert rc == 1 and "심볼릭" in capsys.readouterr().out


def test_cli_simple_commands(capsys) -> None:
    assert team_mod.main(["is-trusted", "ax-company-hq-bot[bot]"]) == 0
    assert team_mod.main(["is-member", "stranger"]) == 1
    assert team_mod.main(["dept-of-branch", "infra-dejafix/x"]) == 0
    assert team_mod.main(["owner-token", "hr"]) == 0
    assert team_mod.main(["export-json", "--check"]) == 0
    out = capsys.readouterr().out.split()
    assert out == ["true", "false", "dejafix", "CLAUDE_TOKEN_JUNHA"]


# ── 보안 검토 반영 ────────────────────────────────────────
@pytest.mark.parametrize("path", ["docs/notes/foo/x.md", "docs/notes/Data/x.md", "docs/notes/x.md"])
def test_unknown_notes_dir_rejected(team: Team, path: str) -> None:
    # CODEOWNERS는 /docs/notes/*/를 owner 없이 열어두므로 부서 id가 아닌 폴더는 막는다
    assert team.check_diff([ch(path, "A")], ["data"], "human")
    assert team.check_diff([ch(path, "A")], ["data"], "bot")


def test_known_notes_dir_and_alias_ok(team: Team) -> None:
    assert team.check_diff([ch("docs/notes/dejafix/x.md", "A")], ["dejafix"], "bot") == []
    assert team.check_diff([ch("docs/notes/infra-dejafix/x.md", "A")], ["dejafix"], "bot") == []


@pytest.mark.parametrize(
    "path", ["agents/data/claude.md", "agents/data/.Claude/settings.json",
             "agents/data/CLAUDE.md. ", "agents/data/sub/.gitattributes", "memory/.GITMODULES"],
)
def test_forbidden_names_case_insensitive(team: Team, path: str) -> None:
    assert team.check_diff([ch(path, "A")], ["data", "memory"], "human")


@pytest.mark.parametrize(
    "path", ["CLAUDE.md", "scripts/safe-test.sh", "docs/a.md", "pyproject.toml"]
)
def test_revert_branch_rejects_human_paths(team: Team, path: str) -> None:
    assert team.check_diff([ch(path)], [], "bot", "revert/12")


def test_revert_branch_allows_dept_and_lock(team: Team) -> None:
    changes = [ch("agents/finance/a.py"), ch("uv.lock")]
    assert team.check_diff(changes, [], "bot", "revert/12") == []


def test_guard_recursion_error_reported() -> None:
    deep = "def test_x():\n    assert " + "(" * 300 + "1" + ")" * 300 + "\n"
    v = guard([ch("agents/data/tests/test_x.py")], BASE_TEST, deep)
    assert v == [] or any("파싱 실패" in x for x in v)


# ── cap-ok · requeue ─────────────────────────────────────
def test_token_depts_of(team: Team) -> None:
    assert team_mod.token_depts_of(team, "JangJaehyeonn") == {"data", "memory"}
    # contracts의 첫 owner는 준하 → 태윤 토큰으로는 contracts 잡을 세지 않는다
    assert "contracts" in team_mod.token_depts_of(team, "haakim7")
    assert "contracts" not in team_mod.token_depts_of(team, "2taeyoon")
    assert "shop-web" in team_mod.token_depts_of(team, "areatmadm")


def test_count_claude_jobs() -> None:
    jobs = [
        ("claude:data:task", "success"),
        ("claude:data:review", "failure"),
        ("claude-review / claude:memory:review", "success"),  # 재사용 워크플로 형태
        ("claude:data:task", "skipped"),  # 실행 안 됨
        ("claude:finance:review", "success"),  # 다른 사람 부서
        ("prep", "success"),
        ("apply", None),
        ("xclaude:data:task", "success"),  # 접두 일치 아님
    ]
    assert team_mod.count_claude_jobs(jobs, {"data", "memory"}) == 3


@pytest.mark.parametrize(
    ("labels", "add", "remove"),
    [
        ([], ["retry-1"], ["in-progress"]),
        (["in-progress", "task"], ["retry-1"], ["in-progress"]),
        (["retry-1"], ["retry-2"], ["in-progress", "retry-1"]),
        (["retry-2", "in-progress"], ["retry-3"], ["in-progress", "retry-2"]),
        (["retry-3"], ["blocked"], ["in-progress", "retry-3"]),
    ],
)
def test_next_retry_labels(labels, add, remove) -> None:
    assert team_mod.next_retry_labels(labels) == (add, remove)


def test_requeue_key() -> None:
    assert team_mod.requeue_key(5, "abc", 3, "99") == "pr:5:abc"
    assert team_mod.requeue_key(None, None, 3, "99") == "issue:3:99"


# ── 잠긴 이슈 코멘트 · gh 오류 로그 ───────────────────────
def test_post_comment_unlocks_and_relocks(monkeypatch) -> None:
    calls = []

    def fake(path, *extra):
        calls.append((path, extra))
        if extra == ():
            return {"locked": True, "active_lock_reason": "resolved"}
        return None

    monkeypatch.setattr(team_mod, "gh_api", fake)
    team_mod.post_comment("o/r", 5, "hi")
    seq = [(p.split("/issues/5")[1] or "/", e[:2]) for p, e in calls]
    assert seq == [("/", ()), ("/lock", ("-X", "DELETE")), ("/comments", ("-X", "POST")),
                   ("/lock", ("-X", "PUT"))]
    assert calls[-1][1][-1] == "lock_reason=resolved"


def test_post_comment_relocks_even_if_comment_fails(monkeypatch) -> None:
    calls = []

    def fake(path, *extra):
        calls.append(extra[:2])
        if extra == ():
            return {"locked": True, "active_lock_reason": "off-topic"}
        if path.endswith("/comments"):
            raise team_mod.subprocess.CalledProcessError(1, "gh")
        return None

    monkeypatch.setattr(team_mod, "gh_api", fake)
    with pytest.raises(team_mod.subprocess.CalledProcessError):
        team_mod.post_comment("o/r", 5, "hi")
    assert calls[-1] == ("-X", "PUT")


def test_post_comment_unlocked_issue_no_lock_calls(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(team_mod, "gh_api",
                        lambda p, *e: calls.append(p) or ({"locked": False} if not e else None))
    team_mod.post_comment("o/r", 5, "hi")
    assert not any(p.endswith("/lock") for p in calls)


def test_gh_api_logs_github_error_without_body(monkeypatch, capsys) -> None:
    def boom(cmd, **kw):
        raise team_mod.subprocess.CalledProcessError(
            1, cmd, stderr="gh: Unable to create comment because issue is locked. (HTTP 403)\n")

    monkeypatch.setattr(team_mod.subprocess, "run", boom)
    with pytest.raises(team_mod.subprocess.CalledProcessError):
        team_mod.gh_api("repos/o/r/issues/5/comments", "-X", "POST", "-f", "body=SECRET-ish text")
    err = capsys.readouterr().err
    assert "POST repos/o/r/issues/5/comments" in err and "HTTP 403" in err and "locked" in err
    assert "SECRET-ish" not in err
