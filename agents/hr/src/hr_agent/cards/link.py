"""기존 결정과 연결: 같은 영역의 새 결정이 옛 결정을 바꾸면 옛 카드를 `superseded`로 돌린다.

바꾼다고 보는 조건(규칙):
- 새 카드와 옛 카드가 모두 영역(`area`)이 있고 같다.
- 둘 다 결정 카드이고 "채택(adopted)"한 결정이며, 옛 카드는 후보가 아닌 유효(active) 카드다.
- 결정 문장이 비슷하지만(같은 주제, 유사도 하한 이상) 문장은 다르다.
기각한 안(`rejected`)은 다른 결정을 대체하지 않는다.
"""

from __future__ import annotations

from ax_contracts.schemas import Card
from hr_agent.clients.memory import MemoryClient
from hr_agent.util import similarity

SAME_TOPIC = 0.5  # 같은 주제로 보는 결정 문장 유사도 하한(제안값)


def _decision_text(card: Card) -> str:
    return str(card.extra.get("decision") or card.title)


async def find_superseded(card: Card, memory: MemoryClient) -> list[str]:
    """새 카드가 대체하는 옛 카드 ID 목록."""
    area = card.extra.get("area")
    if (
        not area
        or card.kind != "decision"
        or card.extra.get("outcome") != "adopted"
        or card.extra.get("candidate")
    ):
        return []
    older = await memory.list_cards(area=area, states=["active"])
    result: list[str] = []
    new_text = _decision_text(card)
    for old in older:
        if (
            old.card_id == card.card_id
            or old.kind != "decision"
            or old.extra.get("outcome") != "adopted"
        ):
            continue
        old_text = _decision_text(old)
        if old_text != new_text and similarity(old_text, new_text) >= SAME_TOPIC:
            result.append(old.card_id)
    return sorted(result)
