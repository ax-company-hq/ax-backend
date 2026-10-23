"""문의와 기획개발 변경 요청의 연결 저장소 (프로세스 메모리, `persistence`가 DB로 영속화한다).

고객지원이 기획개발에 변경을 요청하면(2-2), 기획개발이 변경 요청 사건 `ctx_req_…`를 발급해 돌려준다
(`ContextPrefix.REQUEST`, 발급 주체는 planning). 나중에 처리 결과를 받았을 때 **어느 문의에 답할지**
알려면 문의와 이 사건을 이어 둬야 한다. 한 문의에 변경 요청이 여러 건일 수 있고, 한 변경 요청은
문의 하나에 묶인다.

이 모듈은 저장만 한다. 요청을 보내는 skill과 결과를 받는 skill은 계약(기획개발 접수 skill, 결과 통지
형식)이 정해진 뒤에 붙인다. 상태 이름은 그 계약에 맞춰 `CHANGE_STATUSES` 한 곳에서 바꾼다.

- `requested`: 요청을 보냈고 결과를 기다리는 중
- `completed`: 처리됨 (머지까지 끝남). 고객 공지가 필요하면 `notice_id`를 나중에 붙인다
- `declined`: 처리하지 않기로 함
"""

from __future__ import annotations

from typing import Any, Literal

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel
from ax_contracts.schemas.ids import ContextPrefix
from support_agent.store import now_iso

ChangeStatus = Literal["requested", "completed", "declined"]
CHANGE_STATUSES: tuple[str, ...] = ("requested", "completed", "declined")
TERMINAL = ("completed", "declined")


class ChangeRequestConflict(Exception):
    """이미 연결·종결된 변경 요청과 모순되는 요청이다."""


class ChangeRequestLink(AxModel):
    tenant_id: str = TENANT_ID_DEFAULT
    context_id: str  # 기획개발이 발급한 변경 요청 사건 (ctx_req_…)
    inquiry_id: int
    status: ChangeStatus = "requested"
    request_ref: str | None = None  # 기획개발 쪽 요청 번호 (있으면)
    result: dict[str, Any] | None = None  # 기획개발이 알려 준 처리 결과 (그대로 보관)
    notice_id: str | None = None  # 고객 공지가 필요했다면 마케팅 공지 ID
    seq: int  # 연결한 순서. 시각은 초 단위라 같은 초에 만든 것의 순서를 알 수 없다
    created_at: str
    updated_at: str


class ChangeRequestStore:
    def __init__(self) -> None:
        self._links: dict[str, ChangeRequestLink] = {}

    def _put(self, link: ChangeRequestLink) -> ChangeRequestLink:
        """저장 지점. 영속 저장소가 여기서 DB에도 쓴다."""
        self._links[link.context_id] = link
        return link

    # ---- 읽기
    def get(self, context_id: str) -> ChangeRequestLink | None:
        return self._links.get(context_id)

    def for_inquiry(self, inquiry_id: int) -> list[ChangeRequestLink]:
        links = [link for link in self._links.values() if link.inquiry_id == inquiry_id]
        return sorted(links, key=lambda link: link.seq)

    def pending(self) -> list[ChangeRequestLink]:
        """결과를 기다리는 요청 (오래된 순)."""
        links = [link for link in self._links.values() if link.status == "requested"]
        return sorted(links, key=lambda link: link.seq)

    # ---- 쓰기 (호출하는 쪽이 `execute()`를 거친 뒤에만 부른다)
    def link(
        self,
        context_id: str,
        inquiry_id: int,
        *,
        request_ref: str | None = None,
        tenant_id: str = TENANT_ID_DEFAULT,
    ) -> ChangeRequestLink:
        """문의와 변경 요청을 잇는다. 같은 연결을 다시 요청하면 기존 것을 돌려준다."""
        if not context_id.startswith(ContextPrefix.REQUEST.value):
            raise ValueError(f"변경 요청 사건 ID는 {ContextPrefix.REQUEST.value}로 시작한다")
        existing = self._links.get(context_id)
        if existing is not None:
            if existing.inquiry_id != inquiry_id:
                raise ChangeRequestConflict(
                    f"{context_id}는 이미 문의 {existing.inquiry_id}에 연결되어 있다"
                )
            return existing  # 같은 연결의 재요청 (멱등)
        stamp = now_iso()
        return self._put(
            ChangeRequestLink(
                tenant_id=tenant_id,
                context_id=context_id,
                inquiry_id=inquiry_id,
                request_ref=request_ref,
                seq=max((link.seq for link in self._links.values()), default=0) + 1,
                created_at=stamp,
                updated_at=stamp,
            )
        )

    def record_result(
        self,
        context_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
    ) -> ChangeRequestLink:
        """처리 결과를 기록한다. 같은 결과를 다시 받으면 그대로 두고, 다른 결과면 충돌이다."""
        link = self._links.get(context_id)
        if link is None:
            raise KeyError(context_id)
        if status not in TERMINAL:
            raise ValueError(f"결과 상태는 {TERMINAL} 중 하나여야 한다: {status}")
        if link.status in TERMINAL:
            if link.status == status and link.result == result:
                return link  # 같은 통지의 재전송
            raise ChangeRequestConflict(f"{context_id}는 이미 {link.status}로 끝났다")
        return self._put(
            link.model_copy(update={"status": status, "result": result, "updated_at": now_iso()})
        )

    def attach_notice(self, context_id: str, notice_id: str) -> ChangeRequestLink:
        """처리가 끝난 요청에 고객 공지 ID를 붙인다 (같은 ID를 다시 붙이면 그대로)."""
        link = self._links.get(context_id)
        if link is None:
            raise KeyError(context_id)
        if link.status != "completed":
            raise ChangeRequestConflict("처리가 끝난(completed) 요청에만 공지를 붙일 수 있다")
        if link.notice_id == notice_id:
            return link
        if link.notice_id is not None:
            raise ChangeRequestConflict(f"{context_id}에는 이미 공지 {link.notice_id}가 붙어 있다")
        return self._put(link.model_copy(update={"notice_id": notice_id, "updated_at": now_iso()}))
