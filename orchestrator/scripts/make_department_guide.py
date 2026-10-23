"""부서별 연동 안내서를 다시 만든다: python orchestrator/scripts/make_department_guide.py"""

import asyncio
from pathlib import Path

from ax_orchestrator.guide import render

OUT = (
    Path(__file__).resolve().parents[2] / "docs" / "notes" / "orchestrator" / "department-guide.md"
)

if __name__ == "__main__":
    OUT.write_text(asyncio.run(render()), encoding="utf-8")
    print(f"wrote {OUT}")
