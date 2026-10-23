"""외부 연결(memory·planning·안전 레이어) 오류. 응답이 없거나 이상하면 지어내지 않는다."""

from __future__ import annotations

from contextvars import ContextVar

from ax_contracts.schemas import RequestMeta


class UpstreamError(RuntimeError):
    """다른 서비스가 응답하지 않았거나 실패·이상한 응답을 돌려줬다."""

    def __init__(self, service: str, detail: str) -> None:
        super().__init__(f"{service}: {detail}")
        self.service = service
        self.detail = detail


# 지금 처리 중인 요청의 메타. 실제 연동 클라이언트가 같은 사건·마감으로 호출하려고 쓴다.
_current_meta: ContextVar[RequestMeta | None] = ContextVar("hr_current_meta", default=None)


def bind_meta(meta: RequestMeta | None) -> None:
    _current_meta.set(meta)


def current_meta() -> RequestMeta | None:
    return _current_meta.get()
