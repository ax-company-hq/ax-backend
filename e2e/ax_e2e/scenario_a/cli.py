"""`ax-e2e run`: 같은 입력을 새 프로세스로 3번 돌려 판정하고 `e2e-result.json`을 남긴다.

    ax-e2e run --out out/ [--prev prev/e2e-result.json] [--commit SHA] [--runs 3]

종료 코드는 점수와 무관하다 (점수는 보고용). 실행 자체가 깨졌을 때만 0이 아니다.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ax_contracts.data_access.dev_status import DevStatus
from ax_e2e.scenario_a import harness
from ax_e2e.scenario_a.judge import RUNS_REQUIRED, judge, regressions
from ax_e2e.scenario_a.stages import build_stage_table


def run_subprocess(n: int, tmp: Path) -> dict:
    out = tmp / f"run{n}.json"
    # e2e는 패키지로 설치하지 않으므로 하위 프로세스도 e2e/ 폴더에서 ax_e2e를 찾게 한다
    e2e_dir = str(Path(__file__).resolve().parents[2])
    pythonpath = os.pathsep.join(p for p in (e2e_dir, os.environ.get("PYTHONPATH", "")) if p)
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": pythonpath}
    proc = subprocess.run(
        [sys.executable, "-m", "ax_e2e.scenario_a.once", "--out", str(out)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"실행 {n}회가 깨짐:\n{proc.stderr[-2000:]}")
    return json.loads(out.read_text(encoding="utf-8"))


def summary_lines(detail: dict, status: DevStatus) -> list[str]:
    p = detail["paths"]
    return [
        f"단독 {status.totals.unit_pass}/{status.totals.total}",
        f"연결(사건) {p['incident']['chain_pass']}/{p['incident']['total']}",
        f"고객 경로 {p['customer']['chain_pass']}/{p['customer']['total']}",
    ]


def cmd_run(args: argparse.Namespace) -> int:
    mode = harness.e2e_mode()  # 잘못된 모드는 바로 실패
    stages = build_stage_table()
    prev = None
    if args.prev and Path(args.prev).exists():
        prev = DevStatus.model_validate_json(Path(args.prev).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        runs = [run_subprocess(i, Path(tmp)) for i in range(1, args.runs + 1)]
    status, detail = judge(stages, runs, prev, args.commit)
    detail.update(
        {
            "mode": mode,
            "runs": args.runs,
            "meta": runs[0]["meta"],
            "regressions": regressions(status, prev),
        }
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    result = status.model_dump(mode="json", by_alias=True)
    (out / "e2e-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (out / "e2e-detail.json").write_text(
        json.dumps(detail, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    kind = "기준선(첫 실행)" if detail["baseline"] else f"회귀 {len(detail['regressions'])}건"
    print(f"SCN-A e2e [{mode}] {' · '.join(summary_lines(detail, status))} · {kind}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ax-e2e")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--out", required=True)
    p.add_argument("--prev")
    p.add_argument("--commit")
    p.add_argument("--runs", type=int, default=RUNS_REQUIRED)
    return cmd_run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
