"""계약 스키마와 OpenAPI를 결정적(키 정렬) JSON으로 내보낸다.

실행: uv run python scripts/export-contracts.py <out_dir>

출력:
- <out_dir>/schemas/<module>.<Class>.json  : {"validation": ..., "serialization": ...}
  (요청 호환성은 validation, 응답 호환성은 serialization 스키마로 비교한다)
- <out_dir>/openapi/shop-api.json, dashboard-api.json

다른 체크아웃(merge-base)의 환경으로도 돌릴 수 있도록 레포 상대 경로 import를 쓰지 않는다.
이전 커밋에 모듈/앱이 없으면 건너뛰고 안내만 출력한다.
"""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
import sys
from pathlib import Path

APPS = {
    "shop-api": "ax_shop_api.main",
    "dashboard-api": "ax_dashboard_api.main",
}


def dump(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def export_schemas(out: Path) -> int:
    try:
        from pydantic import BaseModel

        root = importlib.import_module("ax_contracts")
    except ImportError as exc:
        print(f"[skip] ax_contracts 없음: {exc}")
        return 0
    names = ["ax_contracts"]
    for info in pkgutil.walk_packages(root.__path__, "ax_contracts."):
        names.append(info.name)
    count = 0
    for name in sorted(names):
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - 깨진 모듈은 알리고 계속
            print(f"[skip] import 실패 {name}: {exc!r}")
            continue
        for cls_name, cls in inspect.getmembers(module, inspect.isclass):
            if not issubclass(cls, BaseModel) or cls is BaseModel:
                continue
            if cls.__module__ != name:  # 정의된 모듈에서만 (재수출 중복 방지)
                continue
            try:
                data = {
                    "validation": cls.model_json_schema(mode="validation"),
                    "serialization": cls.model_json_schema(mode="serialization"),
                }
            except Exception as exc:  # noqa: BLE001
                print(f"[skip] 스키마 생성 실패 {name}.{cls_name}: {exc!r}")
                continue
            dump(out / "schemas" / f"{name}.{cls_name}.json", data)
            count += 1
    print(f"schemas: {count}개")
    return count


def export_openapi(out: Path) -> None:
    for label, module_name in APPS.items():
        try:
            module = importlib.import_module(module_name)
            spec = module.app.openapi()
        except ImportError as exc:
            print(f"[skip] {label} 없음: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"[skip] {label} openapi 실패: {exc!r}")
            continue
        dump(out / "openapi" / f"{label}.json", spec)
        print(f"openapi: {label}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: export-contracts.py <out_dir>", file=sys.stderr)
        return 2
    out = Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)
    export_schemas(out)
    export_openapi(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
