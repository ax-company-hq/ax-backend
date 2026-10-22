"""정책 문서 조각 검색.

TODO: memory(재현)의 정책 문서(MEM-03) 조회 API가 생기면 거기서 받는다. 그 전까지는 이 모듈의
시드 조각을 쓴다. 점수는 한글 2글자 조각의 겹침으로 코드가 계산한다.
"""

from __future__ import annotations

from support_agent.schemas import Source

KB_CHUNKS: list[dict[str, str]] = [
    {
        "id": "refund-1",
        "doc_id": "refund",
        "category": "refund",
        "title": "환불 기준",
        "section": "수령 후 7일",
        "text": "수령 후 7일 이내 단순 변심 환불은 왕복 배송비를 고객이 부담한다.",
    },
    {
        "id": "refund-2",
        "doc_id": "refund",
        "category": "refund",
        "title": "환불 기준",
        "section": "불량·오배송",
        "text": "불량·오배송은 기간과 관계없이 판매자 부담으로 환불한다.",
    },
    {
        "id": "shipping-1",
        "doc_id": "shipping",
        "category": "shipping",
        "title": "배송 안내",
        "section": "배송 기간",
        "text": "결제 후 영업일 기준 2~3일 안에 도착한다. 도서산간은 하루 더 걸린다.",
    },
]


def _grams(text: str) -> set[str]:
    compact = "".join(text.split())
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def search(query: str, limit: int = 3) -> list[Source]:
    """질의와 겹치는 조각을 점수 순으로 돌려준다. 겹침이 없으면 빈 목록이다."""
    wanted = _grams(query)
    if not wanted:
        return []
    scored: list[Source] = []
    for chunk in KB_CHUNKS:
        grams = _grams(f"{chunk['title']} {chunk['section']} {chunk['text']}")
        score = len(wanted & grams) / len(wanted)
        if score > 0:
            scored.append(Source(**chunk, score=round(score, 3)))
    return sorted(scored, key=lambda s: (-s.score, s.id))[:limit]
