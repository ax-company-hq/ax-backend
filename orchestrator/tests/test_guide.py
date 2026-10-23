import asyncio
from pathlib import Path

from ax_orchestrator.guide import render

DOC = (
    Path(__file__).resolve().parents[2] / "docs" / "notes" / "orchestrator" / "department-guide.md"
)


def test_department_guide_is_up_to_date() -> None:
    expected = asyncio.run(render())
    actual = DOC.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert actual == expected, (
        "plan이 바뀌었다: python orchestrator/scripts/make_department_guide.py 로 다시 만든다"
    )
