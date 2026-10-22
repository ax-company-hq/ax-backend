"""OpenAPI 문서를 contracts/openapi/shop-api.json에 쓴다.

실행: uv run python -m ax_shop_api.export_openapi [출력 경로]
"""

import json
import sys
from pathlib import Path

from ax_shop_api.main import app

# src/ax_shop_api/export_openapi.py → 레포 루트
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = REPO_ROOT / "contracts" / "openapi" / "shop-api.json"


def render() -> str:
    return json.dumps(app.openapi(), sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def main(output: Path = DEFAULT_OUTPUT) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(), encoding="utf-8", newline="\n")
    return output


if __name__ == "__main__":
    path = main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT)
    print(f"wrote {path}")
