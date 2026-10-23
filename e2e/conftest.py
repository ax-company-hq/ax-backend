"""e2e는 workspace 패키지가 아니라 일반 테스트 폴더다. `ax_e2e`를 이 폴더에서 import하게 한다."""

import sys
from pathlib import Path

_E2E = str(Path(__file__).resolve().parent)
if _E2E not in sys.path:
    sys.path.insert(0, _E2E)
