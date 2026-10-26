"""한 번 실행: 단독 + 연결(총괄 12) + 고객 경로(단독 5·연결 5)를 돌려 원시 결과 JSON을 남긴다.

    python -m ax_e2e.scenario_a.once --out run1.json

프로세스마다 부서 앱 상태가 새로 시작되도록 반복 실행(3회)은 이 모듈을 매번 새 프로세스로 부른다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from ax_e2e.scenario_a import customer, harness, unit
from ax_e2e.scenario_a.result import Outcome
from ax_e2e.scenario_a.stages import INCIDENT, Stage, build_stage_table


def _k(stage: Stage) -> str:
    return f"{stage.path}:{stage.key}"


def run_once(mode: str | None = None) -> dict:
    mode = mode or harness.e2e_mode()
    stages = build_stage_table()
    unit_out: dict[str, Outcome] = {}
    chain_out: dict[str, Outcome] = {}
    meta: dict = {"mode": mode}
    if mode == "real" and harness.real_host():
        return _run_real(stages, harness.real_host() or "localhost")
    if mode == "real":
        # AX_REAL_HOST가 없으면 실서버가 없는 것으로 보고 모든 단계를 pending으로 표시한다.
        for s in stages:
            o = Outcome("absent", "real 호출부 없음 (PR-C 이후)")
            unit_out[_k(s)], chain_out[_k(s)] = o, o
        return _pack(stages, unit_out, chain_out, meta)

    workdir = harness.new_workdir()
    harness.prepare_env(workdir)
    apps = harness.load_apps()
    harness.install_router()
    meta["apps_loaded"] = sorted(apps.apps)
    meta["app_errors"] = apps.errors

    from ax_e2e.scenario_a import chain

    inc_unit, outputs = asyncio.run(unit.run_incident_units(stages, apps))
    inc_chain, view, calls = chain.run_chain(stages, apps)
    notice_unit = (outputs.get("publish_notice") or {}).get("notice_id")
    notice_chain = _notice_id(view)
    cust_unit = asyncio.run(customer.run_unit(stages, apps, notice_unit))
    cust_chain = asyncio.run(
        customer.run_chain(stages, apps, notice_chain, (view or {}).get("context_id"))
    )
    for s in stages:
        src_u, src_c = (inc_unit, inc_chain) if s.path == INCIDENT else (cust_unit, cust_chain)
        unit_out[_k(s)], chain_out[_k(s)] = src_u[s.key], src_c[s.key]
    meta["incident"] = {
        "context_id": (view or {}).get("context_id"),
        "status": (view or {}).get("status"),
        "calls": len(calls),
    }
    return _pack(stages, unit_out, chain_out, meta)


DETECT_STAGE = "diagnose"  # 감지·진단 (DJF-01): AX_REAL_DETECT=mock이면 "감지: 목" 표시
CHAIN_PENDING = "real 연결 점수는 총괄 실서버 연결 뒤 (지금은 단독만)"


def _run_real(stages: list[Stage], host: str) -> dict:
    """실서버(compose가 호스트에 연 포트)로 단독 점수를 낸다. 연결 점수는 아직 pending."""
    import os

    harness.prepare_real_env(host)
    detect_mock = os.getenv("AX_REAL_DETECT") == "mock"
    meta: dict = {"mode": "real", "host": host, "detect": "mock" if detect_mock else "real"}
    apps = harness.Apps()  # 프로세스 안 앱 없음: 모든 호출이 HTTP
    inc_unit, outputs = asyncio.run(unit.run_incident_units(stages, apps))
    notice_unit = (outputs.get("publish_notice") or {}).get("notice_id")
    cust_unit = asyncio.run(customer.run_unit(stages, apps, notice_unit))
    unit_out: dict[str, Outcome] = {}
    chain_out: dict[str, Outcome] = {}
    for s in stages:
        src = inc_unit if s.path == INCIDENT else cust_unit
        out = src[s.key]
        if detect_mock and s.path == INCIDENT and s.key == DETECT_STAGE:
            out = Outcome(out.state, f"{out.note} (감지: 목)".strip(), out.blocked_by, out.checks)
        unit_out[_k(s)] = out
        chain_out[_k(s)] = Outcome("absent", CHAIN_PENDING)
    return _pack(stages, unit_out, chain_out, meta)


def _notice_id(view: dict | None) -> str | None:
    for step in (view or {}).get("steps", []):
        if step["step_id"] == "publish_notice" and step["status"] == "completed":
            return (step.get("output") or {}).get("notice_id")
    return None


def _pack(stages, unit_out, chain_out, meta) -> dict:
    return {
        "meta": meta,
        "unit": {k: v.to_json() for k, v in unit_out.items()},
        "chain": {k: v.to_json() for k, v in chain_out.items()},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    result = run_once()
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
