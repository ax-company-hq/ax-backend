#!/usr/bin/env bash
# 자동화가 쓰는 라벨을 만든다/갱신한다 (--force). 사람이 직접 한 번 실행한다.
# 사용: scripts/setup-labels.sh [--dry-run] [<repo>]   (기본 repo: ax-company-hq/ax-backend)
set -euo pipefail

DRY=0
REPO="ax-company-hq/ax-backend"
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    -*) echo "unknown option: $a" >&2; exit 2 ;;
    *) REPO="$a" ;;
  esac
done

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEAM_JSON="$SELF_DIR/../.github/team.json"
PY=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -I -c 'import sys' >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "error: python3 required" >&2; exit 2; }

mk() { # name color description
  if [ "$DRY" = 1 ]; then
    printf 'gh label create "%s" --color %s --description "%s" --force -R %s\n' "$1" "$2" "$3" "$REPO"
  else
    gh label create "$1" --color "$2" --description "$3" --force -R "$REPO"
  fi
}

# 종류
mk external      b60205 "팀원이 아닌 사람의 이슈·PR (Claude 입력 차단)"
mk task          0e8a16 "요구사항에서 생성된 작업 이슈"
mk request       1d76db "다른 부서가 요청한 작업 이슈"
mk priority      d93f0b "우선 처리"
mk report        c5def5 "일일 리포트 이슈"
mk generated-pr  c5def5 "기획 에이전트가 만든 PR (사람이 머지)"
# 진행 상태
mk in-progress   fbca04 "에이전트가 작업 중"
mk blocked       b60205 "재시도 초과로 정지 (사람 확인 필요)"
mk obsolete      cccccc "더 이상 필요 없음"
mk delegated     bfd4f2 "다른 부서 요청으로 넘김"
mk no-op         cccccc "변경 없이 끝남"
mk quota         e99695 "Claude 사용 한도 도달 (UTC 날짜 변경 시 해제)"
mk needs-human   b60205 "같은 이슈 2회 실패 — 사람 확인 필요 (떼고 to: 다시 붙이면 재개)"
mk needs-split   d93f0b "작성이 턴 한도 초과 — 작업을 나눠 다시 올릴 것"
mk wip-wait      fef2c0 "부서 동시 작성 한도 — 자리가 나면 자동 시작"
mk stale         cccccc "48시간 변화 없어 매일 정리가 닫음"
# 사고·복구
mk incident      b60205 "dev 장애 사건"
mk reverted      e99695 "dev 깨짐으로 revert됨"
mk arbiter-fixed d4c5f9 "제3 부서(arbiter)가 수정함"
mk hotfix        d93f0b "code owner 급한 수정 (새 커밋 시 해제)"
# 재시도·리뷰·CI 수정 횟수
for n in 1 2 3; do mk "retry-$n" fef2c0 "작업 재시도 $n회"; done
for n in 1 2 3; do mk "review-round-$n" d4c5f9 "에이전트 리뷰 $n라운드"; done
for n in 1 2; do mk "ci-fix-$n" f9d0c4 "CI 수정 $n회"; done
# 단계
mk phase:1 bfdadc "1단계"
mk phase:2 bfdadc "2단계"
mk phase:3 bfdadc "3단계"

# 부서별: dept:<id> 담당 부서 / to:<id> 요청 대상 / by:<id> 작성 부서
"$PY" -I -c '
import json, sys
for d in sorted(json.load(open(sys.argv[1], encoding="utf-8"))["depts"]):
    print(d)
' "$TEAM_JSON" | tr -d '\r' | while read -r d; do
  mk "dept:$d" 0052cc "담당 부서: $d"
  mk "to:$d"   5319e7 "요청 대상 부서: $d"
  mk "by:$d"   006b75 "작성 부서: $d"
done
