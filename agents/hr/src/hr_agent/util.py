"""작은 공통 도구: 결정적 ID, JSON, 텍스트 유사도, 숫자 추출."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def stable_id(prefix: str, *parts: str, length: int = 12) -> str:
    """같은 입력이면 항상 같은 ID (멱등성·결정성). `prefix`는 `card_`처럼 `_`로 끝낸다."""
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:length]}"


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    return json.loads(text)


_WORD = re.compile(r"\w+", re.UNICODE)


def bigrams(text: str) -> set[str]:
    """공백을 뺀 글자 2-gram 집합. 한국어처럼 띄어쓰기가 불규칙한 문장에도 쓸 수 있다."""
    compact = "".join(_WORD.findall(text.lower()))
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def similarity(a: str, b: str) -> float:
    """두 문장의 Dice 유사도 (0~1). 같은 입력이면 항상 같은 값이다."""
    left, right = bigrams(a), bigrams(b)
    if not left or not right:
        return 0.0
    return 2 * len(left & right) / (len(left) + len(right))


def overlap(a: str, b: str) -> float:
    """짧은 쪽 2-gram이 긴 쪽에 얼마나 들어 있는지(0~1). 길이가 크게 다른 문장을 비교할 때 쓴다."""
    left, right = bigrams(a), bigrams(b)
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> set[str]:
    """문장에 들어 있는 숫자(쉼표 제거). LLM 문장 속 숫자를 계산값과 대조할 때 쓴다."""
    return {m.replace(",", "") for m in _NUMBER.findall(text)}
