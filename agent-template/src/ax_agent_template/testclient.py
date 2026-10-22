"""테스트 클라이언트 CLI: 통합 에이전트 없이 A2A 요청 하나를 보내고 결과를 출력한다.

    uv run ax-test-call --agent http://localhost:8106 --skill impact_analysis --input input.json

기본으로 `orchestrator`인 것처럼 요청한다(`--as`). 목 모드에서는 토큰이 `mock:<이름>`이다.
실제 토큰으로 보내려면 `--token`을 쓴다. 요청자가 `allowed_requesters`에 없으면 거부된다.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from ax_contracts.schemas import AgentRequest, RequestMeta


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ax-test-call", description=__doc__.split("\n")[0])
    parser.add_argument(
        "--agent", required=True, help="대상 에이전트 주소 (예: http://localhost:8106)"
    )
    parser.add_argument("--skill", required=True, help="호출할 skill id")
    parser.add_argument("--input", type=Path, help="입력 JSON 파일 (없으면 빈 입력)")
    parser.add_argument("--untrusted", type=Path, help="untrusted JSON 파일")
    parser.add_argument("--as", dest="requester", default="orchestrator", help="요청자 이름")
    parser.add_argument("--token", help="토큰 (기본: mock:<요청자>)")
    parser.add_argument("--context-id", default="ctx_req_testclient")
    parser.add_argument("--tenant-id", default="shop-demo")
    parser.add_argument("--path", nargs="*", default=[], help="path에 미리 넣을 에이전트 이름")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--sequence", type=int)
    parser.add_argument("--timeout", type=float, default=30.0, help="마감까지 초")
    return parser


def _load_json(path: Path | None) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path else {}


def build_request(args: argparse.Namespace) -> AgentRequest:
    meta = RequestMeta(
        context_id=args.context_id,
        tenant_id=args.tenant_id,
        token=args.token or f"mock:{args.requester}",
        path=list(args.path),
        deadline=datetime.now(UTC) + timedelta(seconds=args.timeout),
        idempotency_key=args.idempotency_key,
    )
    return AgentRequest(
        meta=meta,
        skill=args.skill,
        input=_load_json(args.input),
        untrusted=_load_json(args.untrusted),
        sequence=args.sequence,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    request = build_request(args)
    response = httpx.post(
        f"{args.agent.rstrip('/')}/a2a",
        json=request.model_dump(mode="json"),
        timeout=args.timeout,
    )
    try:
        body = response.json()
    except ValueError:
        print(response.text)
        return 1
    print(json.dumps(body, ensure_ascii=False, indent=2))
    return 0 if response.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
