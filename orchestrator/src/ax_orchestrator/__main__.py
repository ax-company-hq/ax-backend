"""목업 실행기: `uv run python -m ax_orchestrator`.

빈 포트를 찾아(8000부터) 목 모드로 총괄을 띄우고 `/demo` 주소를 알려 준 뒤 브라우저를 연다.
8000번을 다른 서비스가 쓰고 있어도 다른 번호로 떠서 엉뚱한 서버 화면을 보는 일이 없다.
혼합 모드는 `ORCHESTRATOR_REAL_DEPTS`, 포트 지정은 `--port`다.
"""

from __future__ import annotations

import argparse
import os
import socket
import threading
import webbrowser

DEFAULT_PORT = 8000
MAX_TRIES = 50


def free_port(start: int = DEFAULT_PORT, tries: int = MAX_TRIES) -> int:
    """`start`부터 비어 있는 첫 포트. 연결이 되면 이미 쓰는 중이다."""
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.3)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError(f"{start}~{start + tries - 1} 포트가 모두 사용 중이다. --port로 지정하세요")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="총괄(orchestrator) 목업 실행")
    parser.add_argument("--port", type=int, default=None, help="지정하지 않으면 8000부터 빈 포트")
    parser.add_argument("--no-browser", action="store_true", help="브라우저를 열지 않는다")
    args = parser.parse_args(argv)

    port = args.port or free_port()
    os.environ.setdefault("ORCHESTRATOR_MOCK", "1")
    url = f"http://localhost:{port}/demo"
    print(f"\n  총괄 목업: {url}\n  (끄려면 Ctrl+C)\n", flush=True)
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()

    import uvicorn

    uvicorn.run("ax_orchestrator.main:app", host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
