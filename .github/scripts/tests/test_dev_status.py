"""AX 개발실 status.json 생성 테스트: DevStatus 형식, history 이어붙이기, 원자적 쓰기,
비밀·본문 미포함, 스위치 off 무동작, 워크플로 보안 조건."""

import importlib.util
import json
import re
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


team_mod = load("team")
ds = load("dev_status")
guard = load("real_guard")

NOW = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
TEAM = team_mod.Team.load()
BOT = TEAM.bot_login


def stage(no: int, unit: str, chain: str) -> dict:
    return {"no": no, "name": f"단계 {no}", "dept": "orchestrator", "req_ids": ["ORC-01"],
            "unit": {"state": unit}, "chain": {"state": chain}}


E2E = {"stages": [stage(1, "pass", "pass"), stage(2, "pass", "broken"),
                  stage(3, "pending", "pending")],
       "totals": {"total": 3}, "history": [{"date": "2026-10-24", "chain_pass": 0, "real_pass": 0}]}
REAL = {"stages": [{"no": 1, "real": {"state": "pass"}}, {"no": 2, "real": {"state": "broken"}}],
        "totals": {"real_pass": 1, "real_runs_consecutive": 2}}
MORNING = {
    "signals": {"reverts_yesterday": 1, "needs_human_open": 9,
                "merge_rate": {"overall": 0.5, "by_dept": {}}, "human_interventions_yesterday": 2},
    "bottlenecks": {"caps": [{"login": "haakim7", "name": "준하", "write_used": 3,
                              "write_cap": 15, "depts": ["orchestrator"]}],
                    "wip": [], "blocked": []},
    "people": [{"who": "모두", "task": "오늘 후보 3건", "detail": None, "state": "todo",
                "url": None}],
    "orc": [{"id": "ORC-01", "title": "사건 접수", "state": "doing", "note": None, "issue": None}],
    "forecast": None,
}


B_TAIL = chr(10) + "BODY_TAIL_SENTINEL"


def issue(no: int, title: str, labels: list[str], body: str = "") -> dict:
    return {"number": no, "title": title, "body": body, "labels": [{"name": x} for x in labels],
            "html_url": f"https://github.com/ax-company-hq/ax-backend/issues/{no}",
            "updated_at": "2026-10-26T06:00:00Z", "user": {"login": "haakim7"}}


def comment(no: int, login: str, body: str, at: str) -> dict:
    return {"user": {"login": login}, "body": body, "created_at": at,
            "issue_url": f"https://api.github.com/repos/ax-company-hq/ax-backend/issues/{no}",
            "html_url": f"https://github.com/ax-company-hq/ax-backend/issues/{no}#c{at[-3:-1]}"}


def pr(no: int, login: str, ref: str, title: str) -> dict:
    return {"number": no, "title": title, "body": "PR_BODY_SENTINEL", "user": {"login": login},
            "head": {"ref": ref}, "created_at": "2026-10-26T07:00:00Z",
            "html_url": f"https://github.com/ax-company-hq/ax-backend/pull/{no}", "labels": []}


GH = {
    "issues": [
        issue(10, "환불 승인 대기", ["needs-human", "to:finance"], body="ISSUE_BODY_SENTINEL"),
        issue(11, "쿠폰 작성 대기", ["wip-wait"]),
    ],
    "prs": [pr(20, BOT, "finance/10-refund-r1", "feat(finance): refund"),
            pr(21, "haakim7", "orchestrator/relay", "feat(orchestrator): relay")],
    "comments": [
        comment(10, BOT,
                "정산 보류 3건을 확인했어요\n<!-- chat: 정산 보류 3건을 확인했어요 -->" +
                B_TAIL,
                "2026-10-26T07:30:00Z"),
        comment(10, "2taeyoon", "고객 홍길동 010-1234-5678 환불 요청 CUSTOMER_RAW_SENTINEL",
                "2026-10-26T07:40:00Z"),
        comment(10, BOT, "이 댓글은 chat 줄이 없다 NO_CHAT_SENTINEL", "2026-10-26T07:50:00Z"),
    ],
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setenv("AX_DEVROOM", "on")
    monkeypatch.setattr(team_mod, "cap_status", lambda *a, **k: {
        login: {"used": 2, "cap": 15, "ok": True, "reason": ""} for login in TEAM.members})
    monkeypatch.setattr(ds, "fetch_artifact_json", lambda *a, **k: {})


def write(dir_: Path, name: str, data: dict) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return dir_


def generate(tmp_path: Path, gh: dict | None = None, now: datetime = NOW, e2e=E2E, real=REAL,
             morning=MORNING) -> tuple[int, dict]:
    out = tmp_path / "data"
    rc = ds.run(TEAM, out, write(tmp_path / "e2e", "e2e-result.json", e2e),
                write(tmp_path / "real", "real-result.json", real),
                write(tmp_path / "morning", "morning.json", morning), now=now,
                gh=GH if gh is None else gh)
    target = out / "status.json"
    return rc, json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}


# ── 형식 · 내용 ──────────────────────────────────────────
def test_output_validates_against_dev_status_model(tmp_path: Path) -> None:
    pytest.importorskip("ax_contracts")
    from ax_contracts.data_access.dev_status import DevStatus

    rc, status = generate(tmp_path)
    assert rc == 0
    model = DevStatus.model_validate(status)
    assert model.totals.unit_pass == 2 and model.totals.chain_pass == 1
    assert model.totals.real_pass == 1 and model.totals.real_runs_consecutive == 2
    assert model.stages[0].real is not None and model.stages[2].real is None
    assert model.generated_at.tzinfo is not None
    assert all(f.from_ for f in model.feed)


def test_sources_are_merged(tmp_path: Path) -> None:
    _, s = generate(tmp_path)
    assert s["orc"][0]["id"] == "ORC-01"
    assert s["signals"]["reverts_yesterday"] == 1
    assert s["signals"]["needs_human_open"] == 1  # 아침 값(9)이 아니라 지금 열린 needs-human
    kinds = {b["kind"] for b in s["bottlenecks"]["blocked"]}
    assert kinds == {"decision", "wip"}
    assert s["bottlenecks"]["wip"] == [{"dept": "finance", "open_bot_prs": 1, "limit": 2}]
    assert {c["login"] for c in s["bottlenecks"]["caps"]} == set(TEAM.members)
    assert all(c["write_used"] == 2 for c in s["bottlenecks"]["caps"])
    who = {p["who"] for p in s["people"]}
    assert {"모두", "태윤", "준하"} <= who
    assert any(p["state"] == "problem" for p in s["people"])


def test_feed_uses_chat_lines_and_titles_only(tmp_path: Path) -> None:
    _, s = generate(tmp_path)
    feed = s["feed"]
    assert [f["at"] for f in feed] == sorted((f["at"] for f in feed), reverse=True)
    assert any(f["kind"] == "agent" and f["text"] == "정산 보류 3건을 확인했어요" for f in feed)
    human = [f for f in feed if f["kind"] == "human"]
    assert any(f["from"] == "태윤" and "#10 환불 승인 대기" in f["text"] for f in human)
    assert any(f["kind"] == "bot" and f["to"] == "finance 에이전트" for f in feed)
    assert len(feed) <= 50


def test_without_artifacts_keeps_previous_stages(tmp_path: Path) -> None:
    generate(tmp_path)
    out = tmp_path / "data"
    rc = ds.run(TEAM, out, tmp_path / "none1", tmp_path / "none2", tmp_path / "none3",
                now=NOW.replace(hour=9), gh={})
    s = json.loads((out / "status.json").read_text(encoding="utf-8"))
    assert rc == 0 and len(s["stages"]) == 3 and s["totals"]["chain_pass"] == 1


# ── history ─────────────────────────────────────────────
def test_history_appends_and_replaces_same_day(tmp_path: Path) -> None:
    _, s1 = generate(tmp_path)
    assert [h["date"] for h in s1["history"]] == ["2026-10-24", "2026-10-26"]
    assert s1["history"][-1] == {"date": "2026-10-26", "chain_pass": 1, "real_pass": 1}
    better = {**E2E, "stages": [stage(1, "pass", "pass"), stage(2, "pass", "pass"),
                                stage(3, "pass", "pass")]}
    _, same_day = generate(tmp_path, e2e=better, now=NOW.replace(hour=10))
    assert [h["date"] for h in same_day["history"]] == ["2026-10-24", "2026-10-26"]
    assert same_day["history"][-1]["chain_pass"] == 3
    _, next_day = generate(tmp_path, e2e=better, now=datetime(2026, 10, 27, 8, 0, tzinfo=UTC))
    assert [h["date"] for h in next_day["history"]] == ["2026-10-24", "2026-10-26", "2026-10-27"]
    assert next_day["history"][1]["chain_pass"] == 3  # 지난 날짜는 그대로 남는다


# ── 원자적 쓰기 ──────────────────────────────────────────
def test_write_is_atomic_temp_then_rename(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "status.json"
    target.write_text('{"old": true}', encoding="utf-8")
    calls = []
    real_replace = ds.os.replace

    def spy(a, b):
        calls.append((Path(a), Path(b)))
        real_replace(a, b)

    monkeypatch.setattr(ds.os, "replace", spy)
    ds.write_atomic(target, {"new": True})
    ((src, dst),) = calls
    assert dst == target and src != target and src.parent == target.parent
    assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]  # 임시 파일 안 남음


def test_failed_write_keeps_old_file_and_cleans_temp(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "status.json"
    target.write_text('{"old": true}', encoding="utf-8")

    def boom(a, b):
        raise OSError("rename 실패")

    monkeypatch.setattr(ds.os, "replace", boom)
    with pytest.raises(OSError):
        ds.write_atomic(target, {"new": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]


def test_invalid_status_does_not_overwrite_existing_file(tmp_path: Path) -> None:
    pytest.importorskip("ax_contracts")
    generate(tmp_path)
    target = tmp_path / "data" / "status.json"
    before = target.read_text(encoding="utf-8")
    bad = {**E2E, "stages": [stage(1, "exploded", "pass")]}
    rc, _ = generate(tmp_path, e2e=bad, now=NOW.replace(hour=11))
    assert rc == 1 and target.read_text(encoding="utf-8") == before


# ── 비밀 · 원문 ──────────────────────────────────────────
def test_no_secrets_or_bodies_in_status_json(tmp_path: Path, monkeypatch) -> None:
    sentinels = ["ghp_SENTINELTOKEN1234567890", "sk-SENTINELKEY1234567890",
                 "ENVFILE_SENTINEL_VALUE"]
    monkeypatch.setenv("GH_TOKEN", sentinels[0])
    monkeypatch.setenv("GEMINI_API_KEY", sentinels[1])
    monkeypatch.setenv("AX_BOT_PRIVATE_KEY", "PRIVATE_KEY_SENTINEL")
    (tmp_path / ".env").write_text(f"SECRET={sentinels[2]}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    leak = f"<!-- chat: 토큰 {sentinels[0]} 와 {sentinels[1]} 를 봤다 -->"
    gh = {**GH, "comments": [*GH["comments"], comment(10, BOT, leak, "2026-10-26T07:55:00Z")]}
    rc, _ = generate(tmp_path, gh=gh)
    text = (tmp_path / "data" / "status.json").read_text(encoding="utf-8")
    assert rc == 0
    banned_all = [*sentinels, "PRIVATE_KEY_SENTINEL", "ISSUE_BODY_SENTINEL", "PR_BODY_SENTINEL",
                  "BODY_TAIL_SENTINEL", "CUSTOMER_RAW_SENTINEL", "NO_CHAT_SENTINEL",
                  "010-1234-5678"]
    for banned in banned_all:
        assert banned not in text, banned
    assert "***" in text  # chat 줄 안의 비밀 모양 문자열은 가려서 남긴다


def test_clean_masks_secret_shapes_and_limits_length() -> None:
    assert "ghp_" not in ds.clean("a ghp_abcdefghijklmnop1234 b")
    assert "Bearer" not in ds.clean("Authorization: Bearer abc.def.ghi")
    assert "@" not in ds.clean("연락 user@example.com")
    assert "http" not in ds.clean("보기 https://example.com/x?token=1")
    assert len(ds.clean("가" * 500)) == 120
    assert ds.chat_line("앞 <!-- chat: 한 줄 -->") == "한 줄"
    assert ds.chat_line("chat 줄 없음") is None


# ── 스위치 off ───────────────────────────────────────────
def test_switch_off_is_a_noop(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("AX_DEVROOM", "off")
    monkeypatch.setattr(ds, "fetch_artifact_json", lambda *a, **k: pytest.fail("조회하면 안 됨"))
    monkeypatch.setattr(ds, "load_github", lambda *a, **k: pytest.fail("조회하면 안 됨"))
    out = tmp_path / "data"
    assert ds.run(TEAM, out, repo="ax-company-hq/ax-backend") == 0
    assert not out.exists()
    assert "on이 아님" in capsys.readouterr().out


def test_team_cli_dev_status_off_needs_no_arguments(monkeypatch, capsys) -> None:
    monkeypatch.delenv("AX_DEVROOM", raising=False)
    monkeypatch.delenv("AX_DEVROOM_DIR", raising=False)
    assert team_mod.main(["dev-status"]) == 0
    assert "on이 아님" in capsys.readouterr().out


def test_team_cli_dev_status_on_without_dir_fails(monkeypatch) -> None:
    monkeypatch.setenv("AX_DEVROOM", "on")
    monkeypatch.delenv("AX_DEVROOM_DIR", raising=False)
    assert team_mod.main(["dev-status"]) == 1


def test_team_cli_dev_status_writes_to_env_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AX_DEVROOM_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    e2e_dir = write(tmp_path / "e", "e2e-result.json", E2E)
    assert team_mod.main(["dev-status", "--e2e", str(e2e_dir)]) == 0
    assert (tmp_path / "data" / "status.json").is_file()


# ── 워크플로 보안 조건 ───────────────────────────────────
WORKFLOW = ROOT / ".github" / "workflows" / "devroom-update.yml"


def triggers() -> set[str]:
    return guard.trigger_names(WORKFLOW.read_text(encoding="utf-8"))


def test_workflow_triggers() -> None:
    assert triggers() == {"workflow_run", "schedule", "workflow_dispatch"}
    assert not triggers() & set(guard.FORBIDDEN_TRIGGERS)
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflows: [scenario-a, real-run, morning]" in text
    assert "types: [completed]" in text
    assert 'cron: "*/30 * * * *"' in text


def test_workflow_runs_only_for_dev_and_is_gated_by_switch() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count("vars.AX_DEVROOM == 'on'") == 2  # guard, update 모두
    assert text.count("github.ref == 'refs/heads/dev'") == 2
    assert text.count("github.event.workflow_run.event != 'pull_request'") == 2
    assert text.count("head_repository.full_name == github.repository") == 2
    assert text.count("ref: dev") == 2
    assert "needs: guard" in text and text.count("timeout-minutes") == 2
    assert "[self-hosted, ax-server]" in text
    assert "uv sync --all-packages --locked" in text
    assert "real_guard.py check-actor" in text and "github.triggering_actor" in text


def test_workflow_has_no_secrets_no_llm_and_pinned_actions() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in text and "environment:" not in text
    uses = re.findall(r"^\s*-?\s*uses:\s*(\S+)", text, re.M)
    assert uses
    for u in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u), u
    assert not re.search(r"^\s+[\w-]+: write\s*$", text, re.M)  # 쓰기 권한 없음
    assert "anthropic" not in text.lower() and "claude" not in text.lower()


def test_workflow_passes_runner_label_guard_and_actor_rules(tmp_path: Path) -> None:
    assert guard.check_workflows(ROOT / ".github" / "workflows") == []
    assert guard.main(["check-runner-label"]) == 0
    owners = {"haakim7"}
    assert guard.actor_allowed("anyone", "workflow_run", owners)[0]
    assert guard.actor_allowed("haakim7", "workflow_dispatch", owners)[0]
    assert not guard.actor_allowed("stranger", "workflow_dispatch", owners)[0]
    assert not guard.actor_allowed("haakim7", "pull_request", owners)[0]
    # pull_request가 끼면 허용 목록이어도 가드가 막는다
    bad = tmp_path / "devroom-update.yml"
    patched = WORKFLOW.read_text(encoding="utf-8").replace(
        "  workflow_dispatch:\n", "  workflow_dispatch:\n  pull_request:\n")
    bad.write_text(patched, encoding="utf-8")
    assert any("pull_request" in v for v in guard.check_workflows(tmp_path))


def test_compose_is_separate_and_read_only() -> None:
    text = (ROOT / "devroom" / "compose.yml").read_text(encoding="utf-8")
    assert "${AX_DEVROOM_PORT:-8090}:80" in text
    assert "restart: unless-stopped" in text
    assert re.search(r"\$\{AX_DEVROOM_DIR:\?[^}]*\}:/data:ro", text)
    assert not (ROOT / "infra" / "devroom.compose.yml").exists()
