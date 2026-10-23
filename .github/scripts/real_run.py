#!/usr/bin/env python3
"""실서버 실행 (real-run.yml이 서버 PC runner에서 부른다). 표준 라이브러리만 쓴다 (pwsh·bash 공통).

  real_run.py run [--runs auto|N] [--out DIR] [--sha SHA]
      Zabbix 확인 -> (run마다) compose up -> 헬스 대기 -> 결제 장애 주입 -> AX_E2E_MODE=real e2e
      -> 장애 해제 -> compose down -v. 결과는 DIR/real-result.json (DevStatus 부분: stages[].real,
      totals.real_pass·real_runs_consecutive)과 real-detail.json(사유·LLM 사용량·감지 모드).
  real_run.py record [--out DIR]
      Playwright로 `/`와 `/admin/`을 오가며 녹화한다. 게이트웨이가 없으면 조용히 건너뛴다.

규칙
- 키는 서버 PC `.env`에만 있다. 이 스크립트는 `.env`에서 `*_TOKEN`과 AX_ZABBIX_URL 이름만 골라 읽어
  e2e 프로세스·Zabbix 확인에만 넘긴다. 값은 출력하지 않고, 환경 전체를 찍지 않는다.
- compose 프로젝트는 `-p ax-real`로 상시 스택(ax-backend)과 분리한다. 상시 스택이 떠 있으면
  실행하지 않고 실패로 끝낸다. `down -v`는 ax-real 프로젝트(공통 로그 볼륨 포함)만 지운다.
- 이 스크립트는 infra/ 등 다른 부서 파일을 고치지 않는다. 장애 주입 스크립트는 읽어서 실행만 한다.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# 이 CI 실행 전용 프로젝트. infra/compose.yml의 name(ax-backend)은 서버 PC 상시 스택이 쓰므로
# -p로 분리해야 up --build가 상시 컨테이너를 덮어쓰지 않고 down -v가 상시 볼륨을 지우지 않는다
PROJECT = "ax-real"
STANDING_PROJECT = "ax-backend"
COMPOSE = ["docker", "compose", "-p", PROJECT, "-f", "infra/compose.yml"]
STANDING_PS = ["docker", "compose", "-p", STANDING_PROJECT, "-f", "infra/compose.yml", "ps", "-q"]
# CLAUDE.md 포트 표 (헬스: GET /health)
HEALTH_PORTS: dict[str, int] = {
    "orchestrator": 8000,
    "safety": 8001,
    "memory": 8002,
    "shop-api": 8010,
    "dashboard-api": 8020,
    "planning": 8101,
    "support": 8102,
    "marketing": 8103,
    "hr": 8104,
    "finance": 8105,
    "data": 8106,
    "infra": 8107,
}
HEALTH_TIMEOUT = 300.0
FAULT_DIR = Path("infra") / "fault-injection"
FAULT_SCRIPT = "payment_fault.py"  # payment_fault.py inject|clear (infra에 요청한 형식)
ZABBIX_TIMEOUT = 10.0
FRIDAY_RUNS = 3
QUOTES = "\"'"
NOTE_NO_LLM = "LLM 호출 수·토큰 수집 경로 없음 (NFR-06: #282 agent-template 요청)"
# 시간 예산: job timeout 240분 안에 금요일 3회 연속이 들어가야 한다
RUN_MAX_SECONDS = 70 * 60  # run 하나의 최악 (up 25분 + 헬스 5분 + e2e 20분 + down 10분 + 여유)
TOTAL_BUDGET_SECONDS = 220 * 60  # 다음 run이 이 안에 못 끝날 것 같으면 시작하지 않는다
UP_TIMEOUT = 25 * 60
E2E_TIMEOUT = 20 * 60
DOWN_TIMEOUT = 10 * 60


def git_head() -> str:
    """checkout된 커밋 (워크플로 sha 변수 대신 실제 HEAD)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
        )
    except OSError:
        return ""
    return out.stdout.strip()


def log(msg: str) -> None:
    print(msg, flush=True)


def kst_now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) + timedelta(hours=9)


def planned_runs(spec: str, now: datetime | None = None) -> int:
    """auto: 금요일(KST) 3회 연속, 그 밖의 날 1회. 숫자는 1~3으로 제한한다."""
    if spec == "auto":
        return FRIDAY_RUNS if kst_now(now).weekday() == 4 else 1
    return max(1, min(FRIDAY_RUNS, int(spec)))


def mask(value: str) -> None:
    """GitHub Actions 로그에서 값을 가린다 (Actions 밖에서는 아무것도 하지 않는다)."""
    if value and os.getenv("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{value}")


def read_env_file(path: Path) -> dict[str, str]:
    """.env에서 이 스크립트가 쓰는 이름만 읽는다: *_TOKEN, AX_ZABBIX_URL. 값은 가린다."""
    wanted: dict[str, str] = {}
    if not path.exists():
        return wanted
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip(QUOTES)
        if key.endswith("_TOKEN") or key == "AX_ZABBIX_URL":
            wanted[key] = value
            mask(value)
    return wanted


def zabbix_reachable(url: str | None, timeout: float = ZABBIX_TIMEOUT) -> tuple[bool, str]:
    """Zabbix API 접속 확인: apiinfo.version은 인증 없이 부를 수 있다."""
    if not url:
        return False, "AX_ZABBIX_URL이 설정되지 않음"
    endpoint = url.rstrip("/") + "/api_jsonrpc.php"
    body = json.dumps(
        {"jsonrpc": "2.0", "method": "apiinfo.version", "params": [], "id": 1}
    ).encode()
    req = urllib.request.Request(
        endpoint, data=body, headers={"Content-Type": "application/json-rpc"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (서버 PC 설정 주소)
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"접속 실패: {type(exc).__name__}"
    if "result" in data:
        return True, f"Zabbix {data['result']}"
    return False, "응답에 result 없음"


def http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_health(ports: dict[str, int], timeout: float = HEALTH_TIMEOUT, host: str = "localhost"):
    """모든 서비스의 /health가 200일 때까지 기다린다. (준비된 서비스, 못 뜬 서비스)를 돌려준다."""
    deadline = time.monotonic() + timeout
    pending = dict(ports)
    while pending and time.monotonic() < deadline:
        for name, port in list(pending.items()):
            if http_ok(f"http://{host}:{port}/health"):
                del pending[name]
        if pending:
            time.sleep(5)
    return sorted(set(ports) - set(pending)), sorted(pending)


def run_cmd(cmd: list[str], *, env: dict | None = None, timeout: float | None = None) -> int:
    """명령을 실행해 종료 코드를 돌려준다. 출력은 그대로 로그에 흐른다 (환경변수는 찍지 않는다)."""
    log("$ " + " ".join(cmd))
    try:
        return subprocess.run(cmd, cwd=ROOT, env=env, timeout=timeout, check=False).returncode
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        log(f"명령 실패: {type(exc).__name__}")
        return 1


def fault(action: str) -> tuple[bool, str]:
    """infra/fault-injection의 결제 장애 스크립트를 실행한다 (없으면 건너뜀)."""
    script = ROOT / FAULT_DIR / FAULT_SCRIPT
    if not script.exists():
        return False, f"{FAULT_DIR / FAULT_SCRIPT} 없음 (infra 요청: 결제 장애 inject/clear)"
    code = run_cmd([sys.executable, str(script), action], timeout=120)
    return code == 0, f"{action} 종료 코드 {code}"


def write_output(name: str, value: str) -> None:
    path = os.getenv("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def standing_stack_running() -> bool:
    """서버 PC 상시 스택(ax-backend)에 컨테이너가 있으면 True (덮어쓰기·포트 충돌 방지).

    확인 자체가 실패하면(예외, 종료 코드 0 아님) 떠 있는 것으로 보고 멈춘다 (fail-closed).
    """
    try:
        proc = subprocess.run(
            STANDING_PS, cwd=ROOT, capture_output=True, text=True, timeout=60, check=False
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return True
    if proc.returncode != 0:
        return True
    return bool(proc.stdout.strip())


def state_dir() -> Path:
    """직전 결과를 이어받는 서버 PC 폴더 (runner 작업 폴더 밖). 연속 횟수 계산에 쓴다."""
    return Path(os.getenv("AX_REAL_STATE_DIR") or Path.home() / ".ax-real-run")


def one_run(n: int, out: Path, sha: str, env: dict, prev: Path | None, notes: list[str]) -> dict:
    """한 번의 실서버 실행. 반환: up 여부, 못 뜬 서비스, e2e 종료 코드."""
    info: dict = {"run": n, "up": False, "unhealthy": [], "e2e_code": None}
    try:
        up = run_cmd([*COMPOSE, "up", "-d", "--build"], timeout=UP_TIMEOUT)
        info["up"] = up == 0
        if up != 0:
            notes.append(f"run {n}: compose up 실패 (종료 코드 {up})")
            return info
        _ready, unhealthy = wait_health(HEALTH_PORTS)
        info["unhealthy"] = unhealthy
        if unhealthy:
            notes.append(f"run {n}: 헬스 실패 {unhealthy} (뜬 서비스만 점수를 낸다)")
        injected, why = fault("inject")
        info["fault"] = why
        if not injected:
            notes.append(f"run {n}: 장애 주입 안 됨 - {why}")
        # e2e는 workspace 패키지가 아니라 일반 폴더라 PYTHONPATH=e2e로 실행한다
        cmd = ["uv", "run", "python", "-m", "ax_e2e.scenario_a.cli", "run"]
        cmd += ["--out", str(out), "--runs", "1", "--commit", sha]
        if prev and prev.exists():
            cmd += ["--prev-real", str(prev)]
        info["e2e_code"] = run_cmd(cmd, env=env, timeout=E2E_TIMEOUT)
        if injected:
            fault("clear")
    finally:
        # down -v: 이 CI 실행 전용 (공통 로그 볼륨까지 지운다). 실패해도 다음 단계로 간다
        run_cmd([*COMPOSE, "down", "-v", "--remove-orphans"], timeout=DOWN_TIMEOUT)
    return info


def cmd_run(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    runs = planned_runs(args.runs)
    sha = args.sha or git_head()
    if not shutil.which("docker") or not shutil.which("uv"):
        log("docker 또는 uv를 찾을 수 없음 - docs/automation/self-hosted-runner.md 확인")
        return 1
    if standing_stack_running():
        why = f"상시 스택({STANDING_PROJECT})이 떠 있음, 포트 충돌 - 실행하지 않음"
        log(why)
        (out / "real-detail.json").write_text(
            json.dumps({"notes": [why], "runs": []}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        return 1
    env_file = read_env_file(ROOT / ".env")
    ok, zabbix = zabbix_reachable(os.getenv("AX_ZABBIX_URL") or env_file.get("AX_ZABBIX_URL"))
    detect = "real" if ok else "mock"
    log(f"Zabbix: {zabbix} -> 감지: " + ("실서버" if ok else "목"))
    env = {
        **os.environ,
        **{k: v for k, v in env_file.items() if k.endswith("_TOKEN")},
        "AX_E2E_MODE": "real",
        "AX_REAL_HOST": "localhost",
        "AX_REAL_DETECT": detect,
        "PYTHONUTF8": "1",
        "PYTHONPATH": str(ROOT / "e2e"),
    }
    state = state_dir()
    state.mkdir(parents=True, exist_ok=True)
    prev: Path | None = state / "real-result.json"
    notes: list[str] = [] if ok else [f"감지: 목 (Zabbix 미접속: {zabbix})"]
    infos: list[dict] = []
    last_result: Path | None = None
    started = time.monotonic()
    for n in range(1, runs + 1):
        if n > 1 and time.monotonic() - started + RUN_MAX_SECONDS > TOTAL_BUDGET_SECONDS:
            notes.append(f"run {n}~{runs}: 시간 예산 부족으로 건너뜀")
            break
        run_out = out / f"run{n}"
        log(f"== 실서버 실행 {n}/{runs} ==")
        infos.append(one_run(n, run_out, sha, env, prev, notes))
        produced = run_out / "real-result.json"
        if produced.exists():
            prev = last_result = produced
        else:
            notes.append(f"run {n}: real-result.json 없음")
            prev = None  # 연속이 끊긴 것으로 본다
    if last_result is None:
        log("결과가 없음")
        (out / "real-detail.json").write_text(
            json.dumps({"runs": infos, "notes": notes}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        return 1
    result = json.loads(last_result.read_text(encoding="utf-8"))
    final = out / "real-result.json"
    final.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.copyfile(final, state / "real-result.json")
    detail_src = last_result.parent / "e2e-detail.json"
    detail = json.loads(detail_src.read_text(encoding="utf-8")) if detail_src.exists() else {}
    detail.update(
        {
            "runs_planned": runs,
            "run_infos": infos,
            "detect": detect,
            "zabbix": zabbix,
            "notes": notes,
            "llm_usage": {"calls": None, "tokens": None, "note": NOTE_NO_LLM},
            "common_log_volume": "down -v로 삭제됨 (이 CI 실행 전용)",
        }
    )
    (out / "real-detail.json").write_text(
        json.dumps(detail, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    totals = result["totals"]
    all_pass = totals["real_pass"] == totals["total"] and totals["total"] > 0
    line = (
        f"실서버 {totals['real_pass']}/{totals['total']} · 연속 {totals['real_runs_consecutive']}회"
        + " · 감지: "
        + ("실서버" if detect == "real" else "목")
    )
    log(line)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"### 실서버 실행\n\n{line}\n\n" + "".join(f"- {x}\n" for x in notes))
    write_output("pass_day", "true" if all_pass else "false")
    write_output("rec_name", "demo-recording-" + datetime.now(UTC).strftime("%Y%m%d"))
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    """/와 /admin/을 오가며 녹화한다. 게이트웨이(PR-D)가 없거나 playwright가 없으면 건너뛴다."""
    port = os.getenv("AX_GATEWAY_PORT", "8080")
    base = f"http://localhost:{port}"
    if not (http_ok(base + "/") and http_ok(base + "/admin/")):
        log(f"녹화 건너뜀: {base}/ 또는 /admin/에 접속할 수 없음 (게이트웨이 없음)")
        write_output("recorded", "false")
        return 0
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("녹화 건너뜀: playwright 없음")
        write_output("recorded", "false")
        return 0
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        size = {"width": 1280, "height": 720}
        ctx = browser.new_context(record_video_dir=str(out), viewport=size)
        page = ctx.new_page()
        for path in ("/", "/admin/", "/", "/admin/"):
            page.goto(base + path, wait_until="load")
            page.wait_for_timeout(4000)
        ctx.close()  # 닫아야 영상 파일이 저장된다
        browser.close()
    write_output("recorded", "true")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="real_run.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--runs", default="auto", help="auto(금요일 3회, 그 밖 1회) 또는 1~3")
    p.add_argument("--out", default="real-out")
    p.add_argument("--sha")
    p = sub.add_parser("record")
    p.add_argument("--out", default="recording")
    args = ap.parse_args(argv)
    return cmd_run(args) if args.cmd == "run" else cmd_record(args)


if __name__ == "__main__":
    sys.exit(main())
