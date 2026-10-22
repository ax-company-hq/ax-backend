"""`generate_pr`: PR 생성 요청을 `execute()`로 보낸다.

안전 레이어가 코드 규칙으로 검증하므로(#83) params에 변경 규모를 항상 넣는다.
줄·파일 수는 LLM이 아니라 변경 목록에서 코드로 계산한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ax_agent_template.execute import execute
from ax_contracts.schemas import Evidence, ExecuteDecision, RequestMeta


@dataclass(frozen=True)
class ChangedFile:
    path: str  # 레포 루트 기준
    additions: int
    deletions: int


def build_params(
    title: str, branch: str, files: list[ChangedFile], body: str = ""
) -> dict:
    """`generate_pr` execute params. 변경 줄·파일·경로를 함께 담는다."""
    paths = sorted({f.path for f in files})
    return {
        "title": title,
        "branch": branch,
        "body": body,
        "changed_lines": sum(f.additions + f.deletions for f in files),
        "changed_files": len(paths),
        "changed_paths": paths,
    }


async def generate_pr(
    title: str,
    branch: str,
    files: list[ChangedFile],
    context: RequestMeta,
    evidence: Evidence,
    body: str = "",
) -> ExecuteDecision:
    return await execute("generate_pr", build_params(title, branch, files, body), context, evidence)
