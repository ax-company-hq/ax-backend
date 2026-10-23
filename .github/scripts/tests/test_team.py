"""team.py 단위 테스트 (docs/automation-spec.md 완료 기준)."""

import importlib.util
import json
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
    assert set(team.depts_of("JangJaehyeonn")) == {"data", "memory", "contracts"}
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
    assert "테스트 삭제 agents/data/tests/test_x.py::TestC.test_c" in v
    assert any("assert 감소 3 → 0" in x for x in v)  # assert True는 세지 않음


def test_guard_fewer_tests() -> None:
    head = BASE_TEST.replace("def test_b():\n    assert True\n", "")
    assert guard([ch("agents/data/tests/test_x.py")], BASE_TEST, head) == [
        "테스트 삭제 agents/data/tests/test_x.py::test_b"]


def test_guard_assert_halved() -> None:
    head = BASE_TEST.replace("    assert 2 == 2\n", "    pass\n").replace(
        "    assert True\n", "    pass\n")
    v = guard([ch("agents/data/tests/test_x.py")], BASE_TEST, head)
    assert v == ["테스트 함수 안 assert 감소 3 → 2 (부서 합계)"]


def test_guard_rejects_swapping_tests_for_trivial_ones() -> None:
    # 리뷰 지적: 테스트를 지우고 같은 수의 빈 테스트로 갈아치우면 합계가 같아도 실패해야 한다
    head = "def test_n1():\n    assert True\n\ndef test_n2():\n    assert 1\n\n" \
           "def test_n3():\n    assert 1\n"
    v = guard([ch("agents/data/tests/test_x.py", "D"), ch("agents/data/tests/test_y.py", "A")],
              BASE_TEST, head)
    assert any("테스트 삭제" in x for x in v) and any("assert 감소" in x for x in v)


def test_guard_trivial_asserts_do_not_count() -> None:
    head = BASE_TEST.replace("    assert 2 == 2\n", "    assert True\n")
    assert guard([ch("agents/data/tests/test_x.py")], BASE_TEST, head) == [
        "테스트 함수 안 assert 감소 3 → 2 (부서 합계)"]


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
    # tests/ 밖으로 옮기면 테스트 파일이 아니게 됨 → data 합계 감소
    assert rc == 1 and "data: 테스트 삭제 agents/data/tests/test_x.py::test_a" in (
        capsys.readouterr().out)


def test_cli_split_test_file_ok(repo: Path, capsys) -> None:
    # 한 파일을 두 파일로 나눔 (test_gate.py 분할 같은 경우): 부서 합계가 같으면 통과
    (repo / "agents/data/tests/test_x.py").write_text(
        "def test_a():\n    assert 1 == 1\n    assert 2 == 2\n")
    (repo / "agents/data/tests/test_y.py").write_text(
        "def test_b():\n    assert True\n\n\nclass TestC:\n    def test_c(self):\n"
        "        self.assertEqual(1, 1)\n")
    run(repo, "add", "-A")
    run(repo, "commit", "-q", "-m", "split")
    assert team_mod.main(["test-guard", "dev", "HEAD"]) == 0


def test_cli_review_required(repo: Path, capsys) -> None:
    (repo / "agents/data/new.py").write_text("x = 1\n")
    run(repo, "add", "-A")
    run(repo, "commit", "-q", "-m", "one dept")
    assert team_mod.main(["review-required", "dev", "HEAD"]) == 1
    assert capsys.readouterr().out.strip() == "false"
    (repo / "contracts").mkdir()
    (repo / "contracts/x.py").write_text("x = 1\n")
    run(repo, "add", "-A")
    run(repo, "commit", "-q", "-m", "contracts")
    assert team_mod.main(["review-required", "dev", "HEAD"]) == 0


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


def cap_run(title: str, conclusion: str | None, run_id: int = 1) -> tuple:
    return ("ax-company-hq/ax-backend", run_id, title, conclusion)


def test_usage_by_owner_from_run_titles(team: Team) -> None:
    runs = [
        cap_run("claude:data:task #12", "success"),  # 수동 작성 → 재현
        cap_run("claude-request to:data by JangJaehyeonn", "success"),  # 사람 요청 → 재현
        cap_run("claude-request to:memory by bxbae", None),  # 실행 중 → 재현
        cap_run("claude-request to:finance by ax-company-hq-bot[bot]", "success"),  # 봇 요청 → 0
        cap_run("claude-request to:data by JangJaehyeonn", "skipped"),  # 스위치 off → 0
        # 봇 PR data → 리뷰 finance → 태윤
        cap_run("claude-review data/24-x-r0 by ax-company-hq-bot[bot]", "success"),
        cap_run("claude-review agent-template/x by haakim7", "success"),  # finance+planning
        cap_run("claude-review ci/automation by JangJaehyeonn", "success"),  # 사람 경로 → 생략
        cap_run("claude-review dispatch #5 by JangJaehyeonn", "success"),  # 수동 리뷰 → 알 수 없음
        cap_run("feat(data): 옛 형식 제목", "success"),  # 바꾸기 전 run → 0
    ]
    assert team_mod.usage_by_owner(team, runs) == {
        "JangJaehyeonn": 3, "2taeyoon": 2, "NYejin": 1}


def test_failed_runs_counted_only_if_claude_ran(team: Team) -> None:
    runs = [
        cap_run("claude-request to:data by JangJaehyeonn", "failure", 10),  # 설치에서 실패 → 0
        cap_run("claude-review data/1-x-r0 by ax-company-hq-bot[bot]", "failure", 11),  # Claude 돎
        # 판정 결과가 없으면 제목대로 (보수적)
        cap_run("claude-request to:memory by JangJaehyeonn", "cancelled", 12),
    ]
    ran = {"10": [], "11": ["finance"]}
    assert team_mod.usage_by_owner(team, runs, ran) == {"2taeyoon": 1, "JangJaehyeonn": 1}


def _job(name: str, steps: list[tuple[str, str | None]]) -> dict:
    return {"name": name, "steps": [{"name": n, "conclusion": c} for n, c in steps]}


@pytest.mark.parametrize(
    ("job", "dept"),
    [
        # 새 형식: 설치 단계에서 실패 → Claude 단계 skipped → 세지 않음
        (_job("write / claude:data:request", [("Claude Code 설치 (npm, 버전 고정)", "failure"),
                                              ("Claude", "skipped")]), None),
        # 새 형식: Claude가 돌다 실패 → 셈
        (_job("claude:finance:review", [("Claude Code 설치 (npm, 버전 고정)", "success"),
                                        ("Claude (리뷰)", "failure")]), "finance"),
        (_job("claude:finance:review", [("Claude Code 설치 (npm)", "success"),
                                        ("Claude (리뷰)", "success")]), "finance"),
        # 옛 형식(#115 전): 설치가 Claude 단계 안 → 그 단계 실패는 설치 429로 보고 세지 않음
        (_job("claude:data:review", [("Claude (리뷰)", "failure")]), None),
        (_job("claude:data:review", [("Claude (리뷰)", "success")]), "data"),
        # cap이 막아 Claude 단계가 아예 없음 / 다른 잡
        (_job("claude:data:review", [("도구 · cap", "success"),
                                     ("Claude (리뷰)", "skipped")]), None),
        (_job("prep", [("검증 (읽기만)", "failure")]), None),
    ],
)
def test_job_claude_dept(job: dict, dept: str | None) -> None:
    assert team_mod.job_claude_dept(job) == dept


def test_resolve_failed_runs_uses_cache(monkeypatch) -> None:
    calls = []

    def fake(path, *extra):
        calls.append(path)
        return {"jobs": [_job("claude:data:review", [("Claude Code 설치 (npm)", "success"),
                                                      ("Claude (리뷰)", "failure")])]}

    monkeypatch.setattr(team_mod, "gh_api", fake)
    runs = [cap_run("t", "failure", 1), cap_run("t", "success", 2), cap_run("t", "failure", 3)]
    ran = team_mod.resolve_failed_runs(runs, {"1": []})
    assert ran == {"1": [], "3": ["data"]}
    assert calls == ["repos/ax-company-hq/ax-backend/actions/runs/3/jobs?per_page=100"]


def test_cap_cache_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AX_CAP_CACHE", str(tmp_path / "c" / "runs.json"))
    assert team_mod.load_cap_cache() == {}
    team_mod.save_cap_cache({"5": ["data"]})
    assert team_mod.load_cap_cache() == {"5": ["data"]}


@pytest.mark.parametrize(
    ("title", "depts"),
    [("claude:infra-dejafix:request #3", ["dejafix"]),  # 별칭
     ("claude:nowhere:task #3", []),
     ("claude-request to:contracts by 2taeyoon", ["contracts"]),
     ("claude-review support/x by bxbae", ["marketing"]),
     ("claude-review memory/x by bxbae", ["orchestrator"]),  # 같은 사람 대체
     ("claude-review data/x by stranger", [])],
)
def test_run_token_depts(team: Team, title: str, depts: list[str]) -> None:
    assert team_mod.run_token_depts(team, title) == depts


def test_todays_claude_runs_pages_and_skips_self(monkeypatch) -> None:
    calls = []

    def fake(path, *extra):
        calls.append(path)
        if "claude-review.yml" in path and path.endswith("&page=1"):
            items = [{"id": i, "display_title": f"t{i}", "conclusion": "success"}
                     for i in range(100)]
        elif "claude-review.yml" in path:
            items = [{"id": 999, "display_title": "self", "conclusion": None}]
        else:
            items = []
        return {"workflow_runs": items}

    monkeypatch.setattr(team_mod, "gh_api", fake)
    monkeypatch.setenv("GITHUB_RUN_ID", "999")
    runs = team_mod.todays_claude_runs("2026-10-22")
    assert len(runs) == 100 and all(r[1] != 999 for r in runs)
    assert runs[0] == ("ax-company-hq/ax-backend", 0, "t0", "success")
    assert len(calls) == 5  # 워크플로 4개(감사 포함) + review 2쪽, 프론트는 아직 없음


def test_cap_status_ok_and_quota(team: Team, monkeypatch) -> None:
    monkeypatch.setattr(team_mod, "todays_claude_runs",
                        lambda day: [cap_run("claude:data:task #1", "success", i)
                                     for i in range(20)])
    monkeypatch.delenv("AX_CAP_CACHE", raising=False)
    st = team_mod.cap_status(team, ["JangJaehyeonn", "2taeyoon"], "write", "2026-10-22")
    assert st["JangJaehyeonn"]["ok"] is False and st["JangJaehyeonn"]["used"] == 20
    assert st["JangJaehyeonn"]["cap"] == 15 and "작성 20/15" in st["JangJaehyeonn"]["reason"]
    assert st["2taeyoon"]["ok"] is True and st["2taeyoon"]["used"] == 0
    # 작성을 다 써도 리뷰 상한은 따로
    st = team_mod.cap_status(team, ["JangJaehyeonn"], "review", "2026-10-22")
    assert st["JangJaehyeonn"]["ok"] is True and st["JangJaehyeonn"]["used"] == 0


def test_cap_status_fail_closed(team: Team, monkeypatch) -> None:
    def boom(day):
        raise team_mod.subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr(team_mod, "todays_claude_runs", boom)
    st = team_mod.cap_status(team, ["JangJaehyeonn"], "review", "2026-10-22")
    assert st["JangJaehyeonn"]["ok"] is False and st["JangJaehyeonn"]["used"] is None


def test_cap_status_rejects_unknown_kind(team: Team) -> None:
    with pytest.raises(team_mod.TeamError):
        team_mod.cap_status(team, ["JangJaehyeonn"], "daily", "2026-10-22")


@pytest.mark.parametrize(
    ("title", "kind"),
    [("claude:data:task #1", "write"), ("claude-request to:data by bxbae", "write"),
     ("claude-review data/x by bxbae", "review"), ("claude-request dispatch #3 by x", None),
     ("feat: x", None)],
)
def test_run_kind(title: str, kind: str | None) -> None:
    assert team_mod.run_kind(title) == kind


def test_usage_by_owner_split_by_kind(team: Team) -> None:
    runs = [cap_run("claude:data:task #1", "success", 1),
            cap_run("claude-review data/1-x-r0 by ax-company-hq-bot[bot]", "success", 2),
            cap_run("claude-review finance/x by 2taeyoon", "success", 3)]
    assert team_mod.usage_by_owner(team, runs, kind="write") == {"JangJaehyeonn": 1}
    assert team_mod.usage_by_owner(team, runs, kind="review") == {
        "2taeyoon": 1, "JangJaehyeonn": 1}


@pytest.mark.parametrize(
    ("labels", "add", "remove"),
    [
        ([], ["retry-1"], ["in-progress"]),
        (["in-progress", "task"], ["retry-1"], ["in-progress"]),
        # 같은 이슈 두 번째 실패 → needs-human (옛 retry-2·3이 있어도 같음)
        (["retry-1"], ["needs-human"], ["in-progress", "retry-1"]),
        (["retry-2", "in-progress"], ["needs-human"], ["in-progress", "retry-2"]),
        (["retry-3"], ["needs-human"], ["in-progress", "retry-3"]),
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


# ── 이벤트 페이로드 ───────────────────────────────────────
def _event(tmp_path, monkeypatch, name, payload):
    f = tmp_path / "event.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_NAME", name)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(f))


def test_event_pull_request_from_payload(tmp_path, monkeypatch) -> None:
    pr = {"number": 7, "head": {"sha": "a", "ref": "data/x"}, "title": "$(rm -rf /)"}
    _event(tmp_path, monkeypatch, "pull_request_target", {"pull_request": pr})
    assert team_mod.event_pull_request(7) == pr  # 값은 그대로 데이터로만 돌려준다


@pytest.mark.parametrize(
    ("name", "payload", "number"),
    [("workflow_dispatch", {"inputs": {"pr": "7"}}, 7),
     ("pull_request_target", {"pull_request": {"number": 8}}, 7),
     ("pull_request_target", {}, 7),
     ("issues", {"pull_request": {"number": 7}}, 7)],
)
def test_event_pull_request_none(tmp_path, monkeypatch, name, payload, number) -> None:
    _event(tmp_path, monkeypatch, name, payload)
    assert team_mod.event_pull_request(number) is None


def test_event_pull_request_bad_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(tmp_path / "missing.json"))
    assert team_mod.event_pull_request(1) is None


# ── 리뷰 범위 · 다시 실행 ─────────────────────────────────
@pytest.mark.parametrize(
    ("paths", "required"),
    [
        (["agents/data/a.py"], False),  # 부서 하나
        (["agents/data/a.py", "docs/notes/data/x.md", "uv.lock", "README.md"], False),
        (["agents/data/a.py", "memory/b.py"], True),  # 부서 2개 (사람 PR)
        (["contracts/x.py"], True),
        (["agent-template/x.py"], True),
        (["safety/src/x.py"], True),  # 안전 레이어는 단독 PR도 리뷰
        (["orchestrator/x.py"], True),
        (["agents/finance/x.py"], False),
        (["docs/notes/agent-template/x.md"], True),  # notes도 그 부서
        (["README.md"], False),  # human_paths만 (guards가 따로 처리)
    ],
)
def test_review_required(team: Team, paths: list[str], required: bool) -> None:
    assert team.review_required(team.classify(paths)) is required


def test_config_validates_review_depts_and_caps(team: Team) -> None:
    bad = {**team.data, "review_required_depts": ["nowhere"]}
    with pytest.raises(team_mod.TeamError):
        Team(bad)
    members = {k: dict(v) for k, v in team.data["members"].items()}
    members["bxbae"].pop("review_cap")
    with pytest.raises(team_mod.TeamError):
        Team({**team.data, "members": members})


@pytest.mark.parametrize(
    ("labels", "target"),
    [(["request", "to:data"], ("data", "request")),
     (["task", "dept:finance"], ("finance", "task")),
     (["to:nowhere"], None), (["request"], None),
     (["dept:hr", "to:memory"], ("memory", "request"))],
)
def test_write_target(team: Team, labels, target) -> None:
    assert team_mod.write_target(team, labels) == target


def test_dispatch_write_runs_claude_write(team: Team, monkeypatch) -> None:
    calls = []

    def fake(path, *extra):
        calls.append((path, extra))
        return {"labels": [{"name": "to:support"}, {"name": "retry-1"}]} if path.endswith("/9") \
            else None

    monkeypatch.setattr(team_mod, "gh_api", fake)
    assert "support:request" in team_mod.dispatch_write("o/r", team, 9)
    # 실행 전에 wip-wait로 대기 표시 (concurrency 대기열에서 취소돼도 다시 잡히게)
    assert calls[1] == ("repos/o/r/issues/9/labels", ("-X", "POST", "-f", "labels[]=wip-wait"))
    path, extra = calls[2]
    assert path == "repos/o/r/actions/workflows/_claude-write.yml/dispatches"
    assert "inputs[dept]=support" in extra and "inputs[mode]=request" in extra
    assert "inputs[issue]=9" in extra and "ref=dev" in extra


def test_dispatch_write_never_raises(team: Team, monkeypatch) -> None:
    def boom(path, *extra):
        raise team_mod.subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr(team_mod, "gh_api", boom)
    assert "다시 실행하지 못함" in team_mod.dispatch_write("o/r", team, 9)


def test_dispatch_write_explicit_dept_mode_skips_label_lookup(team: Team, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(team_mod, "gh_api", lambda path, *e: calls.append((path, e)))
    assert "data:task" in team_mod.dispatch_write("o/r", team, 9, "data", "task")
    assert [c[0] for c in calls] == ["repos/o/r/issues/9/labels",
                                     "repos/o/r/actions/workflows/_claude-write.yml/dispatches"]
    assert "inputs[dept]=data" in calls[1][1] and "inputs[mode]=task" in calls[1][1]


def test_guard_same_name_in_two_files_must_both_survive() -> None:
    # 준하 리뷰: 흔한 이름(test_smoke)이 두 파일에 있으면 한쪽을 지워도 통과하던 구멍
    a = "def test_smoke():\n    assert x()\n"
    files = {"agents/data/tests/test_a.py": (a, ""), "agents/data/tests/test_b.py": (a, a)}
    v = team_mod.find_test_weakening(
        [ch("agents/data/tests/test_a.py", "D"), ch("agents/data/tests/test_b.py")],
        lambda p: files[p][0], lambda p: files[p][1])
    assert any("테스트 삭제" in x and "test_smoke" in x for x in v)


# ── contracts 영향 부서 · 사람 전용 파일 ───────────────────
@pytest.mark.parametrize(
    ("paths", "reviewers"),
    [
        (["contracts/src/ax_contracts/schemas/data.py"], ["data"]),
        (["contracts/src/ax_contracts/schemas/hr.py",
          "contracts/tests/test_hr.py", "contracts/README.md"], ["hr"]),  # tests·md는 무시
        (["contracts/src/ax_contracts/schemas/record.py"], ["memory", "hr"]),
        (["contracts/src/ax_contracts/data_access/shop_metric_series.py"], ["data", "shop-api"]),
        # 표에 없는 공통 파일이 섞이면 공통 리뷰(agent-template + safety)를 더한다
        (["contracts/src/ax_contracts/schemas/finance.py",
          "contracts/src/ax_contracts/schemas/common.py"], ["finance", "agent-template", "safety"]),
        (["contracts/src/ax_contracts/schemas/event.py"], ["agent-template", "safety"]),
        (["contracts/tests/test_x.py"], ["agent-template", "safety"]),  # 테스트만이면 공통
    ],
)
def test_contracts_reviewers_by_changed_file(team: Team, paths, reviewers) -> None:
    assert team.contracts_reviewers(paths) == reviewers


def test_contracts_reviewers_without_paths_is_default(team: Team) -> None:
    assert team.contracts_reviewers(None) == ["agent-template", "safety"]


def test_contracts_affected_reviewer_replaced_when_author_owns_it(team: Team) -> None:
    # 재현(data 담당)이 data 계약을 바꾸면 data 자리는 대체된다
    # (contracts_fallback=data도 재현 → fallback 순서)
    got = team.reviewers_for(["contracts"], "JangJaehyeonn",
                             ["contracts/src/ax_contracts/schemas/data.py"])
    assert got and "data" not in got and "memory" not in got
    assert team.reviewers_for(["contracts"], "bxbae",
                              ["contracts/src/ax_contracts/schemas/data.py"]) == ["data"]


@pytest.mark.parametrize("path", ["safety/src/ax_safety/rules/thresholds.toml",
                                  "agents/finance/src/finance_agent/ai_review_policy.toml"])
def test_human_files_are_human_paths(team: Team, path: str) -> None:
    assert team.is_human_file(path) and team.dept_of_path(path) is None
    c = team.classify([path])
    assert c.human == [path] and not c.depts
    # 봇은 못 바꾸고 사람은 바꿀 수 있다 (대신 code owner 승인)
    dept = "safety" if path.startswith("safety") else "finance"
    assert any("사람 승인 경로" in v for v in team.check_diff([ch(path)], [dept], "bot"))
    assert team.check_diff([ch(path)], [dept], "human") == []
    # 같은 폴더의 다른 파일은 여전히 부서 것
    assert team.dept_of_path(path.rsplit("/", 1)[0] + "/other.py") == dept


def test_human_files_must_be_inside_dept_folder(team: Team) -> None:
    with pytest.raises(team_mod.TeamError):
        Team({**team.data, "human_files": ["README.md"]})


def test_contracts_affected_validates_depts(team: Team) -> None:
    with pytest.raises(team_mod.TeamError):
        Team({**team.data, "contracts_affected": {"src/x.py": ["nowhere"]}})


def test_cap_counts_contracts_review_runs_from_jobs(team: Team, monkeypatch) -> None:
    calls = []

    def fake(path, *extra):
        calls.append(path)
        return {"jobs": [_job("claude:data:review", [("Claude Code 설치 (npm)", "success"),
                                                      ("Claude (리뷰)", "success")])]}

    monkeypatch.setattr(team_mod, "gh_api", fake)
    runs = [cap_run("claude-review contracts/86-x-r0 by ax-company-hq-bot[bot]", "success", 7),
            cap_run("claude-review data/1-x-r0 by ax-company-hq-bot[bot]", "success", 8)]
    ran = team_mod.resolve_failed_runs(runs, {}, team)
    assert ran == {"7": ["data"]} and len(calls) == 1  # data 브랜치 성공 run은 jobs 안 읽음
    # contracts 리뷰는 실제 리뷰 부서(data → 재현)로, data 리뷰는 제목대로(finance → 태윤)
    assert team_mod.usage_by_owner(team, runs, ran, "review") == {
        "JangJaehyeonn": 1, "2taeyoon": 1}


def test_audit_runs_counted_as_review_from_jobs(team: Team, monkeypatch) -> None:
    assert team_mod.run_kind("human-path-audit #12") == "review"
    assert team_mod.needs_jobs("human-path-audit #12", "success", team)
    assert not team_mod.needs_jobs("human-path-audit #12", None, team)  # 실행 중은 아직 모름
    monkeypatch.setattr(team_mod, "gh_api", lambda path, *e: {"jobs": [
        _job("claude:data:audit", [("Claude Code 설치 (npm)", "success"),
                                   ("Claude (감사)", "success")]),
        _job("claude:hr:audit", [("Claude Code 설치 (npm)", "success"),
                                 ("Claude (감사)", "failure")])]})
    runs = [cap_run("human-path-audit #12", "success", 30)]
    ran = team_mod.resolve_failed_runs(runs, {}, team)
    assert ran == {"30": ["data", "hr"]}
    assert team_mod.usage_by_owner(team, runs, ran, "review") == {
        "JangJaehyeonn": 1, "haakim7": 1}
    assert team_mod.usage_by_owner(team, runs, ran, "write") == {}
