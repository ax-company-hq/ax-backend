"""결제 장애 주입·해제 (SHOP-06, 실서버 시나리오 A).

    python infra/fault-injection/payment_fault.py inject [--kind failure|latency] [--delay-ms N]
    python infra/fault-injection/payment_fault.py clear

shop-api의 `/api/shop/admin/faults`를 부른다. 표준 라이브러리만 쓰고 bash·pwsh 어디서나 돈다.
종료 코드 0은 성공, 1은 호출 실패, 2는 인자 오류다. 토큰 값은 출력하지 않는다.

환경 변수(`.env`가 아니라 프로세스 환경에서 읽는다):
- SHOP_API_URL: 기본 http://localhost:8010
- SHOP_FAULT_TOKEN: 주입(admin)·해제(admin 또는 infra)용 Bearer 토큰. 기본 `mock:admin`(목 모드)
- SHOP_FAULT_TIMEOUT_S: 호출 제한 시간. 기본 10
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

FAULTS_PATH = "/api/shop/admin/faults"


def _config() -> tuple[str, str, float]:
    base = (os.environ.get("SHOP_API_URL") or "http://localhost:8010").rstrip("/")
    token = os.environ.get("SHOP_FAULT_TOKEN") or "mock:admin"
    timeout = float(os.environ.get("SHOP_FAULT_TIMEOUT_S") or 10)
    return base, token, timeout


def _call(method: str, body: dict | None = None) -> dict:
    base, token, timeout = _config()
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base + FAULTS_PATH,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode() or "{}")


def _state() -> dict:
    return _call("GET").get("data") or {}


def inject(kind: str, delay_ms: int) -> dict:
    body: dict = {"kind": kind}
    if kind == "latency":
        body["delay_ms"] = delay_ms
    _call("POST", body)
    return _state()


def clear() -> dict:
    _call("DELETE")
    return _state()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="결제 장애 주입·해제")
    sub = parser.add_subparsers(dest="command", required=True)
    on = sub.add_parser("inject", help="결제 경로에 장애를 넣는다")
    on.add_argument("--kind", choices=("failure", "latency"), default="failure")
    on.add_argument("--delay-ms", type=int, default=2000)
    sub.add_parser("clear", help="장애를 해제한다")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        state = inject(args.kind, args.delay_ms) if args.command == "inject" else clear()
    except urllib.error.HTTPError as exc:
        print(f"payment_fault {args.command} 실패: HTTP {exc.code}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"payment_fault {args.command} 실패: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps({"command": args.command, "fault": state}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
