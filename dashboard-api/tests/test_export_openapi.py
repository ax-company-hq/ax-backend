import json
from pathlib import Path

from ax_dashboard_api import export_openapi


def test_export_writes_sorted_openapi(tmp_path: Path) -> None:
    output = export_openapi.main(tmp_path / "dashboard-api.json")
    text = output.read_text(encoding="utf-8")
    doc = json.loads(text)
    assert "/health" in doc["paths"]
    assert text == json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def test_default_output_is_contracts_openapi() -> None:
    expected = ("contracts", "openapi", "dashboard-api.json")
    assert export_openapi.DEFAULT_OUTPUT.parts[-3:] == expected
