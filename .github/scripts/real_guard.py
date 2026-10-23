#!/usr/bin/env python3
"""실서버(self-hosted runner) 보안 검사. 표준 라이브러리만 쓴다.

  real_guard.py check-actor --actor LOGIN --event NAME [--codeowners PATH]
      workflow_dispatch를 일으킨 사람이 code owner(.github/ 소유자)인지 본다. schedule은 통과한다.
  real_guard.py check-runner-label [--workflows DIR]
      `ax-server` 라벨은 허용 목록의 워크플로만 쓸 수 있다. 그 밖에서 쓰면 실패한다.
      (public 레포에서 다른 워크플로·PR이 서버 PC에서 도는 것을 막는 장치)

종료 코드 0 통과 / 1 위반.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER_LABEL = "ax-server"
SELF_HOSTED = "self-hosted"
# 서버 PC runner를 쓸 수 있는 워크플로 (devroom-update는 세션 E가 추가한다)
ALLOWED_WORKFLOWS = frozenset({"real-run.yml", "devroom-update.yml"})
# 이 트리거가 있는 워크플로는 허용 목록이어도 서버 PC를 쓸 수 없다 (외부 PR 코드가 서버에서 도는 길)
FORBIDDEN_TRIGGERS = ("pull_request", "pull_request_target", "issue_comment", "issues", "fork")
GATE_PATH = "/.github/"


def code_owners(path: Path) -> set[str]:
    """CODEOWNERS에서 `/.github/` 줄의 소유자 (자동화 파일 승인자 = 서버 PC 실행 권한자)."""
    owners: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        pattern, *who = line.split()
        if pattern == GATE_PATH:
            owners = {w.lstrip("@") for w in who if w.startswith("@")}
    return owners


def actor_allowed(actor: str, event: str, owners: set[str]) -> tuple[bool, str]:
    if event == "schedule":
        return True, "schedule 실행"
    if event != "workflow_dispatch":
        return False, f"허용하지 않는 이벤트: {event}"
    if actor in owners:
        return True, f"code owner: {actor}"
    return False, f"code owner가 아님: {actor}"


def uses_label(text: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(RUNNER_LABEL)}(?![\w-])", text) is not None


def trigger_names(text: str) -> set[str]:
    """최상위 `on:` 아래 이벤트 이름 (간단한 들여쓰기 기반 읽기, yaml 의존 없음)."""
    names: set[str] = set()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^(on|\"on\"|'on')\s*:", line):
            rest = line.split(":", 1)[1].strip()
            if rest:  # on: [push, pull_request] / on: push
                names |= {x.strip(" []\"'") for x in rest.split(",") if x.strip(" []\"'")}
                break
            for nxt in lines[i + 1 :]:
                if nxt.strip() and not nxt.startswith((" ", "\t", "#")):
                    break
                m = re.match(r"^  ([A-Za-z_]+)\s*:", nxt)
                if m:
                    names.add(m.group(1))
                m = re.match(r"^  -\s*([A-Za-z_]+)", nxt)
                if m:
                    names.add(m.group(1))
            break
    return names


def runs_on_blocks(text: str) -> list[str]:
    """모든 `runs-on:` 값 (한 줄 또는 아래 들여쓴 줄까지)."""
    lines = text.splitlines()
    blocks: list[str] = []
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)runs-on\s*:(.*)$", line)
        if not m:
            continue
        indent, block = len(m.group(1)), [m.group(2)]
        for nxt in lines[i + 1 :]:
            if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent:
                break
            block.append(nxt)
        blocks.append("\n".join(block))
    return blocks


def check_workflows(directory: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")]):
        text = path.read_text(encoding="utf-8")
        if any("${{" in b for b in runs_on_blocks(text)):
            # 식으로 만든 runs-on은 라벨 검사를 우회할 수 있다
            violations.append(f"{path.name}: runs-on에 식(${{{{ }}}})을 쓸 수 없다")
        if path.name not in ALLOWED_WORKFLOWS and SELF_HOSTED in text:
            allowed = sorted(ALLOWED_WORKFLOWS)
            violations.append(f"{path.name}: `{SELF_HOSTED}`는 {allowed}만 쓸 수 있다")
            continue
        if not uses_label(text):
            continue
        if path.name not in ALLOWED_WORKFLOWS:
            violations.append(
                f"{path.name}: `{RUNNER_LABEL}` 라벨은 {sorted(ALLOWED_WORKFLOWS)}만 쓸 수 있다"
            )
            continue
        bad = sorted(trigger_names(text) & set(FORBIDDEN_TRIGGERS))
        if bad:
            violations.append(f"{path.name}: 서버 PC 워크플로에 금지된 트리거 {bad}")
    return violations


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="real_guard.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check-actor")
    p.add_argument("--actor", required=True)
    p.add_argument("--event", required=True)
    p.add_argument("--codeowners", default=str(ROOT / ".github" / "CODEOWNERS"))
    p = sub.add_parser("check-runner-label")
    p.add_argument("--workflows", default=str(ROOT / ".github" / "workflows"))
    args = ap.parse_args(argv)
    if args.cmd == "check-actor":
        ok, why = actor_allowed(args.actor, args.event, code_owners(Path(args.codeowners)))
        print(why)
        return 0 if ok else 1
    found = check_workflows(Path(args.workflows))
    for v in found:
        print(f"::error::{v}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
