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
  test-guard <base> <head>
  export-json [--check]                      .github/team.json 생성 (--check: 동기화 검사만)
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from dataclasses import dataclass, field
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
        self._validate()

    @classmethod
    def load(cls, path: Path = TEAM_YML) -> Team:
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")))

    def _validate(self) -> None:
        for dept_id, dept in self.depts.items():
            for owner in dept["owners"]:
                if owner not in self.members:
                    raise TeamError(f"{dept_id}의 owner {owner}가 members에 없음")
        for dept_id, revs in self.reviewers.items():
            for r in [dept_id, *revs]:
                if r not in self.depts:
                    raise TeamError(f"reviewers의 {r}가 depts에 없음")
        for d in [*self.fallback_order, *filter(None, [self.contracts_fallback])]:
            if d not in self.depts:
                raise TeamError(f"fallback 부서 {d}가 depts에 없음")

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
        return self.depts[dept]["owners"]

    def owner_token(self, dept: str) -> str:
        owner = self.owners(dept)[0]
        return self.members[owner]["token"]

    def dept_of_branch(self, branch: str) -> str | None:
        if "/" not in branch:
            return None
        return self.resolve_dept(branch.split("/", 1)[0])

    def depts_of(self, login: str) -> list[str]:
        return [d for d, info in self.depts.items() if login in info["owners"]]

    # ── 경로 ─────────────────────────────────────────────
    def dept_of_path(self, path: str) -> str | None:
        """경로가 속한 부서. docs/notes/<부서>/는 그 부서. 없으면 None (human_paths 또는 shared)."""
        if path.startswith(self.notes_prefix):
            rest = path[len(self.notes_prefix) :]
            if "/" in rest:
                return self.resolve_dept(rest.split("/", 1)[0])
            return None
        for dept_id, dept in self.depts.items():
            folder = dept["folder"].rstrip("/")
            if path == folder or path.startswith(folder + "/"):
                return dept_id
        return None

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

    def reviewers_for(self, changed_depts: list[str], author_person: str) -> list[str]:
        """리뷰 부서 목록 (순서 유지, 중복 없음). 같은 사람이면 대체한다."""
        result: list[str] = []

        def take_fallback() -> str | None:
            for f in self.fallback_order:
                if author_person not in self.owners(f) and f not in result:
                    return f
            return None

        for dept in changed_depts:
            for reviewer in self.reviewers.get(dept, []):
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


def find_test_weakening(changes: list[Change], read_base, read_head) -> list[str]:
    """tests 삭제, test_ 함수 수 감소, assert 50% 이상 감소를 찾는다."""
    violations: list[str] = []
    base_tests = head_tests = base_asserts = head_asserts = 0
    for c in changes:
        if not is_test_path(c.path):
            continue
        if c.status == "D":
            violations.append(f"{c.path}: 테스트 파일 삭제")
        try:
            b = count_tests_and_asserts(read_base(c.path)) if c.status != "A" else (0, 0)
            h = count_tests_and_asserts(read_head(c.path)) if c.status != "D" else (0, 0)
        except (SyntaxError, ValueError, RecursionError, MemoryError) as e:
            violations.append(f"{c.path}: 파싱 실패 ({type(e).__name__})")
            continue
        base_tests, head_tests = base_tests + b[0], head_tests + h[0]
        base_asserts, head_asserts = base_asserts + b[1], head_asserts + h[1]
        if c.status == "M" and b[1] > 0 and h[1] * 2 <= b[1]:
            violations.append(f"{c.path}: 테스트 함수 안 assert {b[1]} → {h[1]} (50% 이상 감소)")
    if head_tests < base_tests:
        violations.append(f"test_ 함수 수 감소: {base_tests} → {head_tests}")
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
    for name in ("classify", "test-guard"):
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

    mb = merge_base(args.base, args.head)
    changes = diff_changes(mb, args.head)
    if cmd == "classify":
        print(json.dumps(team.classify([c.path for c in changes]).to_json(), ensure_ascii=False))
        return 0
    if cmd == "check-diff":
        depts = [d for d in (team.resolve_dept(x) for x in args.depts.split(",") if x) if d]
        violations = team.check_diff(changes, depts, args.author_kind, args.branch)
    elif cmd == "reviewers-for":
        changed = list(team.classify([c.path for c in changes]).depts)
        reviewers = team.reviewers_for(changed, args.author_person)
        if not reviewers:
            print("리뷰 부서 없음", file=sys.stderr)
            return 1
        print(",".join(reviewers))
        return 0
    else:  # test-guard
        violations = find_test_weakening(
            changes, lambda p: show(mb, p), lambda p: show(args.head, p)
        )
    for v in violations:
        print(v)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
