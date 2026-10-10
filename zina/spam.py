"""Распознавание спама по тексту.

Спамеры маскируют слова: пишут латиницей вперемешку с кириллицей («пoрнo» с
латинской «o»), «жирными» юникод-буквами (𝐩𝐨𝐫𝐧), через пробелы («п о р н о»)
и невидимые символы. Поэтому текст сначала нормализуется в несколько вариантов,
и каждый вариант проверяется по шаблонам.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List

_INVISIBLE = re.compile("[­​-‏‪-‮⁠-⁤﻿]")
# «п о р н о», «p.o.r.n» -> «порно», «porn»
_SPACED_LETTERS = re.compile(r"\b(?:\w[\s.\-_*•|/]+){2,}\w\b")
_SPACED_SEP = re.compile(r"[\s.\-_*•|/]+")

_LAT = "aceopxykmthb03"
_CYR = "асеорхукмтнвоз"
_TO_CYR = str.maketrans(_LAT, _CYR)
_TO_LAT = str.maketrans(_CYR[:-2], _LAT[:-2])

_PATTERNS = [
    # порно / интим
    r"порн", r"порев", r"эроти[кч]", r"\bинтим", r"\bсекс\w*\s+(знаком|чат|видео|фото)",
    r"знакомств\w*\s+(для\s+взросл|без\s+обязательств|на\s+ночь|18)",
    r"голы[еxх]\s+(фото|видео|девуш)", r"обнаж[её]нн", r"\bнюдс", r"\bнюдес",
    r"шлюх", r"проститут", r"\bэскорт", r"\bвирт\b", r"\bминет", r"\bмастурб", r"\bдроч",
    r"сливы?\s+(фото|видео|девуш|онлифанс|приват)", r"приватн\w*\s+(фото|видео|контент|канал)",
    r"горяч\w*\s+(фото|видео|девуш|контент|штучк)",
    r"18\s*\+.{0,30}(фото|видео|контент|канал|девуш)", r"🔞",
    r"porn", r"\bxxx\b", r"only\s*fans", r"\bnudes?\b", r"\bsexy?\s+(girls?|chat|video|cam)",
    r"\bescort", r"\bhot\s+(girls?|videos?|pics)",
    # «пиши в лс», «ссылка в профиле», лёгкий заработок
    r"(пиш|напиш)\w*\s+(мне\s+)?в\s+(лс|личк|личные)",
    r"(смотри|загляни|заходи|переходи)\w*\s+(в\s+|на\s+)?(мой\s+|мою\s+)?(профил|био|аватар|аву)",
    r"(ссылк\w*|подробн\w*|всё|все)\s+в\s+(профил|био|описани)", r"в\s+мо[её]м\s+профил",
    r"без\s+вложени", r"пассивн\w+\s+доход", r"заработ\w*\s+(от\s+)?\d+", r"доход\w*\s+от\s+\d+",
]
_REGEX = re.compile("|".join(f"(?:{p})" for p in _PATTERNS))


def _collapse_spaced(text: str) -> str:
    return _SPACED_LETTERS.sub(lambda m: _SPACED_SEP.sub("", m.group(0)), text)


def variants(text: str) -> List[str]:
    """Нормализованные варианты текста для проверки."""
    base = unicodedata.normalize("NFKC", text)
    base = _INVISIBLE.sub("", base).lower().replace("ё", "е")
    base = _collapse_spaced(base)
    return [base, base.translate(_TO_CYR), base.translate(_TO_LAT)]


def is_spam(text: str, custom_words: Iterable[str] = ()) -> bool:
    if not text:
        return False
    vs = variants(text)
    if any(_REGEX.search(v) for v in vs):
        return True
    for word in custom_words:
        w = variants(word)
        if any(wv and wv in v for wv, v in zip(w, vs)):
            return True
    return False
