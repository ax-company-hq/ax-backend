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
    assert "always()" in text
    assert "real_run.py cleanup" in text  # down -v는 스크립트(COMPOSE)가 한다
    for line in text.splitlines():
        if "uses: actions/" in line:
            ref = line.split("@", 1)[1].split()[0]
            assert len(ref) == 40, line  # 커밋 SHA로 고정
    assert text.count("timeout-minutes:") == 3


# ── AX_REAL_ENV_FILE: 서버 PC .env를 작업 폴더로 잠깐 복사 ─────────────
SENT_TOKEN = "SENTINEL_TOKEN_abc123"
SENT_LLM = "SENTINEL_LLM_KEY_xyz789"
SENT_ZBX = "http://SENTINEL-zabbix-host.invalid:9"
SENTINELS = (SENT_TOKEN, SENT_LLM, "SENTINEL-zabbix-host")
ENV_BODY = f"SUPPORT_TOKEN={SENT_TOKEN}\nLLM_API_KEY={SENT_LLM}\nAX_ZABBIX_URL={SENT_ZBX}\n"


@pytest.fixture
def space(monkeypatch, tmp_path: Path):
    """workspace(작업 폴더)와 그 밖의 서버 PC 폴더를 만들고 ROOT를 workspace로 바꾼다."""
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "server"
    outside.mkdir()
    (outside / ".env").write_text(ENV_BODY, encoding="utf-8")
    monkeypatch.setattr(run_mod, "ROOT", ws)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(ws))
    monkeypatch.setenv("AX_REAL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("AX_REAL_ENV_FILE", str(outside / ".env"))
    return ws, outside


def test_env_file_outside_workspace_is_accepted(space) -> None:
    _ws, outside = space
    assert run_mod.resolve_env_source(str(outside / ".env")) == (outside / ".env").resolve()


def test_env_file_inside_workspace_is_rejected(space) -> None:
    ws, _ = space
    inner = ws / "sub" / ".env"
    inner.parent.mkdir()
    inner.write_text(ENV_BODY, encoding="utf-8")
    with pytest.raises(run_mod.EnvFileError) as err:
        run_mod.resolve_env_source(str(inner))
    assert "작업 폴더 안" in str(err.value) and str(inner) not in str(err.value)


def test_env_file_missing_unset_relative_and_directory_are_rejected(space) -> None:
    _ws, outside = space
    for raw in (None, "", ".env", str(outside / "nope.env"), str(outside)):
        with pytest.raises(run_mod.EnvFileError):
            run_mod.resolve_env_source(raw)


def test_env_file_symlink_is_rejected(space) -> None:
    ws, outside = space
    (ws / "real.env").write_text(ENV_BODY, encoding="utf-8")
    link = outside / "link.env"
    try:
        link.symlink_to(ws / "real.env")
    except OSError:
        pytest.skip("symlink 권한 없음")
    with pytest.raises(run_mod.EnvFileError):
        run_mod.resolve_env_source(str(link))
    other = outside / "target.env"
    other.write_text(ENV_BODY, encoding="utf-8")
    link2 = outside / "link2.env"
    link2.symlink_to(other)
    with pytest.raises(run_mod.EnvFileError):
        run_mod.resolve_env_source(str(link2))


def fake_run(monkeypatch, calls: list | None = None, boom: bool = False) -> None:
    """docker, uv, 네트워크 없이 cmd_run을 돌린다. one_run은 결과 파일만 만든다."""
    monkeypatch.setattr(run_mod.shutil, "which", lambda name: name)
    monkeypatch.setattr(run_mod, "standing_stack_running", lambda: False)
    monkeypatch.setattr(
        run_mod, "zabbix_reachable", lambda url, timeout=10.0: (False, "접속 실패: X")
    )
    monkeypatch.setattr(run_mod, "git_head", lambda: "abc")

    def one_run(n, out, sha, env, prev, notes):
        if calls is not None:
            calls.append(((run_mod.ROOT / ".env").exists(), dict(env)))
        if boom:
            raise RuntimeError("의도된 예외")
        out.mkdir(parents=True, exist_ok=True)
        totals = {"real_pass": 1, "total": 1, "real_runs_consecutive": 1}
        (out / "real-result.json").write_text(json.dumps({"totals": totals}), encoding="utf-8")
        return {"run": n, "up": True, "unhealthy": [], "e2e_code": 0}

    monkeypatch.setattr(run_mod, "one_run", one_run)


def test_copy_exists_during_run_and_is_deleted_after_success(space, monkeypatch, tmp_path) -> None:
    ws, _ = space
    calls: list = []
    fake_run(monkeypatch, calls)
    args = run_mod.argparse.Namespace(runs="1", out=str(tmp_path / "o"), sha="x")
    assert run_mod.cmd_run(args) == 0
    assert calls[0][0] is True  # compose up 시점에는 .env가 있다
    assert calls[0][1]["SUPPORT_TOKEN"] == SENT_TOKEN  # e2e에는 *_TOKEN만 넘어간다
    assert "LLM_API_KEY" not in calls[0][1]
    assert not (ws / ".env").exists()


def test_copy_is_deleted_after_exception(space, monkeypatch, tmp_path) -> None:
    ws, _ = space
    fake_run(monkeypatch, boom=True)
    args = run_mod.argparse.Namespace(runs="1", out=str(tmp_path / "o"), sha="x")
    with pytest.raises(RuntimeError):
        run_mod.cmd_run(args)
    assert not (ws / ".env").exists()


def test_bad_env_path_fails_without_running_compose(space, monkeypatch, tmp_path, capsys) -> None:
    ws, _ = space
    calls: list = []
    fake_run(monkeypatch, calls)
    inner = ws / "x.env"
    inner.write_text(ENV_BODY, encoding="utf-8")
    monkeypatch.setenv("AX_REAL_ENV_FILE", str(inner))
    args = run_mod.argparse.Namespace(runs="1", out=str(tmp_path / "o"), sha="x")
    assert run_mod.cmd_run(args) == 1
    assert calls == []
    shown = capsys.readouterr()
    assert "작업 폴더 안" in shown.out and str(inner) not in shown.out + shown.err
    assert not (ws / ".env").exists()


def test_cleanup_command_needs_no_env_copy_and_leaves_none(space, monkeypatch) -> None:
    ws, _ = space
    (ws / ".env").write_text("stale", encoding="utf-8")  # 남은 파일도 정리 끝에 지운다
    seen: list = []

    def run_cmd(cmd, **kw):
        seen.append(((ws / ".env").exists(), cmd))
        raise RuntimeError("compose 실패")

    monkeypatch.setattr(run_mod, "run_cmd", run_cmd)
    with pytest.raises(RuntimeError):
        run_mod.cmd_cleanup(run_mod.argparse.Namespace())
    assert "down" in seen[0][1] and "ax-real" in seen[0][1]
    assert not (ws / ".env").exists()


def test_env_values_never_reach_logs_or_result_files(space, monkeypatch, tmp_path, capsys) -> None:
    _ws, outside = space
    fake_run(monkeypatch)
    out = tmp_path / "o"
    assert run_mod.cmd_run(run_mod.argparse.Namespace(runs="1", out=str(out), sha="x")) == 0
    # 녹화 단계 (게이트웨이 없음): 출력 파일과 로그
    gh_out = tmp_path / "gh_out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    monkeypatch.setattr(run_mod, "http_ok", lambda url, timeout=3.0: False)
    assert run_mod.cmd_record(run_mod.argparse.Namespace(out=str(tmp_path / "rec"))) == 0
    shown = capsys.readouterr()
    texts = [shown.out, shown.err, gh_out.read_text(encoding="utf-8")]
    for name in ("real-result.json", "real-detail.json"):
        texts.append((out / name).read_text(encoding="utf-8"))
    texts.extend(p.read_text(encoding="utf-8") for p in (tmp_path / "state").glob("*.json"))
    blob = "\n".join(texts)
    for secret in SENTINELS:
        assert secret not in blob
    assert str(outside) not in blob  # 절대 경로도 남기지 않는다
    assert "env 파일: 작업 폴더 밖, 확인됨" in shown.out
    # 개발실 status.json 생성기는 이 레포에 없다. 이 실행이 쓰는 산출물만 확인한다


def test_masks_values_only_inside_actions(space, monkeypatch, capsys) -> None:
    _, outside = space
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    run_mod.read_env_file(outside / ".env")
    out = capsys.readouterr().out
    assert f"::add-mask::{SENT_TOKEN}" in out and SENT_LLM not in out


def test_workflow_cleans_env_and_locks_deps() -> None:
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "real-run.yml").read_text("utf-8"))
    steps = wf["jobs"]["run"]["steps"]
    cleanup = [s for s in steps if "unlink" in s.get("run", "") and ".env" in s["run"]]
    assert len(cleanup) == 1 and cleanup[0]["if"] == "always()"
    assert "continue-on-error" not in cleanup[0]
    assert steps.index(cleanup[0]) == len(steps) - 1  # compose 정리 뒤 마지막
    down = [s for s in steps if "real_run.py cleanup" in s.get("run", "")]
    assert len(down) == 1 and down[0]["if"] == "always()"
    sync = [s["run"] for s in steps if s.get("run", "").startswith("uv sync")]
    assert sync == ["uv sync --all-packages --locked"]
    for name, job in wf["jobs"].items():  # Windows 기본 셸은 pwsh라 bash 문법이 깨진다
        if "self-hosted" in job["runs-on"]:
            assert job["defaults"]["run"]["shell"] == "bash", name
    assert "AX_REAL_ENV_FILE" not in json.dumps(wf)  # 경로는 runner 환경에서만 온다


# ── 루프백 포트 override ───────────────────────────────────
LOOPBACK = ROOT / ".github" / "real-run" / "compose.loopback.yml"


def test_loopback_override_covers_all_ports_with_127() -> None:
    yaml = pytest.importorskip("yaml")
    text = LOOPBACK.read_text(encoding="utf-8")
    assert "!override" in text
    services = yaml.load(text.replace("!override", ""), Loader=yaml.BaseLoader)["services"]
    ports = {k: v for k, v in run_mod.HEALTH_PORTS.items() if k != "infra"}
    ports.update({"infra-dejafix": 8107, "redis": 6379, "postgres": 5432})
    assert set(services) == set(ports)
    for name, svc in services.items():
        assert svc["ports"] == [f"127.0.0.1:{ports[name]}:{ports[name]}"], name
    assert LOOPBACK.as_posix().endswith(run_mod.LOOPBACK)
    assert run_mod.COMPOSE[-2:] == ["-f", run_mod.LOOPBACK]


def test_compose_config_publishes_only_on_loopback() -> None:
    import shutil
    import subprocess

    if not shutil.which("docker"):
        pytest.skip("docker 없음")
    cmd = [*run_mod.COMPOSE, "config", "--format", "json"]
    try:
        proc = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("docker compose 실행 불가")
    if proc.returncode != 0:
        pytest.skip("compose config 실패 (docker compose 미지원 환경)")
    services = json.loads(proc.stdout)["services"].values()
    ports = [p for svc in services for p in svc.get("ports", [])]
    assert ports and all(p.get("host_ip") == "127.0.0.1" for p in ports)
