#!/usr/bin/env bash
# 한 부서의 검사(pytest, ruff, lint-imports)를 비밀값 없는 컨테이너에서 실행한다.
# 사용: scripts/safe-test.sh <부서> <소스 디렉터리> [<출력 디렉터리>]
#  - 소스 디렉터리는 git archive로 내보낸 사본 (읽기 전용 마운트)
#  - 출력: <out>/test-report.md, 의존성이 바뀌었으면 <out>/uv.lock
# 종료 코드: 전부 통과 0, 아니면 1 (사용법 오류는 2)
set -euo pipefail

IMAGE="${SAFE_TEST_IMAGE:-ghcr.io/astral-sh/uv:0.13.1-python3.12-trixie-slim}"

if [ $# -lt 2 ] || [ $# -gt 3 ]; then
  echo "usage: $0 <dept> <source dir> [<out dir>]" >&2
  exit 2
fi
DEPT="$1"
SRC="$2"
OUT="${3:-./safe-test-out}"

# 신뢰하는 사본(dev)의 team.json을 쓴다: 이 스크립트와 같은 레포의 .github/team.json
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEAM_JSON="$SELF_DIR/../.github/team.json"
[ -f "$TEAM_JSON" ] || { echo "error: $TEAM_JSON not found" >&2; exit 2; }

PY=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -I -c 'import sys; sys.exit(0)' >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "error: python3 required" >&2; exit 2; }

# 부서 검증. 출력: folder, env_prefix(없으면 빈 줄)
if ! INFO="$("$PY" -I -c '
import json, sys
path, dept = sys.argv[1], sys.argv[2]
depts = json.load(open(path, encoding="utf-8"))["depts"]
if dept not in depts:
    sys.stderr.write("error: unknown dept %r. valid: %s\n" % (dept, ", ".join(sorted(depts))))
    sys.exit(3)
d = depts[dept]
print(d["folder"])
print(d.get("env_prefix", ""))
' "$TEAM_JSON" "$DEPT")"; then
  exit 2
fi
FOLDER="$(printf '%s\n' "$INFO" | sed -n 1p | tr -d '\r')"
PREFIX="$(printf '%s\n' "$INFO" | sed -n 2p | tr -d '\r')"

[ -d "$SRC" ] || { echo "error: source dir not found: $SRC" >&2; exit 2; }
mkdir -p "$OUT"
SRC="$(cd "$SRC" && pwd)"
OUT="$(cd "$OUT" && pwd)"
[ -d "$SRC/$FOLDER" ] || { echo "error: folder '$FOLDER' not in source dir" >&2; exit 2; }

# Git Bash(Windows)는 docker 볼륨에 Windows 경로가 필요하고 경로 변환을 꺼야 한다
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    export MSYS_NO_PATHCONV=1
    SRC="$(cd "$SRC" && pwd -W)"
    OUT="$(cd "$OUT" && pwd -W)"
    ;;
esac

ENV_ARGS=(-e TENANT_ID=shop-demo -e UV_LINK_MODE=copy -e UV_NO_PROGRESS=1 -e PYTHONDONTWRITEBYTECODE=1)
MOCK_VAR=""
if [ -n "$PREFIX" ]; then
  MOCK_VAR="${PREFIX}_MOCK"
  ENV_ARGS+=(-e "${MOCK_VAR}=1")
fi

rm -rf "$OUT/.steps" "$OUT/test-report.md" "$OUT/uv.lock"
START=$(date +%s)

# 컨테이너 안에서 실행할 스크립트 (호스트 env는 전달하지 않는다)
read -r -d '' INNER <<'INNER_EOF' || true
set -u
FOLDER="$1"
mkdir -p /work /out/.steps
cp -a /src/. /work/
cd /work
LOCK_CHANGED=0
if uv lock --check >/out/.steps/lock.log 2>&1; then
  echo 0 >/out/.steps/lock.rc
else
  uv lock >>/out/.steps/lock.log 2>&1
  echo $? >/out/.steps/lock.rc
  LOCK_CHANGED=1
fi
echo "$LOCK_CHANGED" >/out/.steps/lock.changed
uv sync --locked --all-packages >/out/.steps/sync.log 2>&1
SYNC_RC=$?
echo "$SYNC_RC" >/out/.steps/sync.rc
if [ "$SYNC_RC" -ne 0 ]; then
  for s in pytest ruff lint-imports; do echo "skipped: uv sync failed" >/out/.steps/$s.log; echo 99 >/out/.steps/$s.rc; done
else
  uv run pytest "$FOLDER/tests" -q >/out/.steps/pytest.log 2>&1; echo $? >/out/.steps/pytest.rc
  uv run ruff check "$FOLDER" >/out/.steps/ruff.log 2>&1; echo $? >/out/.steps/ruff.rc
  uv run lint-imports >/out/.steps/lint-imports.log 2>&1; echo $? >/out/.steps/lint-imports.rc
fi
if [ "$LOCK_CHANGED" = 1 ] && [ -f /work/uv.lock ]; then cp /work/uv.lock /out/uv.lock; fi
chmod -R a+rwX /out 2>/dev/null
exit 0
INNER_EOF

CRC=0
docker run --rm --security-opt no-new-privileges --cap-drop ALL --pids-limit 1024 \
  --entrypoint bash \
  "${ENV_ARGS[@]}" \
  -v "$SRC:/src:ro" -v "$OUT:/out" \
  "$IMAGE" -c "$INNER" _ "$FOLDER" || CRC=$?

STEPS="$OUT/.steps"
rc() { [ -f "$STEPS/$1.rc" ] && tr -d '[:space:]' <"$STEPS/$1.rc" || echo "n/a"; }
ALL_OK=1
[ "$CRC" -eq 0 ] || ALL_OK=0
for s in lock sync pytest ruff lint-imports; do [ "$(rc $s)" = "0" ] || ALL_OK=0; done

DUR=$(( $(date +%s) - START ))
{
  echo "# 테스트 리포트"
  echo
  echo "- 부서: \`$DEPT\`"
  echo "- 폴더: \`$FOLDER\`"
  echo "- MOCK 변수: \`${MOCK_VAR:-없음}\`"
  echo "- 이미지: \`$IMAGE\`"
  echo "- 소요: ${DUR}s"
  echo "- 결과: $([ $ALL_OK = 1 ] && echo 통과 || echo 실패)"
  [ "$CRC" -eq 0 ] || echo "- 컨테이너 종료 코드: $CRC (실행 자체 실패)"
  [ "$(cat "$STEPS/lock.changed" 2>/dev/null || echo 0)" = 1 ] && echo "- uv.lock 갱신됨 (uv.lock을 함께 내보냄)"
  echo
  for s in lock sync pytest ruff lint-imports; do
    r="$(rc $s)"
    if [ "$r" = "0" ]; then st="PASS"; else st="FAIL (exit $r)"; fi
    echo "## $s: $st"
    echo
    echo '```'
    if [ -f "$STEPS/$s.log" ]; then tail -n 60 "$STEPS/$s.log" | sed 's/\x1b\[[0-9;]*[A-Za-z]//g'; else echo "(no output)"; fi
    echo '```'
    echo
  done
} >"$OUT/test-report.md"
rm -rf "$STEPS"

echo "safe-test dept=$DEPT: $([ $ALL_OK = 1 ] && echo PASS || echo FAIL) (${DUR}s) report: $OUT/test-report.md"
[ $ALL_OK = 1 ]
