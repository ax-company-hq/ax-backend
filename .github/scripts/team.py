#!/usr/bin/env python3
"""자동화 규칙 판단 (docs/automation-spec.md 0장). 모든 워크플로는 규칙 판단을 이 스크립트로만 한다.

environment bot 잡에서는 레포 밖 임시 venv(해시 고정 의존성)에서
`python3 -I .github/scripts/team.py ...`로 실행한다.
PR 코드는 git으로 읽기만 하고 실행하지 않는다 (test-guard도 ast.parse로 정적 분석만).

명령:
  is-member <login> · is-trusted <login> · owner-token <dept>
  dept-of-branch <branch> · depts-of <login>
  check-diff <base> <head> --depts a,b --author-kind human|bot [--branch <branch>]
  classify <base> <head>                     바뀐 경로를 부서·human_paths·shared로 나눈 JSON
  reviewers-for <base> <head> <author_person>
  arbiter-for <author_dept> <reviewer_dept...>
  test-guard <base> <head>                   부서별 합계로 테스트 약화 검사
  review-required <base> <head>              에이전트 리뷰 대상이면 0 (공유 부서·부서 2개 이상)
  cap-ok <login> --kind write|review [--day YYYY-MM-DD]   오늘 그 종류 Claude 잡 수 < 상한이면 0
  requeue <issue> <reason> [--pr N --head-sha S] [--run-id R]   (GITHUB_REPOSITORY, GH_TOKEN=ax-bot)
  export-json [--check]                      .github/team.json 생성 (--check: 동기화 검사만)
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).resolve().parents[2]
TEAM_YML = ROOT / ".github" / "team.yml"
TEAM_JSON = ROOT / ".github" / "team.json"

SYMLINK_MODE = "120000"
GITLINK_MODE = "160000"


class TeamError(Exception):
    """설정이나 입력이 규칙에 맞지 않을 때."""


@dataclass(frozen=True)
class Change:
    """git diff --raw 한 줄. status는 A·M·D·T 중 하나 (rename은 끄고 읽는다)."""

    status: str
    path: str
    new_mode: str = "100644"


@dataclass
class Classified:
    depts: dict[str, list[str]] = field(default_factory=dict)
    human: list[str] = field(default_factory=list)
    shared: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"depts": self.depts, "human": self.human, "shared": self.shared}


class Team:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.members: dict[str, dict] = data["members"]
        self.bot_login: str = data["bot_login"]
        self.depts: dict[str, dict] = data["depts"]
        self.reviewers: dict[str, list[str]] = data.get("reviewers", {})
        self.fallback_order: list[str] = data.get("fallback_order", [])
        self.contracts_fallback: str | None = data.get("contracts_fallback")
        self.notes_prefix: str = data.get("human_path_exceptions", "docs/notes/").rstrip("/") + "/"
        self.shared_rules: dict[str, str] = data.get("shared_rules", {})
        self.bot_branches: dict[str, dict] = data.get("bot_branches", {})
        self.forbidden_in_dept: list[str] = data.get("forbidden_in_dept", [])
        self.hotfix_labelers: list[str] = data.get("hotfix_labelers", [])
        self.review_required_depts: list[str] = data.get("review_required_depts", [])
        self.wip: dict[str, int] = data.get("wip", {})
        # 부서별 WIP 덮어쓰기 { <부서>: { open_bot_prs: N, in_progress_issues: M } } (없으면 wip)
        self.wip_overrides: dict[str, dict[str, int]] = data.get("wip_overrides") or {}
        self.stale_hours: int = int(data.get("stale_hours", 48))
        self.contracts_affected: dict[str, list[str]] = data.get("contracts_affected", {})
        self.human_files: set[str] = set(data.get("human_files", []))
        self._validate()

    @classmethod
    def load(cls, path: Path = TEAM_YML) -> Team:
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")))

    def _validate(self) -> None:
        for dept_id, dept in self.depts.items():
            for owner in dept["owners"]:
                if owner not in self.members:
                    raise TeamError(f"{dept_id}의 owner {owner}가 members에 없음")
            token_owner = dept.get("token_owner")
            if token_owner is not None and token_owner not in self.members:
                raise TeamError(f"{dept_id}의 token_owner {token_owner}가 members에 없음")
        for dept_id, limits in self.wip_overrides.items():
            if dept_id not in self.depts:
                raise TeamError(f"wip_overrides의 {dept_id}가 depts에 없음")
            for key, value in (limits or {}).items():
                valid = isinstance(value, int) and not isinstance(value, bool) and value >= 0
                if key not in ("open_bot_prs", "in_progress_issues") or not valid:
                    raise TeamError(f"wip_overrides {dept_id}.{key}가 올바르지 않음")
        for dept_id, revs in self.reviewers.items():
            for r in [dept_id, *revs]:
                if r not in self.depts:
                    raise TeamError(f"reviewers의 {r}가 depts에 없음")
        for d in [*self.fallback_order, *filter(None, [self.contracts_fallback])]:
            if d not in self.depts:
                raise TeamError(f"fallback 부서 {d}가 depts에 없음")
        for d in self.review_required_depts:
            if d not in self.depts:
                raise TeamError(f"review_required_depts의 {d}가 depts에 없음")
        for prefix, ds in self.contracts_affected.items():
            for d in ds:
                if d not in self.depts:
                    raise TeamError(f"contracts_affected {prefix}의 {d}가 depts에 없음")
        for f in self.human_files:
            if not self._dept_folder_of(f):
                raise TeamError(f"human_files {f}가 부서 폴더 안이 아님")
        for login, m in self.members.items():
            for kind in CAP_KINDS:
                if not isinstance(m.get(f"{kind}_cap"), int):
                    raise TeamError(f"{login}의 {kind}_cap이 정수가 아님")

    # ── 사람·부서 ─────────────────────────────────────────
    def is_member(self, login: str) -> bool:
        return login in self.members

    def is_trusted(self, login: str) -> bool:
        return self.is_member(login) or login == self.bot_login

    def resolve_dept(self, name: str) -> str | None:
        """부서 id, alias, 폴더 마지막 이름을 부서 id로 바꾼다."""
        if name in self.depts:
            return name
        for dept_id, dept in self.depts.items():
            if name in dept.get("aliases", []) or name == PurePosixPath(dept["folder"]).name:
                return dept_id
        return None

    def owners(self, dept: str) -> list[str]:
        """부서 담당 (리뷰어 배정·작성자 판단용). 실행 토큰·cap의 주인은 `token_owner`."""
        return self.depts[dept]["owners"]

    def token_owner(self, dept: str) -> str:
        """그 부서 Claude 실행의 토큰·하루 cap 주인. team.yml `token_owner`, 없으면 owners[0]."""
        return self.depts[dept].get("token_owner") or self.owners(dept)[0]

    def owner_token(self, dept: str) -> str:
        return self.members[self.token_owner(dept)]["token"]

    def wip_limit(self, dept: str, key: str) -> int:
        """부서의 WIP 한도 (open_bot_prs | in_progress_issues). wip_overrides가 wip보다 먼저."""
        override = self.wip_overrides.get(dept) or {}
        if key in override:
            return int(override[key])
        return int(self.wip.get(key, 0))

    def dept_of_branch(self, branch: str) -> str | None:
        if "/" not in branch:
            return None
        return self.resolve_dept(branch.split("/", 1)[0])

    def depts_of(self, login: str) -> list[str]:
        return [d for d, info in self.depts.items() if login in info["owners"]]

    # ── 경로 ─────────────────────────────────────────────
    def _dept_folder_of(self, path: str) -> str | None:
        for dept_id, dept in self.depts.items():
            folder = dept["folder"].rstrip("/")
            if path == folder or path.startswith(folder + "/"):
                return dept_id
        return None

    def is_core_path(self, path: str) -> bool:
        """핵심 파일: core_paths(.github/·.claude/·CODEOWNERS)와 human_files(기준표 toml)."""
        if path in self.human_files:
            return True
        for p in self.data.get("core_paths", []):
            if path == p.rstrip("/") or (p.endswith("/") and path.startswith(p)):
                return True
        return False

    def is_human_file(self, path: str) -> bool:
        """부서 폴더 안이지만 사람만 바꾸는 파일 (team.yml human_files)."""
        return path in self.human_files

    def dept_of_path(self, path: str) -> str | None:
        """경로가 속한 부서. docs/notes/<부서>/는 그 부서. 없으면 None (human_paths 또는 shared).

        human_files(부서 폴더 안의 사람 전용 파일)는 None → human_paths로 취급한다.
        """
        if path.startswith(self.notes_prefix):
            rest = path[len(self.notes_prefix) :]
            if "/" in rest:
                return self.resolve_dept(rest.split("/", 1)[0])
            return None
        if self.is_human_file(path):
            return None
        return self._dept_folder_of(path)

    def unknown_notes_dir(self, path: str) -> str | None:
        """docs/notes/<x>/에서 x가 부서 id·별칭이 아니면 x.

        CODEOWNERS가 /docs/notes/*/를 owner 없이 열어두므로 여기서 막는다.
        """
        if not path.startswith(self.notes_prefix):
            return None
        rest = path[len(self.notes_prefix) :]
        if "/" not in rest:
            return rest
        name = rest.split("/", 1)[0]
        return None if self.resolve_dept(name) else name

    def is_shared(self, path: str) -> bool:
        return path in self.shared_rules

    def classify(self, paths: list[str]) -> Classified:
        result = Classified()
        for p in paths:
            dept = self.dept_of_path(p)
            if dept:
                result.depts.setdefault(dept, []).append(p)
            elif self.is_shared(p):
                result.shared.append(p)
            else:
                result.human.append(p)
        return result

    def forbidden_name(self, path: str) -> str | None:
        """부서 폴더 안에 생기면 안 되는 이름(CLAUDE.md, .claude/ 등)이 경로에 있으면 그 이름."""
        dept = self.dept_of_path(path)
        if not dept or path.startswith(self.notes_prefix):
            return None
        folder = self.depts[dept]["folder"].rstrip("/")
        inner = PurePosixPath(path[len(folder) + 1 :]).parts if path != folder else ()
        forbidden = {f.casefold() for f in self.forbidden_in_dept}
        for part in inner:
            if part.rstrip(". ").casefold() in forbidden:
                return part
        return None

    def bot_branch_rule(self, branch: str | None) -> tuple[str, dict] | None:
        if not branch:
            return None
        for prefix, rule in self.bot_branches.items():
            if branch.startswith(prefix):
                return prefix, rule
        return None

    # ── 판단 ─────────────────────────────────────────────
    def check_diff(
        self,
        changes: list[Change],
        depts: list[str],
        author_kind: str,
        branch: str | None = None,
    ) -> list[str]:
        """규칙 위반 목록. 빈 목록이면 통과."""
        if author_kind not in ("human", "bot"):
            raise TeamError(f"author_kind는 human|bot: {author_kind}")
        violations: list[str] = []
        changed = {c.path for c in changes}

        for c in changes:
            if c.status != "D" and c.new_mode == SYMLINK_MODE:
                violations.append(f"{c.path}: 심볼릭 링크는 허용하지 않음")
            if c.status != "D" and c.new_mode == GITLINK_MODE:
                violations.append(f"{c.path}: 서브모듈은 허용하지 않음")
            name = self.forbidden_name(c.path)
            if name and c.status != "D":
                violations.append(f"{c.path}: 부서 폴더 안에 {name}를 만들 수 없음")
            notes = self.unknown_notes_dir(c.path)
            if notes is not None and c.status != "D":
                msg = f"docs/notes/ 아래는 부서 id 폴더만 쓸 수 있음 ({notes})"
                violations.append(f"{c.path}: {msg}")

        rule = self.bot_branch_rule(branch) if author_kind == "bot" else None
        if rule:
            prefix, spec = rule
            allowed = spec.get("paths")
            for p in sorted(changed):
                if isinstance(allowed, list):
                    if not any(p.startswith(a) for a in allowed):
                        only = ", ".join(allowed)
                        violations.append(f"{p}: {prefix} 브랜치는 {only}만 바꿀 수 있음")
                elif not self.dept_of_path(p) and not self.is_shared(p):
                    # revert/: 되돌리는 PR(에이전트 PR)은 human_paths를 바꾼 적이 없다.
                    # 사람 경로는 사람만 되돌린다
                    violations.append(f"{p}: {prefix} 브랜치는 사람 승인 경로를 바꿀 수 없음")
            return violations

        own_folders = {self.depts[d]["folder"].rstrip("/") for d in depts}
        for p in sorted(changed):
            dept = self.dept_of_path(p)
            if dept:
                if dept not in depts:
                    who = ", ".join(self.members[o]["name"] for o in self.owners(dept))
                    violations.append(f"{p}: {dept} 부서({who}) 폴더 — to:{dept} 요청으로 넘길 것")
            elif self.is_shared(p):
                if self.shared_rules[p] == "requires_pyproject":
                    if not any(f"{folder}/pyproject.toml" in changed for folder in own_folders):
                        msg = "같은 PR에서 자기 부서 pyproject.toml이 바뀔 때만 허용"
                        violations.append(f"{p}: {msg}")
                else:
                    violations.append(f"{p}: 알 수 없는 shared_rules 값 {self.shared_rules[p]}")
            elif author_kind == "bot":
                violations.append(f"{p}: 사람 승인 경로(human_paths) — 에이전트는 바꿀 수 없음")
        return violations

    def contracts_reviewers(self, paths: list[str] | None) -> list[str]:
        """contracts PR의 리뷰 부서: 바뀐 계약 파일을 쓰는 부서 (contracts_affected).

        표에 없는 파일이 하나라도 있거나 경로를 모르면 reviewers.contracts(공통 리뷰)를 더한다.
        contracts/tests/·*.md는 리뷰 부서를 정하는 데 쓰지 않는다.
        """
        default = self.reviewers.get("contracts", [])
        if paths is None:
            return list(default)
        folder = self.depts["contracts"]["folder"].rstrip("/") + "/"
        out: list[str] = []
        unmapped = False
        for p in paths:
            rel = p[len(folder):] if p.startswith(folder) else p
            if rel.startswith("tests/") or rel.endswith(".md"):
                continue
            hit = [ds for prefix, ds in self.contracts_affected.items() if rel.startswith(prefix)]
            if not hit:
                unmapped = True
            for ds in hit:
                out += [d for d in ds if d not in out]
        if unmapped or not out:
            out += [d for d in default if d not in out]
        return out

    def reviewers_for(self, changed_depts: list[str], author_person: str,
                      contracts_paths: list[str] | None = None) -> list[str]:
        """리뷰 부서 목록 (순서 유지, 중복 없음). 같은 사람이면 대체한다.

        contracts는 contracts_paths(바뀐 contracts 경로)로 영향받는 부서를 고른다.
        """
        result: list[str] = []

        def take_fallback() -> str | None:
            for f in self.fallback_order:
                if author_person not in self.owners(f) and f not in result:
                    return f
            return None

        for dept in changed_depts:
            revs = (self.contracts_reviewers(contracts_paths) if dept == "contracts"
                    else self.reviewers.get(dept, []))
            for reviewer in revs:
                slot: str | None = reviewer
                if author_person in self.owners(reviewer):
                    slot = None
                    if dept == "contracts" and self.contracts_fallback:
                        fb = self.contracts_fallback
                        if author_person not in self.owners(fb) and fb not in result:
                            slot = fb
                    if slot is None:
                        slot = take_fallback()
                if slot and slot not in result:
                    result.append(slot)
        return result

    def review_required(self, classified: Classified) -> bool:
        """에이전트 리뷰가 필요한 PR인가: review_required_depts를 건드렸거나 부서 2개 이상.

        부서 하나 안에서만 바뀐 PR(+ uv.lock·human_paths)은 리뷰 없이 CI·관문으로 머지한다.
        human_paths는 code owner 승인이 따로 걸린다.
        """
        depts = set(classified.depts)
        return bool(depts & set(self.review_required_depts)) or len(depts) >= 2

    def arbiter_for(self, author_dept: str, reviewer_depts: list[str]) -> str:
        used = {author_dept, *reviewer_depts}
        people = {p for d in used for p in self.owners(d)}
        for f in self.fallback_order:
            if f not in used and not people & set(self.owners(f)):
                return f
        for f in self.fallback_order:
            if f not in used:
                return f
        raise TeamError("arbiter로 쓸 부서가 없음")

    def to_json_text(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


# ── test-guard (정적 분석) ───────────────────────────────
def is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    return p.suffix == ".py" and "tests" in p.parts and (
        p.name.startswith("test_") or p.name.endswith("_test.py")
    )


def _count_asserts(node: ast.AST) -> int:
    n = 0
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert):
            n += 1
        elif isinstance(sub, ast.Call):
            func = sub.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name.startswith("assert") or name == "raises":
                n += 1
    return n


def count_tests_and_asserts(source: str) -> tuple[int, int]:
    """(test_ 함수 수, 테스트 함수 안 assert 수)."""
    tree = ast.parse(source)
    tests = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name.startswith("test_")
    ]
    return len(tests), sum(_count_asserts(t) for t in tests)


HUMAN_TESTS = "(사람 경로)"


def _trivial_assert(node: ast.AST) -> bool:
    """`assert True`, `assert 1`, `assert "x"`처럼 항상 통과하는 상수 assert."""
    return isinstance(node, ast.Assert) and isinstance(node.test, ast.Constant) and bool(
        node.test.value)


def _count_real_asserts(node: ast.AST) -> int:
    return _count_asserts(node) - sum(1 for sub in ast.walk(node) if _trivial_assert(sub))


def test_inventory(source: str) -> tuple[Counter[str], int]:
    """(테스트 이름 개수 `Class.test_x`/`test_x`, 의미 있는 assert 수)."""
    tree = ast.parse(source)
    names: Counter[str] = Counter()
    asserts = 0

    def visit(body, prefix: str) -> None:
        nonlocal asserts
        for n in body:
            if isinstance(n, ast.ClassDef):
                visit(n.body, f"{prefix}{n.name}.")
            elif isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name.startswith(
                    "test_"):
                names[prefix + n.name] += 1
                asserts += _count_real_asserts(n)

    visit(tree.body, "")
    return names, asserts


def find_test_weakening(changes: list[Change], read_base, read_head,
                        team: Team | None = None) -> list[str]:
    """부서별로 테스트 약화를 찾는다.

    - 바뀐 파일에 있던 테스트 이름이 같은 부서의 바뀐 파일에서 그 개수만큼 남아 있지 않으면 실패
      (파일을 나누거나 옮기는 것은 이름이 남으니 통과, 지우거나 이름만 바꿔 갈아치우면 실패.
       `test_smoke`처럼 흔한 이름이 두 파일에 있으면 둘 다 남아야 한다)
    - 테스트 함수 안 의미 있는 assert 수(`assert True` 같은 상수 assert 제외) 합계가 줄면 실패
    team이 없으면 전체를 한 묶음으로 본다. 부서 폴더 밖 테스트는 "(사람 경로)" 한 묶음.
    """
    violations: list[str] = []
    groups: dict[str, dict] = {}
    for c in changes:
        if not is_test_path(c.path):
            continue
        try:
            b = test_inventory(read_base(c.path)) if c.status != "A" else (Counter(), 0)
            h = test_inventory(read_head(c.path)) if c.status != "D" else (Counter(), 0)
        except (SyntaxError, ValueError, RecursionError, MemoryError) as e:
            violations.append(f"{c.path}: 파싱 실패 ({type(e).__name__})")
            continue
        group = (team.dept_of_path(c.path) if team else None) or HUMAN_TESTS
        g = groups.setdefault(group, {"base": Counter(), "where": {}, "head": Counter(),
                                      "ba": 0, "ha": 0})
        for name in b[0]:
            g["where"].setdefault(name, c.path)
        g["base"] += b[0]
        g["head"] += h[0]
        g["ba"] += b[1]
        g["ha"] += h[1]
    for group, g in sorted(groups.items()):
        where = f"{group}: " if team else ""
        gone = sorted(n for n, k in g["base"].items() if g["head"][n] < k)
        for name in gone[:10]:
            violations.append(f"{where}테스트 삭제 {g['where'][name]}::{name}")
        if len(gone) > 10:
            violations.append(f"{where}테스트 삭제 외 {len(gone) - 10}개")
        if g["ha"] < g["ba"]:
            violations.append(
                f"{where}테스트 함수 안 assert 감소 {g['ba']} → {g['ha']} (부서 합계)")
    return violations


# ── git ──────────────────────────────────────────────────
def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, encoding="utf-8",
        errors="surrogateescape",
    ).stdout


def merge_base(base: str, head: str) -> str:
    return git("merge-base", base, head).strip()


def diff_changes(base: str, head: str) -> list[Change]:
    """merge-base 기준 three-dot 변경 목록. rename은 삭제+추가로 본다."""
    out = git("diff", "--raw", "-z", "--no-renames", "--no-abbrev", f"{base}...{head}")
    parts = out.split("\0")
    changes: list[Change] = []
    i = 0
    while i < len(parts) - 1:
        meta, path = parts[i], parts[i + 1]
        i += 2
        if not meta.startswith(":"):
            continue
        fields = meta[1:].split()
        changes.append(Change(status=fields[4][0], path=path, new_mode=fields[1]))
    return changes


def show(rev: str, path: str) -> str:
    return git("show", f"{rev}:{path}")


# ── cap-ok (하루 Claude 실행 상한) ─────────────────────────
# Claude 잡이 있는 워크플로는 run-name(=display_title)에 부서를 남긴다. 그래서 run 목록만 읽으면
# 셀 수 있다 (run마다 jobs를 읽지 않음). 워크플로 파일당 1회 조회, 100개를 넘으면 다음 페이지.
#   _claude-write (수동 실행): "claude:<부서>:<모드> #<이슈>"
#   claude-request:            "claude-request to:<부서> by <라벨 붙인 사람>"
#   claude-review:             "claude-review <PR 브랜치> by <PR 작성자>"
#                              → 리뷰 부서는 reviewers_for로 계산해서 센다
CAP_KINDS = ("write", "review")

CLAUDE_RUN_WORKFLOWS = {
    "ax-company-hq/ax-backend": (
        "_claude-write.yml", "claude-request.yml", "claude-review.yml", "morning.yml",
    ),
    "ax-company-hq/ax-frontend": (),  # F-1단계에서 Claude 워크플로가 생기면 추가
}
WRITE_TITLE = re.compile(r"^claude:([a-z0-9-]+):[a-z-]+(?: |$)")
REQUEST_TITLE = re.compile(r"^claude-request to:([a-z0-9-]+) by (\S+)$")
REVIEW_TITLE = re.compile(r"^claude-review (\S+) by (\S+)$")


def run_kind(title: str) -> str | None:
    """run 제목 → 상한 종류 (작성 write / 리뷰 review)."""
    if WRITE_TITLE.match(title) or REQUEST_TITLE.match(title):
        return "write"
    if REVIEW_TITLE.match(title):
        return "review"
    return None


def token_depts_of(team: Team, login: str) -> set[str]:
    """그 사람이 실행 토큰 주인(token_owner, 없으면 owners의 첫 번째)인 부서. 프론트 부서 포함."""
    depts = {d for d in team.depts if team.token_owner(d) == login}
    depts |= {d for d, who in team.data.get("frontend_depts", {}).items() if who == login}
    return depts


def run_token_depts(team: Team, title: str) -> list[str]:
    """run 제목에서 그 실행이 쓴 Claude 토큰의 부서 목록 (부서 하나 = Claude 잡 하나)."""
    m = WRITE_TITLE.match(title)
    if m:
        dept = team.resolve_dept(m.group(1))
        return [dept] if dept else []
    m = REQUEST_TITLE.match(title)
    if m:
        dept = team.resolve_dept(m.group(1))
        # 봇이 붙인 요청 라벨은 route가 실행하지 않는다
        return [dept] if dept and m.group(2) != team.bot_login else []
    m = REVIEW_TITLE.match(title)
    if m:
        branch, author = m.groups()
        dept = team.dept_of_branch(branch)
        if not dept:
            return []  # ci/·docs/ 같은 사람 경로 PR은 리뷰 생략
        person = team.token_owner(dept) if author == team.bot_login else author
        return team.reviewers_for([dept], person) if person in team.members else []
    return []


# 끝났지만 성공이 아닌 run: Claude 단계까지 갔는지 jobs로 한 번 확인한다 (결과는 캐시)
FAILED_CONCLUSIONS = {"failure", "cancelled", "timed_out", "startup_failure"}
CLAUDE_STEPS = ("Claude", "Claude (리뷰)")
CLAUDE_INSTALL_STEP = "Claude Code 설치"
CLAUDE_JOB = re.compile(r"(?:^|/ )claude:([a-z0-9-]+):")


def job_claude_dept(job: dict) -> str | None:
    """Claude 단계가 실제로 돈 claude:<부서>: 잡이면 그 부서, 아니면 None.

    Claude 단계 전(설치·토큰 확인·cap 등)에서 끝났으면 Claude 단계는 skipped라 세지 않는다.
    설치 단계가 따로 없던 옛 run(#115 전)은 설치가 Claude 단계 안에 있어, 그 단계가 실패했으면
    설치 실패(429)로 보고 세지 않는다.
    """
    m = CLAUDE_JOB.search(job.get("name") or "")
    if not m:
        return None
    steps = job.get("steps") or []
    claude = next((s for s in steps if s.get("name") in CLAUDE_STEPS), None)
    if not claude or claude.get("conclusion") in (None, "skipped"):
        return None
    legacy = not any((s.get("name") or "").startswith(CLAUDE_INSTALL_STEP) for s in steps)
    if legacy and claude.get("conclusion") == "failure":
        return None
    return m.group(1)


def load_cap_cache() -> dict[str, list[str]]:
    path = os.environ.get("AX_CAP_CACHE")
    try:
        with open(path, encoding="utf-8") as fh:  # type: ignore[arg-type]
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


def save_cap_cache(cache: dict[str, list[str]]) -> None:
    path = os.environ.get("AX_CAP_CACHE")
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)


def needs_jobs(title: str, conclusion: str | None, team: Team | None = None) -> bool:
    """run 제목만으로는 Claude 잡 부서를 알 수 없어 jobs를 읽어야 하는 run.

    실패·취소 run (Claude 단계까지 갔는지),
    끝난 contracts 리뷰 run (리뷰 부서가 바뀐 파일로 정해짐).
    """
    if conclusion in FAILED_CONCLUSIONS:
        return True
    m = REVIEW_TITLE.match(title)
    return bool(team and conclusion and conclusion != "skipped" and m
                and team.dept_of_branch(m.group(1)) == "contracts")


def resolve_failed_runs(runs: list[tuple], cache: dict[str, list[str]],
                        team: Team | None = None) -> dict[str, list[str]]:
    """jobs가 필요한 run마다 Claude 단계가 돈 부서 목록을 채운다 (캐시에 없는 것만 jobs 조회)."""
    for repo, run_id, title, conclusion in runs:
        if not needs_jobs(title, conclusion, team) or str(run_id) in cache:
            continue
        data = gh_api(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100") or {}
        cache[str(run_id)] = [d for d in (job_claude_dept(j) for j in data.get("jobs", [])) if d]
    return cache


def usage_by_owner(team: Team, runs: list[tuple], ran: dict[str, list[str]] | None = None,
                   kind: str | None = None) -> dict[str, int]:
    """(repo, run_id, display_title, conclusion) 목록 → 토큰 주인별 Claude 잡 수.

    kind(write|review)를 주면 그 종류 run만 센다. skipped run은 세지 않는다.
    실패·취소 run은 ran(실제로 Claude 단계가 돈 부서)으로 센다. ran에 없으면 제목 기준 (보수적으로).
    """
    ran = ran or {}
    counts: dict[str, int] = {}
    fronts = team.data.get("frontend_depts", {})
    for _repo, run_id, title, conclusion in runs:
        if conclusion == "skipped" or (kind and run_kind(title) != kind):
            continue
        if needs_jobs(title, conclusion, team) and str(run_id) in ran:
            depts = [team.resolve_dept(d) or d for d in ran[str(run_id)]]
        else:
            depts = run_token_depts(team, title)
        for dept in depts:
            owner = team.token_owner(dept) if dept in team.depts else fronts.get(dept)
            if owner:
                counts[owner] = counts.get(owner, 0) + 1
    return counts


def todays_claude_runs(day: str) -> list[tuple]:
    """오늘(UTC) Claude 워크플로 run 목록.

    실패하면 예외를 그대로 올린다 (호출한 쪽이 fail-closed).
    """
    runs: list[tuple] = []
    me = os.environ.get("GITHUB_RUN_ID")
    for repo, files in CLAUDE_RUN_WORKFLOWS.items():
        for wf in files:
            page = 1
            while True:
                data = gh_api(f"repos/{repo}/actions/workflows/{wf}/runs"
                              f"?created=>={day}&per_page=100&page={page}") or {}
                items = data.get("workflow_runs", [])
                runs += [(repo, r.get("id"), r.get("display_title") or "", r.get("conclusion"))
                         for r in items if str(r.get("id")) != me]  # 지금 실행 중인 자기 자신 제외
                if len(items) < 100:
                    break
                page += 1
    return runs


def cap_status(team: Team, owners: list[str], kind: str, day: str | None = None
               ) -> dict[str, dict]:
    """토큰 주인별 {used, cap, ok}. kind는 write|review (작성·리뷰 상한을 따로 센다).

    run 목록을 읽지 못하면 전부 ok=False (fail-closed).
    """
    if kind not in CAP_KINDS:
        raise TeamError(f"cap 종류는 write|review: {kind}")
    day = day or datetime.now(UTC).strftime("%Y-%m-%d")
    try:
        runs = todays_claude_runs(day)
        ran = resolve_failed_runs(runs, load_cap_cache(), team)
        save_cap_cache(ran)
        usage = usage_by_owner(team, runs, ran, kind)
    except subprocess.CalledProcessError:
        return {o: {"used": None, "cap": None, "ok": False, "reason": "cap 확인 실패"}
                for o in owners}
    label = "작성" if kind == "write" else "리뷰"
    out = {}
    for o in owners:
        cap = int(team.members.get(o, {}).get(f"{kind}_cap", 0))
        used = usage.get(o, 0)
        name = team.members.get(o, {}).get("name", o)
        out[o] = {"used": used, "cap": cap, "ok": used < cap,
                  "reason": f"{name} 오늘 {label} {used}/{cap}"}
    return out


def gh_api(path: str, *extra: str):
    try:
        out = subprocess.run(
            ["gh", "api", path, *extra], check=True, capture_output=True, text=True,
            encoding="utf-8",
        ).stdout
    except subprocess.CalledProcessError as e:
        # GitHub 에러 메시지만 남긴다 (요청 본문·토큰은 출력하지 않음)
        method = next((extra[i + 1] for i, x in enumerate(extra[:-1]) if x == "-X"), "GET")
        detail = (e.stderr or "").strip().replace("\n", " ")[:500]
        print(f"::warning::gh api {method} {path} 실패: {detail}", file=sys.stderr)
        raise
    return json.loads(out) if out.strip() else None


PR_EVENTS = ("pull_request", "pull_request_target")


def event_pull_request(number: int) -> dict | None:
    """이번 실행을 일으킨 이벤트의 PR 페이로드 (GitHub API 호출 없음).

    PR 이벤트이고 번호가 같을 때만 쓴다. workflow_dispatch처럼 페이로드가 없으면 None
    → 호출한 쪽이 API로 읽는다.
    값은 이벤트 시점 기준이다 (그 뒤에 새 커밋이 오면 새 이벤트가 새 실행을 만든다).
    """
    if os.environ.get("GITHUB_EVENT_NAME") not in PR_EVENTS:
        return None
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            pr = json.load(fh).get("pull_request")
    except (OSError, ValueError):
        return None
    if not isinstance(pr, dict) or pr.get("number") != number:
        return None
    return pr


def post_comment(repo: str, issue: int, body: str) -> None:
    """잠긴 이슈·PR에는 봇도 코멘트를 못 쓴다 → 잠금 해제 → 코멘트 → 같은 이유로 다시 잠금."""
    info = gh_api(f"repos/{repo}/issues/{issue}")
    locked = bool(info and info.get("locked"))
    reason = (info or {}).get("active_lock_reason") or "resolved"
    if locked:
        gh_api(f"repos/{repo}/issues/{issue}/lock", "-X", "DELETE")
    try:
        gh_api(f"repos/{repo}/issues/{issue}/comments", "-X", "POST", "-f", f"body={body}")
    finally:
        if locked:
            gh_api(f"repos/{repo}/issues/{issue}/lock", "-X", "PUT", "-f", f"lock_reason={reason}")


# ── requeue (모든 재대기는 이 함수 하나로) ───────────────────
RETRY_LABELS = ("retry-1", "retry-2", "retry-3")  # retry-2·3은 옛 라벨 (있으면 needs-human으로)
NEEDS_HUMAN = "needs-human"


def next_retry_labels(labels: list[str]) -> tuple[list[str], list[str]]:
    """(붙일 라벨, 뗄 라벨). 첫 실패는 retry-1, 같은 이슈가 두 번째 실패하면 needs-human."""
    current = [lb for lb in RETRY_LABELS if lb in labels]
    remove = ["in-progress", *current]
    if not current:
        return ["retry-1"], remove
    return [NEEDS_HUMAN], remove


def requeue_key(pr: int | None, head_sha: str | None, issue: int, run_id: str | None) -> str:
    if pr:
        return f"pr:{pr}:{head_sha}"
    return f"issue:{issue}:{run_id}"


def requeue(repo: str, team: Team, issue: int, reason: str, pr: int | None = None,
            head_sha: str | None = None, run_id: str | None = None) -> str:
    """PR이 있으면 닫고 브랜치 삭제, 이슈를 다시 열고 retry 라벨을 올린다.

    같은 키로 이미 requeue했으면(봇 코멘트의 표시) 아무것도 안 한다.
    """
    key = requeue_key(pr, head_sha, issue, run_id)
    marker = f"<!-- requeue:{key} -->"
    comments = gh_api(f"repos/{repo}/issues/{issue}/comments?per_page=100") or []
    if any(marker in (c.get("body") or "") and (c.get("user") or {}).get("login") ==
           team.bot_login for c in comments):
        return "already"
    if pr:
        info = gh_api(f"repos/{repo}/pulls/{pr}")
        if info["state"] == "open":
            gh_api(f"repos/{repo}/pulls/{pr}", "-X", "PATCH", "-f", "state=closed")
        branch = info["head"]["ref"]
        same_repo = (info["head"]["repo"] or {}).get("full_name") == repo
        if same_repo and info["user"]["login"] == team.bot_login and team.dept_of_branch(branch):
            subprocess.run(["gh", "api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{branch}"],
                           capture_output=True)
    issue_info = gh_api(f"repos/{repo}/issues/{issue}")
    labels = [lb["name"] for lb in issue_info["labels"]]
    add, remove = next_retry_labels(labels)
    if issue_info["state"] == "closed":  # needs-human이어도 열어 둬야 사람이 본다
        gh_api(f"repos/{repo}/issues/{issue}", "-X", "PATCH", "-f", "state=open")
    for lb in remove:
        if lb in labels:
            subprocess.run(["gh", "api", "-X", "DELETE",
                            f"repos/{repo}/issues/{issue}/labels/{lb}"], capture_output=True)
    gh_api(f"repos/{repo}/issues/{issue}/labels", "-X", "POST",
           *[x for lb in add for x in ("-f", f"labels[]={lb}")])
    where = f" (PR #{pr} 닫음)" if pr else ""
    tail = ("\n같은 이슈가 두 번 실패해 자동 재시도를 멈춥니다. 사람이 확인한 뒤 "
            "needs-human 라벨을 떼고 to: 라벨을 다시 붙이면 재개합니다."
            if NEEDS_HUMAN in add else "")
    post_comment(repo, issue, f"{marker}\n재대기{where}: {reason}\n→ {', '.join(add)}{tail}")
    return ",".join(add)


WRITE_WORKFLOW = "_claude-write.yml"


def write_target(team: Team, labels: list[str]) -> tuple[str, str] | None:
    """이슈 라벨 → (부서, 작성 모드). to:<부서>는 request, dept:<부서>는 task."""
    for prefix, mode in (("to:", "request"), ("dept:", "task")):
        for lb in labels:
            if lb.startswith(prefix) and lb[len(prefix):] in team.depts:
                return lb[len(prefix):], mode
    return None


QUEUE_LABEL = "wip-wait"


def dispatch_write(repo: str, team: Team, issue: int, dept: str | None = None,
                   mode: str | None = None) -> str:
    """이슈를 다시 작성하게 _claude-write를 실행한다 (ax-bot 토큰, actions: write).

    dept·mode를 주면 그대로, 없으면 이슈 라벨(to:→request, dept:→task)로 정한다.
    실행 전에 wip-wait를 붙여 둔다: _claude-write의 concurrency 대기열에서 실행이 밀려 취소돼도
    라벨이 남아 wip-resume·매일 정리가 다시 잡는다 (작성이 시작되면 in-progress 단계가 뗀다).
    run 제목이 claude:<부서>:<모드> #<이슈>라 cap이 작성으로 센다. 실패해도 예외를 올리지 않는다.
    """
    try:
        if not (dept in team.depts and mode in ("task", "request")):
            info = gh_api(f"repos/{repo}/issues/{issue}") or {}
            target = write_target(team, [lb["name"] for lb in info.get("labels", [])])
            if not target:
                return f"#{issue}: to:/dept: 라벨이 없어 다시 실행하지 않음"
            dept, mode = target
        gh_api(f"repos/{repo}/issues/{issue}/labels", "-X", "POST",
               "-f", f"labels[]={QUEUE_LABEL}")
        gh_api(f"repos/{repo}/actions/workflows/{WRITE_WORKFLOW}/dispatches", "-X", "POST",
               "-f", "ref=dev", "-f", f"inputs[dept]={dept}", "-f", f"inputs[mode]={mode}",
               "-f", f"inputs[issue]={issue}")
        return f"#{issue}: {dept}:{mode} 다시 실행"
    except subprocess.CalledProcessError:
        return f"::warning::#{issue}: 다시 실행하지 못함 (재대기만 됨)"


# ── CLI ──────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="team.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("is-member", "is-trusted", "depts-of"):
        sub.add_parser(name).add_argument("login")
    sub.add_parser("owner-token").add_argument("dept")
    sub.add_parser("dept-of-branch").add_argument("branch")
    p = sub.add_parser("check-diff")
    p.add_argument("base")
    p.add_argument("head")
    p.add_argument("--depts", default="")
    p.add_argument("--author-kind", required=True, choices=["human", "bot"])
    p.add_argument("--branch")
    for name in ("classify", "test-guard", "review-required"):
        p = sub.add_parser(name)
        p.add_argument("base")
        p.add_argument("head")
    p = sub.add_parser("reviewers-for")
    p.add_argument("base")
    p.add_argument("head")
    p.add_argument("author_person")
    p = sub.add_parser("arbiter-for")
    p.add_argument("author_dept")
    p.add_argument("reviewer_depts", nargs="+")
    sub.add_parser("export-json").add_argument("--check", action="store_true")
    p = sub.add_parser("cap-ok")
    p.add_argument("login")
    p.add_argument("--kind", required=True, choices=CAP_KINDS)
    p.add_argument("--day", help="UTC 날짜 YYYY-MM-DD (기본 오늘)")
    p = sub.add_parser("requeue")
    p.add_argument("issue", type=int)
    p.add_argument("reason")
    p.add_argument("--pr", type=int)
    p.add_argument("--head-sha")
    p.add_argument("--run-id")
    args = ap.parse_args(argv)

    team = Team.load()
    cmd = args.cmd

    if cmd in ("is-member", "is-trusted"):
        ok = team.is_member(args.login) if cmd == "is-member" else team.is_trusted(args.login)
        print("true" if ok else "false")
        return 0 if ok else 1
    if cmd == "depts-of":
        depts = team.depts_of(args.login)
        print(",".join(depts))
        return 0 if depts else 1
    if cmd == "owner-token":
        dept = team.resolve_dept(args.dept)
        if not dept:
            print(f"알 수 없는 부서: {args.dept}", file=sys.stderr)
            return 1
        print(team.owner_token(dept))
        return 0
    if cmd == "dept-of-branch":
        dept = team.dept_of_branch(args.branch)
        if not dept:
            return 1
        print(dept)
        return 0
    if cmd == "arbiter-for":
        print(team.arbiter_for(args.author_dept, args.reviewer_depts))
        return 0
    if cmd == "export-json":
        text = team.to_json_text()
        if args.check:
            current = TEAM_JSON.read_text(encoding="utf-8") if TEAM_JSON.exists() else ""
            if current != text:
                msg = ".github/team.json이 team.yml과 다름 — team.py export-json 실행"
                print(msg, file=sys.stderr)
                return 1
            return 0
        TEAM_JSON.write_text(text, encoding="utf-8", newline="\n")
        return 0
    if cmd == "cap-ok":
        if not team.is_member(args.login):
            print(f"팀원이 아님: {args.login}", file=sys.stderr)
            return 1
        st = cap_status(team, [args.login], args.kind, args.day)[args.login]
        print(st["reason"])
        return 0 if st["ok"] else 1
    if cmd == "requeue":
        repo = os.environ["GITHUB_REPOSITORY"]
        print(requeue(repo, team, args.issue, args.reason, args.pr, args.head_sha, args.run_id))
        return 0

    mb = merge_base(args.base, args.head)
    changes = diff_changes(mb, args.head)
    if cmd == "classify":
        print(json.dumps(team.classify([c.path for c in changes]).to_json(), ensure_ascii=False))
        return 0
    if cmd == "review-required":
        required = team.review_required(team.classify([c.path for c in changes]))
        print("true" if required else "false")
        return 0 if required else 1
    if cmd == "check-diff":
        depts = [d for d in (team.resolve_dept(x) for x in args.depts.split(",") if x) if d]
        violations = team.check_diff(changes, depts, args.author_kind, args.branch)
    elif cmd == "reviewers-for":
        classified = team.classify([c.path for c in changes])
        reviewers = team.reviewers_for(list(classified.depts), args.author_person,
                                       classified.depts.get("contracts"))
        if not reviewers:
            print("리뷰 부서 없음", file=sys.stderr)
            return 1
        print(",".join(reviewers))
        return 0
    else:  # test-guard
        violations = find_test_weakening(
            changes, lambda p: show(mb, p), lambda p: show(args.head, p), team
        )
    for v in violations:
        print(v)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
