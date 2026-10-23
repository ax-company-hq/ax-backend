#!/usr/bin/env python3
"""실서버 결과 후속 처리: mock에서는 통과하는데 실서버에서 실패한 단계 -> 담당 부서 이슈.

  real_follow.py prev-mock --out DIR   dev 마지막 성공 scenario-a의 e2e-result 아티팩트를 받는다
  real_follow.py act --real F --detail F --mock F --sha S

스위치(repo 변수) AX_REAL_RUN이 on이 아니면 아무것도 하지 않는다. LLM을 부르지 않는다.
이슈: 라벨 dept:<부서> bug from:real-run, 같은 단계의 열린 이슈가 있으면 댓글만 단다.
봇 본문·댓글에는 `<!-- autopilot -->` 표시를 붙인다 (표시가 있는 이벤트는 무시한다).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_follow as ef  # noqa: E402
import team as team_mod  # noqa: E402

MARK = ef.MARK
LABEL = "from:real-run"
FAIL_STATES = ("broken", "regressed")


def switch_on() -> bool:
    return os.environ.get("AX_REAL_RUN", "") == "on"


def marker_of(name: str) -> str:
    return f"<!-- real-run:{name} -->"


def mock_passed(mock: dict | None) -> dict[str, bool]:
    """단계 이름 -> mock에서 통과했는지 (단독 또는 연결 중 하나라도 pass)."""
    out: dict[str, bool] = {}
    for stage in (mock or {}).get("stages", []):
        states = (stage["unit"]["state"], stage["chain"]["state"])
        out[stage["name"]] = "pass" in states
    return out


def failing_stages(real: dict, mock: dict | None, detail: dict, team) -> list[dict]:
    """mock pass / real fail 단계. real이 pending(아직 없음)이면 대상이 아니다."""
    passed = mock_passed(mock)
    folders = {s["name"]: s.get("folder") for s in detail.get("stages", [])}
    found = []
    for stage in real.get("stages", []):
        real_result = stage.get("real") or {}
        if real_result.get("state") not in FAIL_STATES or not passed.get(stage["name"]):
            continue
        dept = team.resolve_dept(folders.get(stage["name"]) or "") or team.resolve_dept(
            stage["dept"]
        )
        found.append(
            {
                "name": stage["name"],
                "dept": stage["dept"],
                "team_dept": dept,
                "no": stage["no"],
                "req_ids": stage.get("req_ids") or [],
                "state": real_result["state"],
                "note": real_result.get("note"),
            }
        )
    return found


def issue_body(item: dict, sha: str, detect: str) -> str:
    req = ", ".join(item["req_ids"]) or "-"
    return "\n".join(
        [
            marker_of(item["name"]),
            MARK,
            "",
            f"실서버 실행에서 **{item['name']}** 단계가 실패했습니다 (mock e2e에서는 통과).",
            "",
            f"- 상태: {item['state']} · 담당: {item['dept']} · 요구사항: {req}",
            f"- 실행 커밋: `{sha[:10]}` · 감지: {'실서버' if detect == 'real' else '목'}",
            f"- 사유: {ef.clean(item.get('note') or '사유 없음')}",
            "",
            "mock과 실서버의 차이(환경변수·네트워크·LLM 키·compose 설정)를 먼저 의심하세요.",
            "결과 파일: 해당 실행의 `real-result` 아티팩트 (`real-detail.json`에 사유).",
        ]
    )


def find_open_issue(name: str) -> dict | None:
    items = ef.api(f"repos/{ef.repo()}/issues?state=open&labels={LABEL}&per_page=100") or []
    mark = marker_of(name)
    for item in items:
        if "pull_request" not in item and mark in (item.get("body") or ""):
            return item
    return None


def act(real_path: Path, detail_path: Path, mock_path: Path, sha: str) -> list[str]:
    if not switch_on():
        return ["AX_REAL_RUN이 on이 아님 - 아무것도 하지 않음"]
    if not mock_path.exists():
        return ["mock e2e 결과가 없음 - 비교할 수 없어 이슈를 만들지 않음"]
    real = json.loads(real_path.read_text(encoding="utf-8"))
    mock = json.loads(mock_path.read_text(encoding="utf-8"))
    detail = json.loads(detail_path.read_text(encoding="utf-8")) if detail_path.exists() else {}
    team = team_mod.Team.load()
    log: list[str] = []
    for item in failing_stages(real, mock, detail, team):
        existing = find_open_issue(item["name"])
        if existing:
            note = ef.clean(item.get("note") or "사유 없음", 200)
            ef.say(existing["number"], f"`{sha[:10]}` 실서버에서도 계속 실패합니다: {note}")
            log.append(f"{item['name']}: #{existing['number']} 댓글")
            continue
        dept = item["team_dept"]
        labels = ["bug", LABEL, *([f"dept:{dept}"] if dept else [])]
        created = ef.api(
            f"repos/{ef.repo()}/issues",
            "-X", "POST",
            "-f", f"title=[실서버 실패] {item['name']}",
            "-f", f"body={issue_body(item, sha, detail.get('detect', 'mock'))}",
            *[x for lb in labels for x in ("-f", f"labels[]={lb}")],
        )
        log.append(f"{item['name']}: #{created['number']} 이슈")
    return log or ["mock pass / real fail 단계 없음"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="real_follow.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prev-mock")
    p.add_argument("--out", required=True)
    p = sub.add_parser("act")
    p.add_argument("--real", required=True)
    p.add_argument("--detail", required=True)
    p.add_argument("--mock", required=True)
    p.add_argument("--sha", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "prev-mock":
        print(ef.find_prev("0", Path(args.out)) or "")
        return 0
    for line in act(Path(args.real), Path(args.detail), Path(args.mock), args.sha):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
