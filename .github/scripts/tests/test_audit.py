"""audit.py: 감사 경로, 기계 규칙, 감사 토큰 순번, 판정 형식(fail-closed) 테스트."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("audit", SCRIPTS / "audit.py")
audit = importlib.util.module_from_spec(spec)
sys.modules["audit"] = audit
spec.loader.exec_module(audit)
team_mod = audit.team_mod
HEAD = "a" * 40


@pytest.fixture(scope="module")
def team():
    return team_mod.Team.load()


def ch(path, status="M"):
    return team_mod.Change(status=status, path=path)


def rules(team, path, base, head, status="M"):
    return audit.mechanical_findings(team, [ch(path, status)], lambda p: base, lambda p: head)


# ── 경로 등급 ─────────────────────────────────────────────
@pytest.mark.parametrize(
    ("path", "audited"),
    [("docs/requirements.md", True), ("docs/automation-spec.md", True),
     ("contracts/src/ax_contracts/schemas/data.py", True), ("scripts/compat-check.py", True),
     ("pyproject.toml", True),
     ("scripts/safe-test.sh", False),  # 핵심 scripts
     ("docs/notes/data/memo.md", False),  # 부서 notes
     (".github/workflows/ci.yml", False), ("CLAUDE.md", False), ("README.md", False),
     ("agents/data/pyproject.toml", False), ("safety/src/ax_safety/rules/thresholds.toml", False)],
)
def test_is_audit_path(team, path, audited) -> None:
    assert audit.is_audit_path(team, path) is audited


# ── 기계 규칙: 걸리는 경우 / 안 걸리는 경우 ───────────────
WF = """name: x
on:
  pull_request:
permissions: {}
jobs:
  a:
    runs-on: ubuntu-latest
    environment: bot
    permissions:
      contents: read
    steps:
      - uses: actions/create-github-app-token@v2
        with:
          permission-issues: read
      - run: echo ${{ secrets.AX_BOT_APP_ID }}
"""


def test_workflow_unchanged_or_comment_only_passes(team) -> None:
    assert rules(team, ".github/workflows/x.yml", WF, WF + "# 주석\n") == []


@pytest.mark.parametrize(
    ("old", "new", "expect"),
    [
        ("      contents: read", "      contents: write", "권한 상승 a contents → write"),
        ("permissions: {}", "permissions: write-all", "권한 상승 (workflow) *"),
        ("          permission-issues: read", "          permission-issues: write",
         "권한 상승 a (앱 토큰) issues"),
        ("    environment: bot", "    environment: claude", "environment 변경 a: bot → claude"),
        ("  pull_request:", "  pull_request:\n  pull_request_target:",
         "트리거 추가 pull_request_target"),
        ("  pull_request:", "  pull_request:\n  workflow_run:\n    workflows: [ci]",
         "트리거 추가 workflow_run"),
        ("      - run: echo", "      - uses: actions/checkout@v4\n        with:\n"
         "          ref: ${{ github.event.pull_request.head.sha }}\n      - run: echo",
         "PR head를 받는 줄 추가"),
        ("secrets.AX_BOT_APP_ID", "secrets.AX_BOT_APP_ID }} ${{ secrets.NEW_TOKEN",
         "새 secrets 참조 NEW_TOKEN"),
    ],
)
def test_workflow_rules_hit(team, old, new, expect) -> None:
    found = rules(team, ".github/workflows/x.yml", WF, WF.replace(old, new))
    assert any(expect in f for f in found), found


def test_workflow_permission_lowered_passes(team) -> None:
    assert rules(team, ".github/workflows/x.yml", WF,
                 WF.replace("      contents: read", "      contents: none")) == []


def test_existing_trigger_and_secret_not_flagged(team) -> None:
    base = WF.replace("  pull_request:", "  pull_request_target:")
    head = base.replace("echo ${{", "echo again ${{")
    assert rules(team, ".github/workflows/x.yml", base, head) == []


def test_new_workflow_file_with_write_is_flagged(team) -> None:
    found = rules(team, ".github/workflows/new.yml", "", WF.replace("read", "write"), "A")
    assert any("권한 상승" in f for f in found)


def test_secrets_in_non_workflow_file(team) -> None:
    assert any("새 secrets" in f for f in rules(team, "scripts/x.sh", "a\n",
                                               "a\n${{ secrets.X }}\n"))
    assert any("동적 참조" in f for f in rules(team, "docs/x.md", "", "secrets[name]\n"))


@pytest.mark.parametrize("path", [".github/CODEOWNERS", "CODEOWNERS"])
def test_codeowners_change(team, path) -> None:
    assert any("CODEOWNERS 변경" in f for f in rules(team, path, "* @a\n", "* @b\n"))


def test_branch_protection_line(team) -> None:
    found = rules(team, "scripts/x.sh", "",
                  "gh api -X PUT repos/o/r/branches/dev/protection\n", "A")
    assert any("브랜치 보호" in f for f in found)
    assert rules(team, "docs/x.md", "protection 문서\n", "protection 문서 수정\n") == []


TEAM_YML = (Path(__file__).resolve().parents[2] / "team.yml").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("old", "new", "expect"),
    [("write_cap: 15, review_cap: 15 }", "write_cap: 99, review_cap: 15 }", "members"),
     ("hotfix_labelers: [JangJaehyeonn, haakim7]",
      "hotfix_labelers: [JangJaehyeonn, haakim7, bxbae]", "hotfix_labelers"),
     ("wip: { open_bot_prs: 2", "wip: { open_bot_prs: 9", "wip"),
     ("owners: [bxbae], env_prefix: SUPPORT", "owners: [bxbae, NYejin], env_prefix: SUPPORT",
      "depts.support.owners")],
)
def test_team_guarded_keys(team, old, new, expect) -> None:
    head = TEAM_YML.replace(old, new, 1)
    assert head != TEAM_YML
    assert any(expect in f for f in rules(team, ".github/team.yml", TEAM_YML, head))


def test_team_unguarded_key_passes(team) -> None:
    head = TEAM_YML.replace("data: [finance]", "data: [finance, support]", 1)
    assert head != TEAM_YML
    assert rules(team, ".github/team.yml", TEAM_YML, head) == []


TOML = '[grades]\ncoupon_c = 400000\nrefund_c = 100000\n[auto_review]\nenabled = false\n'
TOML_PATH = "safety/src/ax_safety/rules/thresholds.toml"


@pytest.mark.parametrize(
    ("head", "expect"),
    [(TOML.replace("400000", "900000"), "한도 상향 grades.coupon_c 400000 → 900000"),
     (TOML.replace("false", "true"), "auto_review.enabled false → true"),
     (TOML + "extra = 1\n", "새 키 auto_review.extra")],
)
def test_toml_limit_raised(team, head, expect) -> None:
    assert any(expect in f for f in rules(team, TOML_PATH, TOML, head))


def test_toml_limit_lowered_passes(team) -> None:
    assert rules(team, TOML_PATH, TOML, TOML.replace("400000", "300000")) == []


def test_toml_unparseable_is_flagged(team) -> None:
    assert any("TOML을 읽을 수 없음" in f for f in rules(team, TOML_PATH, TOML, "[x\n"))


@pytest.mark.parametrize("path", audit.AUDIT_FILES)
def test_audit_files_changed(team, path) -> None:
    assert any("감사 워크플로·프롬프트·규칙" in f for f in rules(team, path, "a\n", "b\n"))


def test_docs_and_contracts_change_passes(team) -> None:
    changes = [ch("docs/requirements.md"), ch("contracts/src/ax_contracts/schemas/data.py")]
    assert audit.mechanical_findings(team, changes, lambda p: "x = 1\n",
                                     lambda p: "x = 1\ny: int = 2\n") == []


def test_yaml_unparseable_is_flagged(team) -> None:
    assert any("YAML을 읽을 수 없음" in f
               for f in rules(team, ".github/workflows/x.yml", WF, "a: [\n"))


def test_symlink_is_flagged(team) -> None:
    c = team_mod.Change(status="A", path="docs/link", new_mode=team_mod.SYMLINK_MODE)
    assert audit.mechanical_findings(team, [c], lambda p: "", lambda p: "x")


def test_binary_is_flagged(team) -> None:
    def boom(p):
        raise UnicodeDecodeError("utf-8", b"", 0, 1, "x")

    assert any("읽을 수 없음" in f for f in
               audit.mechanical_findings(team, [ch("docs/a.png")], boom, boom))


# ── 감사 토큰 순번 ────────────────────────────────────────
def caps(**ok):
    return {k: {"ok": v} for k, v in ok.items()}


def test_pick_auditors_rotates_by_pr(team) -> None:
    all_ok = caps(JangJaehyeonn=True, haakim7=True, **{"2taeyoon": True})
    a0 = audit.pick_auditors(team, 0, all_ok)
    a1 = audit.pick_auditors(team, 1, all_ok)
    assert [a["owner"] for a in a0] == ["JangJaehyeonn", "haakim7"]
    assert [a["owner"] for a in a1] == ["haakim7", "2taeyoon"]
    assert [a["role"] for a in a0] == ["security", "standards"]
    assert a0[0]["token_secret"] == "CLAUDE_TOKEN_JAEHYEON"


def test_pick_auditors_skips_quota_and_needs_two(team) -> None:
    got = audit.pick_auditors(team, 0, caps(JangJaehyeonn=False, haakim7=True,
                                            **{"2taeyoon": True}))
    assert [a["owner"] for a in got] == ["haakim7", "2taeyoon"]
    assert audit.pick_auditors(team, 0, caps(JangJaehyeonn=False, haakim7=True,
                                             **{"2taeyoon": False})) is None


# ── 판정 형식 (fail-closed) ──────────────────────────────
def verdict(**kw):
    v = {"head_sha": HEAD, "role": "security", "verdict": "approve",
         "findings": [{"severity": "low", "file": "docs/a.md", "detail": "참고"}],
         "summary": "문제 없음"}
    v.update(kw)
    return v


def test_check_verdict_ok() -> None:
    out = audit.check_verdict(verdict(), HEAD, "security")
    assert out["verdict"] == "approve" and out["findings"][0]["severity"] == "low"


@pytest.mark.parametrize(
    "data",
    [
        "approve",  # 객체 아님
        {k: v for k, v in verdict().items() if k != "summary"},  # 키 빠짐
        {**verdict(), "extra": 1},  # 키 더함
        verdict(head_sha="b" * 40),
        verdict(role="standards"),
        verdict(verdict="ok"),
        verdict(findings="none"),
        verdict(findings=[{"severity": "low", "file": "a"}]),
        verdict(findings=[{"severity": "critical", "file": "a", "detail": "b"}]),
        verdict(findings=[{"severity": "low", "file": "a", "detail": 3}]),
        verdict(verdict="reject", findings=[]),  # 거절인데 근거 없음
        verdict(findings=[{"severity": "high", "file": "a", "detail": "b"}]),  # high인데 승인
        verdict(summary=None),
        verdict(findings=[{"severity": "low", "file": "a", "detail": "b"}] * 21),
    ],
)
def test_check_verdict_rejects_bad_shapes(data) -> None:
    with pytest.raises(audit.AuditError):
        audit.check_verdict(data, HEAD, "security")


PLAN = {"head_sha": HEAD, "auditors": [
    {"role": "security", "dept": "data", "token_secret": "T1", "owner": "JangJaehyeonn",
     "name": "재현"},
    {"role": "standards", "dept": "hr", "token_secret": "T2", "owner": "haakim7", "name": "준하"}]}


def test_decide() -> None:
    ok = {"security": verdict(), "standards": verdict(role="standards")}
    assert audit.decide(PLAN, ok)[0] == "success"
    rej = {**ok, "standards": verdict(role="standards", verdict="reject")}
    assert audit.decide(PLAN, rej) == ("failure", "감사 거절: 기준 정합 감사")
    err = {**ok, "security": "AuditError: x"}
    assert audit.decide(PLAN, err)[0] == "failure"
    assert audit.decide(PLAN, {"security": verdict()})[0] == "failure"  # 하나 없음


def apply_env(monkeypatch, tmp_path, files):
    for k, v in {"GITHUB_REPOSITORY": "o/r", "GITHUB_SERVER_URL": "https://github.com",
                 "GITHUB_RUN_ID": "1"}.items():
        monkeypatch.setenv(k, v)
    for role, content in files.items():
        d = tmp_path / f"audit-{role}"
        d.mkdir()
        (d / "verdict.json").write_text(content, encoding="utf-8")
    calls = []

    def fake_api(path, *extra):
        calls.append((path, extra))
        if path.endswith("/pulls/9"):
            return {"head": {"sha": HEAD}}
        return []

    monkeypatch.setattr(audit, "api", fake_api)
    monkeypatch.setattr(team_mod, "post_comment", lambda r, n, b: calls.append(("comment", b)))
    return calls


def status_of(calls):
    for path, extra in calls:
        if "/statuses/" in path:
            return next(x for x in extra if x.startswith("state=")).split("=", 1)[1]
    return None


def test_apply_success_when_both_approve(team, monkeypatch, tmp_path) -> None:
    calls = apply_env(monkeypatch, tmp_path, {
        "security": json.dumps(verdict()),
        "standards": json.dumps(verdict(role="standards"))})
    assert audit.cmd_apply(team, 9, PLAN, tmp_path) == "success"
    assert status_of(calls) == "success"


@pytest.mark.parametrize(
    "files",
    [{"security": json.dumps(verdict())},  # 한쪽 결과 파일 없음
     {"security": json.dumps(verdict()), "standards": "{not json"},  # JSON 파싱 실패
     {"security": json.dumps(verdict()), "standards": ""},  # 빈 파일
     {"security": json.dumps(verdict()),
      "standards": json.dumps(verdict(role="standards", head_sha="b" * 40))}],
)
def test_apply_fail_closed(team, monkeypatch, tmp_path, files) -> None:
    calls = apply_env(monkeypatch, tmp_path, files)
    assert audit.cmd_apply(team, 9, PLAN, tmp_path) == "failure"
    assert status_of(calls) == "failure"


def test_verdict_cli_exit_code(tmp_path) -> None:
    f = tmp_path / "v.json"
    f.write_text("{oops", encoding="utf-8")
    assert audit.main(["verdict", str(f), "--head-sha", HEAD, "--role", "security"]) == 1
    f.write_text(json.dumps(verdict()), encoding="utf-8")
    assert audit.main(["verdict", str(f), "--head-sha", HEAD, "--role", "security"]) == 0


def test_mark_mechanical_sets_needs_human(team, monkeypatch, tmp_path) -> None:
    calls = apply_env(monkeypatch, tmp_path, {})
    plan = {"head_sha": HEAD, "mechanical": [".github/CODEOWNERS: CODEOWNERS 변경"]}
    assert audit.cmd_mark(team, 9, plan) == "mechanical"
    assert status_of(calls) == "failure"
    assert any("labels[]=needs-human" in e for p, e in calls if p.endswith("/labels"))


def test_merged_records_in_report(team, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    calls = []

    def fake_api(path, *extra):
        calls.append((path, extra))
        if path.endswith("/pulls/9"):
            return {"merged_at": "2026-10-23T05:00:00Z", "base": {"ref": "dev"},
                    "title": "docs: x"}
        if "/files" in path:
            return [{"filename": "docs/a.md"}, {"filename": "agents/data/x.py"}]
        if "labels=report" in path:
            return [{"number": 50, "title": "[report] 정리 2026-10-23",
                     "user": {"login": team.bot_login}}]
        return None

    monkeypatch.setattr(audit, "api", fake_api)
    monkeypatch.setattr(team_mod, "post_comment", lambda r, n, b: calls.append((n, b)))
    assert audit.cmd_merged(team, 9) == "report #50에 기록"
    assert (50, "@JangJaehyeonn 감사 경로 머지: #9 docs: x — `docs/a.md`") in calls
    assert any("labels[]=audit-path" in e for p, e in calls if p.endswith("/9/labels"))
