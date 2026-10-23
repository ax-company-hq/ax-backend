"""문의와 기획개발 변경 요청의 연결 저장소 (메모리, `persistence`가 DB로 영속화한다).

고객지원이 기획개발에 변경을 요청하면(2-2, 통합 에이전트를 거치지 않는 직통) 기획개발이 변경 요청
사건 `ctx_req_…`를 **발급**해 돌려준다 (`ContextPrefix.REQUEST`). 같은 요청을 여러 부서·여러 문의가
보내면 기획개발이 하나로 묶으므로(`linked_duplicate`) **한 변경 요청에 문의가 여러 개** 연결될 수
있다. 한 문의에 요청이 여러 건일 수도 있다. 그래서 연결은 (`context_id`, `inquiry_id`) 한 쌍이 한
행이다.

요청을 보낼 때 문의 ID는 `source_ref`(`inq_<번호>`)로 보내고(`source_ref_for`), 돌려받은
`context_id`로 이 저장소에 이어 둔다. 결과 통지는 같은 `context_id`에 `path`를 비운 새 요청으로
오고, 그 `context_id`에 연결된 **모든 문의**에 같은 결과를 적용한다 (`record_result`).

이 모듈은 저장만 한다. 요청을 보내는 skill(`create_ticket` 호출)과 결과를 받는 skill은
계약(#172)이 머지된 뒤에 붙인다. 상태 이름은 계약에 맞춰 `CHANGE_STATUSES` 한 곳에서 바꾼다.

- `requested`: 요청을 보냈고 결과를 기다리는 중
- `completed`: 처리됨 (머지까지 끝남). 고객 공지가 필요하면 `notice_id`를 나중에 붙인다
- `declined`: 처리하지 않기로 함
"""

from __future__ import annotations

from typing import Any, Literal

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel
from ax_contracts.schemas.ids import ContextPrefix, IdPrefix
from support_agent.store import now_iso

ChangeStatus = Literal["requested", "completed", "declined"]
CHANGE_STATUSES: tuple[str, ...] = ("requested", "completed", "declined")
TERMINAL = ("completed", "declined")


def source_ref_for(inquiry_id: int) -> str:
    """기획개발에 보내는 요청 부서 쪽 참조 ID (`inq_<번호>`)."""
    return f"{IdPrefix.INQUIRY.value}{inquiry_id}"


def inquiry_id_from(source_ref: str) -> int | None:
    """`source_ref_for`의 역변환. 우리 형식이 아니면 None."""
    prefix = IdPrefix.INQUIRY.value
    if source_ref.startswith(prefix) and source_ref[len(prefix) :].isdecimal():
        return int(source_ref[len(prefix) :])
    return None


class ChangeRequestConflict(Exception):
    """이미 끝난 변경 요청이나 붙어 있는 공지와 모순되는 요청이다."""


class ChangeRequestLink(AxModel):
    tenant_id: str = TENANT_ID_DEFAULT
    context_id: str  # 기획개발이 발급한 변경 요청 사건 (ctx_req_…)
    inquiry_id: int
    source_ref: str  # 이 문의를 가리켜 보낸 참조 ID (inq_<번호>)
    status: ChangeStatus = "requested"
    request_id: str | None = None  # 기획개발 쪽 요청 번호 (REQ-n)
    duplicate_of: str | None = None  # 같은 요청이 이미 있어 묶였다면 그 요청 번호
    result: dict[str, Any] | None = None  # 기획개발이 알려 준 처리 결과 (그대로 보관)
    notice_id: str | None = None  # 고객 공지가 필요했다면 마케팅 공지 ID
    seq: int  # 연결한 순서. 시각은 초 단위라 같은 초에 만든 것의 순서를 알 수 없다
    created_at: str
    updated_at: str


class ChangeRequestStore:
    def __init__(self) -> None:
        self._links: dict[tuple[str, int], ChangeRequestLink] = {}

    def _put(self, link: ChangeRequestLink) -> ChangeRequestLink:
        """저장 지점. 영속 저장소가 여기서 DB에도 쓴다."""
        self._links[(link.context_id, link.inquiry_id)] = link
        return link

    # ---- 읽기
    def get(self, context_id: str, inquiry_id: int) -> ChangeRequestLink | None:
        return self._links.get((context_id, inquiry_id))

    def for_context(self, context_id: str) -> list[ChangeRequestLink]:
        """한 변경 요청에 연결된 모든 문의 (연결한 순서)."""
        links = [link for link in self._links.values() if link.context_id == context_id]
        return sorted(links, key=lambda link: link.seq)

    def for_inquiry(self, inquiry_id: int) -> list[ChangeRequestLink]:
        links = [link for link in self._links.values() if link.inquiry_id == inquiry_id]
        return sorted(links, key=lambda link: link.seq)

    def pending(self) -> list[ChangeRequestLink]:
        """결과를 기다리는 연결 (연결한 순서)."""
        links = [link for link in self._links.values() if link.status == "requested"]
        return sorted(links, key=lambda link: link.seq)

    # ---- 쓰기 (호출하는 쪽이 `execute()`를 거친 뒤에만 부른다)
    def link(
        self,
        context_id: str,
        inquiry_id: int,
        *,
        request_id: str | None = None,
        duplicate_of: str | None = None,
        tenant_id: str = TENANT_ID_DEFAULT,
    ) -> ChangeRequestLink:
        """문의와 변경 요청을 잇는다. 같은 연결을 다시 요청하면 기존 것을 돌려준다.

        이미 끝난 요청에 새 문의가 묶이면(기획개발이 같은 요청으로 판단) 그 결과를 이어받는다.
        그래야 결과 통지를 다시 받을 일 없이 바로 그 문의에 답할 수 있다.
        """
        if not context_id.startswith(ContextPrefix.REQUEST.value):
            raise ValueError(f"변경 요청 사건 ID는 {ContextPrefix.REQUEST.value}로 시작한다")
        existing = self._links.get((context_id, inquiry_id))
        if existing is not None:
            return existing  # 같은 연결의 재요청 (멱등)
        siblings = self.for_context(context_id)
        inherited: dict[str, Any] = {}
        if siblings:
            first = siblings[0]
            inherited = {
                "status": first.status,
                "result": first.result,
                "notice_id": first.notice_id,
                "request_id": request_id or first.request_id,
            }
        stamp = now_iso()
        fields: dict[str, Any] = {
            "tenant_id": tenant_id,
            "context_id": context_id,
            "inquiry_id": inquiry_id,
            "source_ref": source_ref_for(inquiry_id),
            "request_id": request_id,
            "duplicate_of": duplicate_of,
            "seq": max((link.seq for link in self._links.values()), default=0) + 1,
            "created_at": stamp,
            "updated_at": stamp,
        }
        fields.update({k: v for k, v in inherited.items() if v is not None})
        return self._put(ChangeRequestLink(**fields))

    def record_result(
        self,
        context_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
    ) -> list[ChangeRequestLink]:
        """처리 결과를 기록한다. 그 요청에 연결된 모든 문의에 같은 결과가 적용된다.

        같은 결과를 다시 받으면 재전송으로 보고 그대로 두고, 다른 결과면 충돌이다.
        """
        links = self.for_context(context_id)
        if not links:
            raise KeyError(context_id)
        if status not in TERMINAL:
            raise ValueError(f"결과 상태는 {TERMINAL} 중 하나여야 한다: {status}")
        current = links[0]
        if current.status in TERMINAL:
            if current.status == status and current.result == result:
                return links  # 같은 통지의 재전송
            raise ChangeRequestConflict(f"{context_id}는 이미 {current.status}로 끝났다")
        stamp = now_iso()
        return [
            self._put(
                link.model_copy(update={"status": status, "result": result, "updated_at": stamp})
            )
            for link in links
        ]

    def attach_notice(self, context_id: str, notice_id: str) -> list[ChangeRequestLink]:
        """처리가 끝난 요청에 고객 공지 ID를 붙인다 (연결된 모든 문의에, 같은 ID는 그대로)."""
        links = self.for_context(context_id)
        if not links:
            raise KeyError(context_id)
        current = links[0]
        if current.status != "completed":
            raise ChangeRequestConflict("처리가 끝난(completed) 요청에만 공지를 붙일 수 있다")
        if current.notice_id == notice_id:
            return links
        if current.notice_id is not None:
            raise ChangeRequestConflict(
                f"{context_id}에는 이미 공지 {current.notice_id}가 붙어 있다"
            )
        stamp = now_iso()
        return [
            self._put(link.model_copy(update={"notice_id": notice_id, "updated_at": stamp}))
            for link in links
        ]
