"""고객 메시지의 언어를 문자 종류로 판별한다 (코드만, LLM 없음).

지원하는 답변 언어는 한국어(`ko`)·영어(`en`)·일본어(`ja`)·중국어(`zh`)다. 답변 가드(`guard.py`)가
이 네 언어의 약속 표현을 검사할 수 있기 때문이다. 그 밖의 언어로 온 문의는 영어로 답한다.

판별 규칙(글자 수가 가장 많은 쪽): 한글 → `ko`, 가나 → `ja`, 한자(가나·한글 없음) → `zh`,
라틴 문자 → `en`, 그 밖의 문자(키릴·아랍·태국어 등) → `other`. 글자가 없으면(숫자·기호뿐) `ko`다.
라틴 문자 언어는 영어와 구분하지 못한다(스페인어 등도 `en`). 그래서 가드는 악센트 있는 라틴 글자가
있으면 경고한다.
"""

from __future__ import annotations

import re

SUPPORTED = ("ko", "en", "ja", "zh")
NAMES = {"ko": "Korean", "en": "English", "ja": "Japanese", "zh": "Chinese"}

_HANGUL = re.compile(r"[ᄀ-ᇿ㄰-㆏가-힯]")
_KANA = re.compile(r"[぀-ゟ゠-ヿㇰ-ㇿ]")
_HAN = re.compile(r"[㐀-䶿一-鿿]")
_LATIN = re.compile(r"[A-Za-zÀ-ɏ]")
_OTHER = re.compile(r"[Ѐ-ӿ֐-ۿݐ-ݿ฀-๿ऀ-ॿ]")
ACCENTED_LATIN = re.compile(r"[À-ÿĀ-ɏ]")


def detect(text: str) -> str:
    """`ko`·`en`·`ja`·`zh`·`other` 중 하나."""
    counts = {
        "ko": len(_HANGUL.findall(text)),
        "ja": len(_KANA.findall(text)),
        "zh": len(_HAN.findall(text)),
        "en": len(_LATIN.findall(text)),
        "other": len(_OTHER.findall(text)),
    }
    if counts["ja"]:  # 일본어 문장에는 한자가 섞이므로 가나가 있으면 일본어다
        counts["ja"] += counts["zh"]
        counts["zh"] = 0
    if counts["ko"]:  # 한국어 문장에도 한자·영문이 섞인다
        counts["zh"] = 0
    language, count = max(counts.items(), key=lambda item: item[1])
    return language if count else "ko"


def reply_language(detected: str) -> str:
    """고객에게 답할 언어. 가드가 검사하지 못하는 언어는 영어로 답한다."""
    return detected if detected in SUPPORTED else "en"
