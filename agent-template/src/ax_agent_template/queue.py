"""직렬 처리 큐 인터페이스 (마케팅 보상 쿠폰 처리용).

순서가 중요한 요청(보상 쿠폰 등)은 `AgentRequest.sequence` 순서대로 한 건씩 처리한다.
앞 건의 결과를 회신한 뒤에 다음 건을 꺼내고, 번호가 건너뛰면 `SEQUENCE_GAP`으로 거부한다.

이 파일은 인터페이스(스텁)만 둔다. 구현은 마케팅 담당이 한다.
"""

from __future__ import annotations

from ax_contracts.schemas import AgentRequest, AgentResult


class SerialQueue:
    """`sequence` 순서대로 한 건씩 처리하는 큐."""

    async def enqueue(self, request: AgentRequest) -> None:
        """요청을 넣는다. `sequence`가 없거나 이미 있는 번호면 거부한다.

        번호가 건너뛰었는지는 `next`에서 판단하고, 건너뛰면 `SEQUENCE_GAP`이다.
        """
        raise NotImplementedError("TODO: 마케팅 구현")

    async def next(self) -> AgentRequest | None:
        """다음 순서의 요청 하나를 꺼낸다. 앞 건이 아직 `ack`되지 않았으면 None이다."""
        raise NotImplementedError("TODO: 마케팅 구현")

    async def ack(self, sequence: int, result: AgentResult) -> None:
        """`sequence`번 요청의 결과를 회신한 것으로 기록한다. 이후에 다음 건을 꺼낼 수 있다."""
        raise NotImplementedError("TODO: 마케팅 구현")
