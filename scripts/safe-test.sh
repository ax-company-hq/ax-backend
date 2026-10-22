#!/usr/bin/env bash
# 한 부서의 검사(pytest, ruff, lint-imports)를 비밀값 없는 컨테이너에서 실행한다.
# 사용: scripts/safe-test.sh <부서> <소스 디렉터리> [<출력 디렉터리>]
#  - 소스 디렉터리는 git archive로 내보낸 사본 (항상 읽기 전용 마운트)
#  - 출력: <out>/test-report.md, 의존성이 바뀌었으면 <out>/uv.lock
# 종료 코드: 전부 통과 0, 아니면 1, 사용법 오류 2
#
# 구조 (테스트 대상 코드가 리포트·lock을 위조하지 못하게 컨테이너를 나눈다):
#   1) lock : 네트워크 O, 프로젝트 코드 실행 없음(--no-build). uv.lock을 stdout으로만 내보낸다. 쓰기 가능한 호스트 마운트 없음
#   2) sync : 네트워크 O, 이름 있는 볼륨(/work)에 소스 복사 + uv sync. 호스트 쓰기 마운트 없음
#   3) 검사 : pytest / ruff / lint-imports 각각 별도 컨테이너. --network none, 볼륨 읽기 전용, 호스트 쓰기 마운트 없음
# 종료 코드와 로그는 호스트의 docker run 프로세스에서 수집하고 리포트도 호스트가 쓴다.
set -euo pipefail

IMAGE="${SAFE_TEST_IMAGE:-ghcr.io/astral-sh/uv:0.13.1-python3.12-trixie-slim}"
T_LOCK="${SAFE_TEST_TIMEOUT_LOCK:-300}"
T_SYNC="${SAFE_TEST_TIMEOUT_SYNC:-600}"
T_CHECK="${SAFE_TEST_TIMEOUT_CHECK:-600}"

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

# 호스트 쪽 임시 디렉터리 (컨테이너에는 lock 결과 읽기 전용 마운트로만 노출)
WORK="$(mktemp -d "$OUT/.safe-test.XXXXXX")"
RUN_ID="$$-$RANDOM"
VOL="safe-test-vol-$RUN_ID"
cleanup() {
  docker volume rm -f "$VOL" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# Git Bash(Windows)는 docker 볼륨에 Windows 경로가 필요하고 경로 변환을 꺼야 한다
SRC_M="$SRC"
LOCKDIR_M="$WORK/lockout"
mkdir -p "$WORK/lockout" "$WORK/logs"
LOGS="$WORK/logs"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    export MSYS_NO_PATHCONV=1
    SRC_M="$(cd "$SRC" && pwd -W)"
    LOCKDIR_M="$(cd "$WORK/lockout" && pwd -W)"
    ;;
esac

UIDGID="$(id -u):$(id -g)"
HARDEN=(--rm --cap-drop ALL --security-opt no-new-privileges --pids-limit 1024 --memory 4g
        --user "$UIDGID" --entrypoint bash)
RO_ROOT=(--read-only --tmpfs '/tmp:rw,exec,size=2g' -e HOME=/tmp -e UV_CACHE_DIR=/tmp/uv-cache)
UV_ENV=(-e UV_LINK_MODE=copy -e UV_NO_PROGRESS=1 -e UV_PYTHON_DOWNLOADS=never -e PYTHONDONTWRITEBYTECODE=1)
CHECK_ENV=(-e TENANT_ID=shop-demo)
MOCK_VAR=""
if [ -n "$PREFIX" ]; then
  MOCK_VAR="${PREFIX}_MOCK"
  CHECK_ENV+=(-e "${MOCK_VAR}=1")
fi

declare -A RC
START=$(date +%s)

# run_step <name> <timeout sec> <stdout file> <docker run 인자...>
# 호스트에서 종료 코드·로그를 수집한다. 타임아웃이면 컨테이너를 강제로 지운다.
run_step() {
  local name="$1" secs="$2" so="$3" cname rc=0
  shift 3
  cname="safe-test-$RUN_ID-$name"
  timeout "$secs" docker run --name "$cname" "$@" >"$so" 2>"$LOGS/$name.err" || rc=$?
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    echo "timeout after ${secs}s" >>"$LOGS/$name.err"
    rc=124
  fi
  docker rm -f "$cname" >/dev/null 2>&1 || true
  RC[$name]=$rc
}

skip_step() { RC[$1]=99; echo "skipped: $2" >"$LOGS/$1.err"; : >"$LOGS/$1.log"; }

# 1) lock: 프로젝트 코드를 실행하지 않는다 (--no-build: 빌드 백엔드·sdist 빌드 금지, 메타데이터는 정적 pyproject만)
LOCK_SCRIPT='
set -eu
cp -R /src/. /work/
cd /work
if uv lock --check --no-build >&2; then :; else uv lock --no-build >&2; fi
cat /work/uv.lock
'
run_step lock "$T_LOCK" "$LOGS/uv.lock.new" "${HARDEN[@]}" "${RO_ROOT[@]}" "${UV_ENV[@]}" \
  --tmpfs /work:rw,exec,size=1g -v "$SRC_M:/src:ro" "$IMAGE" -c "$LOCK_SCRIPT"
mv "$LOGS/lock.err" "$LOGS/lock.log"
LOCK_CHANGED=0
if [ "${RC[lock]}" -eq 0 ]; then
  if [ ! -f "$SRC/uv.lock" ] || ! cmp -s "$SRC/uv.lock" "$LOGS/uv.lock.new"; then LOCK_CHANGED=1; fi
  cp "$LOGS/uv.lock.new" "$WORK/lockout/uv.lock"
fi

# 2) sync: 이름 있는 볼륨에 소스 복사 + 설치. 이 볼륨이 이후 검사에 읽기 전용으로 마운트된다
if [ "${RC[lock]}" -eq 0 ]; then
  docker volume create "$VOL" >/dev/null
  docker run --rm --user 0 --cap-drop ALL --cap-add CHOWN --entrypoint bash -v "$VOL:/work" "$IMAGE" \
    -c "chown $UIDGID /work" >/dev/null
  SYNC_SCRIPT='
set -eu
cp -R /src/. /work/
cp /newlock/uv.lock /work/uv.lock
cd /work
uv sync --locked --all-packages
chmod -R a+rX /work
'
  run_step sync "$T_SYNC" "$LOGS/sync.log" "${HARDEN[@]}" "${RO_ROOT[@]}" "${UV_ENV[@]}" \
    -v "$SRC_M:/src:ro" -v "$LOCKDIR_M:/newlock:ro" -v "$VOL:/work" "$IMAGE" -c "$SYNC_SCRIPT"
  cat "$LOGS/sync.err" >>"$LOGS/sync.log"
else
  skip_step sync "uv lock failed"
fi

# 3) 검사: 네트워크 없음, 볼륨 읽기 전용, 호스트 쓰기 마운트 없음
check() { # <name> <command...>
  local name="$1"; shift
  if [ "${RC[sync]}" -ne 0 ]; then skip_step "$name" "uv sync failed"; return; fi
  run_step "$name" "$T_CHECK" "$LOGS/$name.log" "${HARDEN[@]}" "${RO_ROOT[@]}" "${UV_ENV[@]}" "${CHECK_ENV[@]}" \
    --network none -w /work -v "$VOL:/work:ro" "$IMAGE" -c "$*"
  cat "$LOGS/$name.err" >>"$LOGS/$name.log"
}
check pytest "exec uv run --no-sync --frozen pytest '$FOLDER/tests' -q -p no:cacheprovider"
check ruff "exec uv run --no-sync --frozen ruff check --no-cache '$FOLDER'"
check lint-imports "exec uv run --no-sync --frozen lint-imports --no-cache"

# 리포트는 호스트가 쓴다
ALL_OK=1
for s in lock sync pytest ruff lint-imports; do [ "${RC[$s]}" -eq 0 ] || ALL_OK=0; done
DUR=$(( $(date +%s) - START ))
rm -f "$OUT/test-report.md" "$OUT/uv.lock"
{
  echo "# 테스트 리포트"
  echo
  echo "- 부서: \`$DEPT\`"
  echo "- 폴더: \`$FOLDER\`"
  echo "- MOCK 변수: \`${MOCK_VAR:-없음}\`"
  echo "- 이미지: \`$IMAGE\`"
  echo "- 소요: ${DUR}s"
  echo "- 결과: $([ "$ALL_OK" = 1 ] && echo 통과 || echo 실패)"
  [ "$LOCK_CHANGED" = 1 ] && echo "- uv.lock 갱신됨 (uv.lock을 함께 내보냄)"
  echo
  for s in lock sync pytest ruff lint-imports; do
    r="${RC[$s]}"
    if [ "$r" -eq 0 ]; then st="PASS"; else st="FAIL (exit $r)"; fi
    echo "## $s: $st"
    echo
    echo '```'
    if [ -f "$LOGS/$s.log" ]; then
      # shellcheck disable=SC2016
      tail -n 60 "$LOGS/$s.log" | sed 's/\x1b\[[0-9;]*[A-Za-z]//g; s/```/` ` `/g'
    else
      echo "(no output)"
    fi
    echo '```'
    echo
  done
} >"$OUT/test-report.md"
# lock 단계 산출물만 밖으로 (테스트 대상 코드는 이 파일에 관여할 수 없다)
if [ "$LOCK_CHANGED" = 1 ] && [ "${RC[lock]}" -eq 0 ]; then cp "$LOGS/uv.lock.new" "$OUT/uv.lock"; fi

echo "safe-test dept=$DEPT: $([ "$ALL_OK" = 1 ] && echo PASS || echo FAIL) (${DUR}s) report: $OUT/test-report.md"
[ "$ALL_OK" = 1 ]
