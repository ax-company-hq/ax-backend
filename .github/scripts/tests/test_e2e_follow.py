"""e2e_follow.py: 스위치가 꺼져 있으면 무동작, 기준선은 무동작, regressed만 이슈, 봇 PR만 revert."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("e2e_follow", SCRIPTS / "e2e_follow.py")
ef = importlib.util.module_from_spec(spec)
sys.modules["e2e_follow"] = ef
spec.loader.exec_module(ef)
BOT = "ax-company-hq-bot[bot]"
REPO = "ax-company-hq/ax-backend"


@pytest.fixture
def team():
    return ef.team_mod.Team.load()


class Calls:
    """GitHub 호출을 기록한다 (실제 호출 없음)."""

    def __init__(self, monkeypatch, issues=None) -> None:
        self.log: list[tuple] = []
        self.issues = issues or []
        monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
        monkeypatch.setattr(ef, "api", self.api)
        monkeypatch.setattr(
            ef.team_mod, "post_comment", lambda r, n, b: self.log.append(("say", n, b))
        )
        monkeypatch.setattr(
            ef.team_mod,
            "dispatch_write",
            lambda *a, **k: self.log.append(("dispatch", a[2], a[3], a[4])) or "ok",
        )

    def api(self, path, *extra):
        self.log.append(("api", path, extra))
        if path.startswith(f"repos/{REPO}/issues?state=open"):
            return self.issues
        if path.endswith("/issues") and "POST" in extra:
            return {"number": 900}
        return None

    def kinds(self) -> list[str]:
        return [c[0] for c in self.log]

    def posts(self) -> list[tuple]:
        return [c for c in self.log
                if c[0] == "api" and c[1].endswith("/issues") and "POST" in c[2]]

    def labeled(self, label: str) -> bool:
        return any(c[0] == "api" and f"labels[]={label}" in c[2] for c in self.log)


def write(tmp_path: Path, regressions, baseline=False) -> tuple[Path, Path]:
    detail = {
        "baseline": baseline,
        "regressions": regressions,
        "stages": [
            {"name": "[총괄] impact (impact_analysis)", "folder": "data", "dept": "data"},
            {"name": "[총괄] diagnose", "folder": "infra-dejafix", "dept": "infra"},
        ],
    }
    (tmp_path / "d.json").write_text(json.dumps(detail), encoding="utf-8")
    (tmp_path / "r.json").write_text("{}", encoding="utf-8")
    return tmp_path / "r.json", tmp_path / "d.json"


def reg(name="[총괄] impact (impact_analysis)", dept="data", view="unit"):
    return {"name": name, "dept": dept, "view": view, "note": "status=failed", "since": None,
            "last_pass_commit": "abc", "req_ids": ["DEP-DATA-02"], "no": 2}


def run_act(tmp_path, regressions, baseline=False, prev="p" * 40):
    r, d = write(tmp_path, regressions, baseline)
    return ef.act(r, d, "s" * 40, prev)


def test_everything_off_does_nothing(tmp_path, monkeypatch) -> None:
    for name in ("AX_E2E", "AX_GO", "AX_AUTO_REVERT"):
        monkeypatch.delenv(name, raising=False)
    calls = Calls(monkeypatch)
    assert run_act(tmp_path, [reg()]) == ["AX_E2E가 on이 아님 - 보고만 함"]
    assert calls.log == []


def test_ax_e2e_off_ignores_other_switches(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AX_GO", "on")
    monkeypatch.setenv("AX_AUTO_REVERT", "on")
    monkeypatch.delenv("AX_E2E", raising=False)
    calls = Calls(monkeypatch)
    run_act(tmp_path, [reg()])
    assert calls.log == []


def test_baseline_and_no_regression_do_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AX_E2E", "on")
    monkeypatch.setenv("AX_AUTO_REVERT", "on")
    calls = Calls(monkeypatch)
    assert run_act(tmp_path, [reg()], baseline=True) == ["첫 실행 - 기준선만 기록"]
    assert run_act(tmp_path, []) == ["회귀 없음"]  # broken·pending만 있으면 regressions는 비어 있다
    assert calls.log == []


def test_regression_files_issue_with_labels_and_no_dispatch_without_go(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AX_E2E", "on")
    monkeypatch.delenv("AX_GO", raising=False)
    monkeypatch.delenv("AX_AUTO_REVERT", raising=False)
    calls = Calls(monkeypatch)
    out = run_act(tmp_path, [reg(), reg(view="chain"), reg("[총괄] diagnose", "infra")])
    assert "dispatch" not in calls.kinds()
    assert len(calls.posts()) == 2  # 같은 단계의 단독·연결은 한 이슈
    flat = [str(p[2]) for p in calls.posts()]
    assert any(
        "labels[]=dept:data" in f and "labels[]=from:e2e" in f and "labels[]=bug" in f
        for f in flat
    )
    assert any("labels[]=dept:dejafix" in f for f in flat)  # infra 카드 = dejafix 부서
    assert all("<!-- autopilot -->" in f for f in flat)
    assert len(out) == 2


def test_go_on_dispatches_write(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AX_E2E", "on")
    monkeypatch.setenv("AX_GO", "on")
    monkeypatch.delenv("AX_AUTO_REVERT", raising=False)
    calls = Calls(monkeypatch)
    run_act(tmp_path, [reg()])
    assert ("dispatch", 900, "data", "task") in calls.log


def test_duplicate_regression_comments_instead_of_new_issue(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AX_E2E", "on")
    monkeypatch.setenv("AX_GO", "on")
    monkeypatch.delenv("AX_AUTO_REVERT", raising=False)
    existing = [{"number": 41, "body": ef.marker_of("[총괄] impact (impact_analysis)")}]
    calls = Calls(monkeypatch, issues=existing)
    run_act(tmp_path, [reg()])
    assert calls.posts() == [] and "dispatch" not in calls.kinds()
    assert [c for c in calls.log if c[0] == "say"][0][1] == 41
    assert "<!-- autopilot -->" in calls.log[-1][2]


def pr(branch="data/1-x-r0", user=BOT, number=7):
    return {"number": number, "title": "feat(data): x", "user": {"login": user},
            "head": {"ref": branch}, "merge_commit_sha": "m" * 40}


def test_decide_revert_only_bot_pr_without_core_files(team) -> None:
    decide = ef.decide_revert
    assert decide(team, pr(), ["agents/data/x.py"], []) == "revert"
    assert decide(team, pr(user="JangJaehyeonn"), ["agents/data/x.py"], []) == "human"
    assert decide(team, pr(), [".github/team.yml"], []) == "core"
    assert decide(team, pr(), ["safety/src/ax_safety/rules/thresholds.toml"], []) == "core"
    assert decide(team, pr(branch="revert/5"), ["agents/data/x.py"], []) == "no_rerevert"
    assert decide(team, pr(), [], [{"state": "closed", "merged_at": "t"}]) == "pause"
    assert decide(team, pr(), [], [{"state": "open", "merged_at": None}]) == "wait"


def setup_cause(monkeypatch, author, files, prs=(7,), branch="data/1-x-r0"):
    monkeypatch.setenv("AX_E2E", "on")
    monkeypatch.setenv("AX_AUTO_REVERT", "on")
    monkeypatch.delenv("AX_GO", raising=False)
    calls = Calls(monkeypatch)
    reverted: list[int] = []
    monkeypatch.setattr(ef, "suspect_prs", lambda p, s: list(prs))
    monkeypatch.setattr(ef, "revert_prs", lambda n: [])
    monkeypatch.setattr(
        ef, "git_revert_pr", lambda p, i: reverted.append(p["number"]) or "reverted"
    )
    orig = calls.api

    def api(path, *extra):
        if path.endswith("/pulls/7"):
            return pr(branch, author)
        if path.endswith("/pulls/7/files?per_page=100"):
            return [{"filename": f} for f in files]
        return orig(path, *extra)

    monkeypatch.setattr(ef, "api", api)
    return calls, reverted


def test_bot_pr_is_reverted(tmp_path, monkeypatch) -> None:
    calls, reverted = setup_cause(monkeypatch, BOT, ["agents/data/x.py"])
    run_act(tmp_path, [reg()])
    assert reverted == [7]


def test_human_pr_mentions_author_and_needs_human_without_revert(tmp_path, monkeypatch) -> None:
    calls, reverted = setup_cause(monkeypatch, "JangJaehyeonn", ["agents/data/x.py"])
    run_act(tmp_path, [reg()])
    assert reverted == []
    assert any("@JangJaehyeonn" in c[2] for c in calls.log if c[0] == "say")
    assert calls.labeled("needs-human")


def test_core_path_pr_is_needs_human_only(tmp_path, monkeypatch) -> None:
    calls, reverted = setup_cause(monkeypatch, BOT, [".github/workflows/ci.yml"])
    run_act(tmp_path, [reg()])
    assert reverted == [] and calls.labeled("needs-human")


def test_revert_branch_pr_is_never_reverted_again(tmp_path, monkeypatch) -> None:
    calls, reverted = setup_cause(monkeypatch, BOT, ["agents/data/x.py"], branch="revert/3")
    run_act(tmp_path, [reg()])
    assert reverted == [] and calls.labeled("needs-human")


def test_ambiguous_cause_does_not_revert(tmp_path, monkeypatch) -> None:
    calls, reverted = setup_cause(monkeypatch, BOT, ["agents/data/x.py"], prs=(7, 8))
    run_act(tmp_path, [reg()])
    assert reverted == []
    assert any(c[0] == "say" and "하나로 정하지 못해" in c[2] for c in calls.log)


def test_auto_revert_off_never_looks_for_cause(tmp_path, monkeypatch) -> None:
    calls, reverted = setup_cause(monkeypatch, BOT, ["agents/data/x.py"])
    monkeypatch.delenv("AX_AUTO_REVERT")
    monkeypatch.setattr(ef, "suspect_prs", lambda p, s: pytest.fail("원인 탐색이 돌면 안 됨"))
    run_act(tmp_path, [reg()])
    assert reverted == []


def test_pause_when_still_red_after_revert(tmp_path, monkeypatch) -> None:
    calls, reverted = setup_cause(monkeypatch, BOT, ["agents/data/x.py"])
    monkeypatch.setattr(ef, "revert_prs", lambda n: [{"state": "closed", "merged_at": "t"}])
    paused: list[bool] = []
    monkeypatch.setattr(ef, "set_pause", lambda: paused.append(True) or True)
    run_act(tmp_path, [reg()])
    assert paused == [True] and reverted == []
