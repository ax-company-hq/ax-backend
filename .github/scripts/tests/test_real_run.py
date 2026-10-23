"""실서버 실행 보안·판정 테스트: 실행자 확인, 서버 PC 라벨 가드, 스위치 off 무동작, 이슈 판정."""

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS.parents[1]
sys.path.insert(0, str(SCRIPTS))


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


guard = load("real_guard")
run_mod = load("real_run")
follow = load("real_follow")
REPO = "ax-company-hq/ax-backend"


# ── 실행자 확인 ──────────────────────────────────────────
def test_code_owners_come_from_github_folder_line() -> None:
    owners = guard.code_owners(ROOT / ".github" / "CODEOWNERS")
    assert {"JangJaehyeonn", "haakim7", "2taeyoon"} <= owners
    assert "bxbae" not in owners


def test_dispatch_allowed_only_for_code_owner() -> None:
    owners = {"JangJaehyeonn", "haakim7", "2taeyoon"}
    assert guard.actor_allowed("haakim7", "workflow_dispatch", owners)[0]
    assert not guard.actor_allowed("bxbae", "workflow_dispatch", owners)[0]
    assert not guard.actor_allowed("stranger", "workflow_dispatch", owners)[0]


def test_other_events_are_rejected_but_schedule_passes() -> None:
    owners = {"haakim7"}
    assert guard.actor_allowed("anyone", "schedule", owners)[0]
    for event in ("pull_request", "pull_request_target", "push", "issue_comment"):
        assert not guard.actor_allowed("haakim7", event, owners)[0]


def test_check_actor_cli_exit_codes(tmp_path: Path) -> None:
    owners = tmp_path / "CODEOWNERS"
    owners.write_text("/.github/   @haakim7 @2taeyoon\n", encoding="utf-8")
    base = ["check-actor", "--event", "workflow_dispatch", "--codeowners", str(owners)]
    assert guard.main([*base, "--actor", "haakim7"]) == 0
    assert guard.main([*base, "--actor", "bxbae"]) == 1


# ── ax-server 라벨 가드 ──────────────────────────────────
RUNS_ON = "jobs:\n  a:\n    runs-on: [self-hosted, ax-server]\n"
SCHEDULE_ON = "on:\n  schedule:\n    - cron: 0 1 * * *\n  workflow_dispatch:\n"


def write_wf(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(body, encoding="utf-8")


def test_workflows_in_repo_pass_the_label_guard() -> None:
    assert guard.check_workflows(ROOT / ".github" / "workflows") == []


def test_label_in_other_workflow_fails(tmp_path: Path) -> None:
    write_wf(tmp_path, "ci.yml", "on: push\n" + RUNS_ON)
    found = guard.check_workflows(tmp_path)
    assert len(found) == 1 and "ci.yml" in found[0]
    assert guard.main(["check-runner-label", "--workflows", str(tmp_path)]) == 1


def test_label_in_allowlisted_workflows_passes(tmp_path: Path) -> None:
    write_wf(tmp_path, "real-run.yml", SCHEDULE_ON + RUNS_ON)
    write_wf(tmp_path, "devroom-update.yml", SCHEDULE_ON + RUNS_ON)
    write_wf(tmp_path, "ci.yml", "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n")
    assert guard.check_workflows(tmp_path) == []


@pytest.mark.parametrize("trigger", ["pull_request", "pull_request_target", "issue_comment"])
def test_allowlisted_workflow_with_external_trigger_fails(tmp_path: Path, trigger: str) -> None:
    write_wf(tmp_path, "real-run.yml", f"on:\n  {trigger}:\n  workflow_dispatch:\n" + RUNS_ON)
    assert any("금지된 트리거" in v for v in guard.check_workflows(tmp_path))


def test_similar_label_names_are_not_flagged(tmp_path: Path) -> None:
    write_wf(tmp_path, "x.yml", "on: push\njobs:\n  a:\n    runs-on: my-ax-server-2\n")
    assert guard.check_workflows(tmp_path) == []


def test_real_run_yml_shape() -> None:
    text = (ROOT / ".github" / "workflows" / "real-run.yml").read_text(encoding="utf-8")
    assert guard.trigger_names(text) == {"schedule", "workflow_dispatch"}
    assert text.count("vars.AX_REAL_RUN == ") >= 3  # 모든 job이 스위치로 막힌다
    assert "ref: dev" in text
    assert "secrets." not in text.replace("secrets.AX_BOT_", "")  # 워크플로에 키를 넣지 않는다


# ── real_run.py ──────────────────────────────────────────
def test_planned_runs_friday_is_three_in_kst() -> None:
    friday_kst = datetime(2026, 10, 22, 17, 0, tzinfo=UTC)  # 금요일 02:00 KST
    monday_kst = datetime(2026, 10, 18, 17, 0, tzinfo=UTC)  # 월요일 02:00 KST
    assert run_mod.planned_runs("auto", friday_kst) == 3
    assert run_mod.planned_runs("auto", monday_kst) == 1
    assert run_mod.planned_runs("2") == 2
    assert run_mod.planned_runs("9") == 3


def test_env_file_reader_takes_only_tokens_and_zabbix(tmp_path: Path, capsys) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "LLM_API_KEY=secret-llm\nSUPPORT_TOKEN=tok-s\n# c\nAX_ZABBIX_URL=http://z/\nOTHER=1\n",
        encoding="utf-8",
    )
    got = run_mod.read_env_file(env)
    assert set(got) == {"SUPPORT_TOKEN", "AX_ZABBIX_URL"}
    assert "secret-llm" not in capsys.readouterr().out


def test_zabbix_not_configured_means_detect_mock() -> None:
    ok, why = run_mod.zabbix_reachable(None)
    assert not ok and "AX_ZABBIX_URL" in why
    ok, _ = run_mod.zabbix_reachable("http://127.0.0.1:1", timeout=0.5)
    assert not ok


def test_record_skips_without_gateway(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(run_mod, "http_ok", lambda url, timeout=3.0: False)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out.txt"))
    args = run_mod.argparse.Namespace(out=str(tmp_path / "rec"))
    assert run_mod.cmd_record(args) == 0
    assert "recorded=false" in (tmp_path / "out.txt").read_text(encoding="utf-8")
    assert "건너뜀" in capsys.readouterr().out


def test_missing_fault_script_is_reported_not_fatal(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(run_mod, "ROOT", tmp_path)
    ok, why = run_mod.fault("inject")
    assert not ok and "infra 요청" in why


# ── real_follow.py: 스위치 off 무동작 + mock pass / real fail ─────
def result(state: str, note: str = "") -> dict:
    return {"state": state, "note": note}


def stage(no: int, name: str, dept: str, unit: str, chain: str, real: str, note: str = "") -> dict:
    return {
        "no": no,
        "name": name,
        "dept": dept,
        "req_ids": ["DEP-X"],
        "unit": result(unit),
        "chain": result(chain),
        "real": result(real, note),
    }


class Gh:
    def __init__(self, monkeypatch, issues=None) -> None:
        self.calls: list[tuple] = []
        self.issues = issues or []
        monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
        monkeypatch.setattr(follow.ef, "api", self.api)
        monkeypatch.setattr(
            follow.ef.team_mod, "post_comment", lambda r, n, b: self.calls.append(("say", n, b))
        )

    def api(self, path, *extra):
        self.calls.append(("api", path, extra))
        if "issues?state=open" in path:
            return self.issues
        if path.endswith("/issues") and "POST" in extra:
            return {"number": 900}
        return None

    def posts(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "api" and "POST" in c[2]]


def files(tmp_path: Path, stages: list[dict]) -> tuple[Path, Path, Path]:
    real, detail, mock = (tmp_path / n for n in ("real.json", "detail.json", "mock.json"))
    for path in (real, mock):  # mock 결과는 같은 단계 표의 unit·chain만 본다
        path.write_text(json.dumps({"stages": stages}), encoding="utf-8")
    info = {"detect": "mock", "stages": [{"name": s["name"], "folder": "data"} for s in stages]}
    detail.write_text(json.dumps(info), encoding="utf-8")
    return real, detail, mock


def test_switch_off_does_nothing(monkeypatch, tmp_path: Path) -> None:
    gh = Gh(monkeypatch)
    r, d, m = files(tmp_path, [stage(1, "a", "data", "pass", "pass", "broken")])
    monkeypatch.delenv("AX_REAL_RUN", raising=False)
    assert "on이 아님" in follow.act(r, d, m, "c" * 40)[0]
    monkeypatch.setenv("AX_REAL_RUN", "off")
    assert "on이 아님" in follow.act(r, d, m, "c" * 40)[0]
    assert gh.calls == []


def test_mock_pass_real_fail_files_issue_with_labels(monkeypatch, tmp_path: Path) -> None:
    gh = Gh(monkeypatch)
    monkeypatch.setenv("AX_REAL_RUN", "on")
    stages = [
        stage(1, "impact", "data", "pass", "pass", "broken", "http 401"),
        stage(2, "loss", "finance", "pass", "pass", "pending"),  # real 아직 없음
        stage(3, "notice", "marketing", "broken", "broken", "broken"),  # mock도 실패
        stage(4, "record", "hr", "pass", "pass", "pass"),
    ]
    r, d, m = files(tmp_path, stages)
    out = follow.act(r, d, m, "a" * 40)
    posts = gh.posts()
    assert len(posts) == 1 and out == ["impact: #900 이슈"]
    sent = posts[0][2]
    for label in ("from:real-run", "bug", "dept:data"):
        assert f"labels[]={label}" in sent
    body = next(x for x in sent if isinstance(x, str) and x.startswith("body="))
    assert "<!-- autopilot -->" in body and "<!-- real-run:impact -->" in body
    assert "http 401" in body


def test_existing_open_issue_gets_comment_not_duplicate(monkeypatch, tmp_path: Path) -> None:
    gh = Gh(monkeypatch, issues=[{"number": 41, "body": follow.marker_of("impact")}])
    monkeypatch.setenv("AX_REAL_RUN", "on")
    r, d, m = files(tmp_path, [stage(1, "impact", "data", "pass", "pass", "regressed", "timeout")])
    out = follow.act(r, d, m, "b" * 40)
    assert gh.posts() == [] and out == ["impact: #41 댓글"]
    assert any(c[0] == "say" and c[1] == 41 and "<!-- autopilot -->" in c[2] for c in gh.calls)


def test_no_mock_result_means_no_issue(monkeypatch, tmp_path: Path) -> None:
    gh = Gh(monkeypatch)
    monkeypatch.setenv("AX_REAL_RUN", "on")
    r, d, _ = files(tmp_path, [stage(1, "a", "data", "pass", "pass", "broken")])
    out = follow.act(r, d, tmp_path / "missing.json", "c" * 40)
    assert "비교할 수 없어" in out[0] and gh.calls == []


# ── 감사 반영: 라벨 우회, 상시 스택 분리, 재실행·ref·SHA 고정 ─────────
def test_self_hosted_outside_allowlist_fails_even_without_label(tmp_path: Path) -> None:
    write_wf(tmp_path, "ci.yml", "on: push\njobs:\n  a:\n    runs-on: [self-hosted, other]\n")
    found = guard.check_workflows(tmp_path)
    assert any("ci.yml" in v and "self-hosted" in v for v in found)


@pytest.mark.parametrize(
    "runs_on",
    [
        "runs-on: ${{ vars.RUNNER }}",
        "runs-on: [self-hosted, \"${{ vars.LABEL }}\"]",
        "runs-on:\n      - ${{ fromJSON(vars.LABELS) }}",
    ],
)
def test_expression_in_runs_on_fails_everywhere(tmp_path: Path, runs_on: str) -> None:
    write_wf(tmp_path, "ci.yml", f"on: push\njobs:\n  a:\n    {runs_on}\n")
    write_wf(tmp_path, "real-run.yml", SCHEDULE_ON + f"jobs:\n  a:\n    {runs_on}\n")
    found = guard.check_workflows(tmp_path)
    assert sum("runs-on에 식" in v for v in found) == 2


def test_expression_elsewhere_is_fine(tmp_path: Path) -> None:
    lines = ["on: push", "jobs:", "  a:", "    runs-on: ubuntu-latest", "    steps:",
             "      - run: echo ${{ github.sha }}"]
    write_wf(tmp_path, "ci.yml", "\n".join(lines) + "\n")
    assert guard.check_workflows(tmp_path) == []


def test_compose_project_is_separated_from_standing_stack() -> None:
    assert run_mod.COMPOSE[:4] == ["docker", "compose", "-p", "ax-real"]
    assert "ax-backend" not in run_mod.COMPOSE
    assert run_mod.STANDING_PS[3] == "ax-backend" and run_mod.STANDING_PS[-2:] == ["ps", "-q"]


def test_standing_stack_running_makes_run_fail_without_touching_compose(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(run_mod, "standing_stack_running", lambda: True)
    monkeypatch.setattr(run_mod, "run_cmd", lambda cmd, **kw: calls.append(cmd) or 0)
    monkeypatch.setattr(run_mod.shutil, "which", lambda name: name)
    args = run_mod.argparse.Namespace(runs="1", out=str(tmp_path / "o"), sha="x")
    assert run_mod.cmd_run(args) == 1
    assert calls == []  # up도 down -v도 부르지 않는다 (상시 스택 보호)
    detail = json.loads((tmp_path / "o" / "real-detail.json").read_text(encoding="utf-8"))
    assert "상시 스택" in detail["notes"][0] and "포트 충돌" in detail["notes"][0]


def test_standing_stack_check_reads_ps_output(monkeypatch) -> None:
    class Done:
        def __init__(self, out: str, code: int = 0) -> None:
            self.stdout = out
            self.returncode = code

    monkeypatch.setattr(run_mod.subprocess, "run", lambda *a, **k: Done("abc123\n"))
    assert run_mod.standing_stack_running() is True
    monkeypatch.setattr(run_mod.subprocess, "run", lambda *a, **k: Done(""))
    assert run_mod.standing_stack_running() is False


def test_standing_stack_check_fails_closed(monkeypatch) -> None:
    """확인이 실패하면(종료 코드 0 아님, 예외) 떠 있는 것으로 보고 실행하지 않는다."""

    class Done:
        stdout = ""
        returncode = 1

    monkeypatch.setattr(run_mod.subprocess, "run", lambda *a, **k: Done())
    assert run_mod.standing_stack_running() is True

    def boom(*a, **k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(run_mod.subprocess, "run", boom)
    assert run_mod.standing_stack_running() is True


def test_time_budget_fits_job_timeout_with_three_runs() -> None:
    text = (ROOT / ".github" / "workflows" / "real-run.yml").read_text(encoding="utf-8")
    assert "timeout-minutes: 240" in text
    assert 3 * run_mod.RUN_MAX_SECONDS <= 240 * 60 - 15 * 60
    assert run_mod.TOTAL_BUDGET_SECONDS < 240 * 60


def test_workflow_hardening_flags() -> None:
    text = (ROOT / ".github" / "workflows" / "real-run.yml").read_text(encoding="utf-8")
    assert "github.actor" not in text.replace("github.triggering_actor", "")
    assert text.count("github.triggering_actor") >= 2  # guard + 서버 PC job
    assert text.count("github.ref == ") >= 2  # guard·run 모두 dev ref만
    assert "github.sha" not in text.split("  follow:")[0]  # 서버 PC 쪽은 checkout HEAD를 쓴다
    assert "always()" in text and "-p ax-real" in text
    for line in text.splitlines():
        if "uses: actions/" in line:
            ref = line.split("@", 1)[1].split()[0]
            assert len(ref) == 40, line  # 커밋 SHA로 고정
    assert text.count("timeout-minutes:") == 3
