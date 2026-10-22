"""`python -m hr_agent.demo` — localhost 데모 서버를 띄운다 (기본 8114, `HR_DEMO_PORT`로 변경)."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    port = int(os.getenv("HR_DEMO_PORT", "8114"))
    print(f"hr 데모: http://localhost:{port}")
    uvicorn.run("hr_agent.demo.server:demo_app", host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
