"""Вопросы-«пропуски» для проверки, что пишет человек."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import List

GREETINGS = [
    "Ты к кому, милок?",
    "Стой, кто идёт?",
    "Куда без пропуска?!",
    "Ноги вытер? Теперь докажи, что не робот.",
    "Тут тебе не проходной двор!",
]

EMOJI = [
    ("🍎", "яблоко"), ("🐱", "котика"), ("🚗", "машинку"), ("🌵", "кактус"),
    ("⚽", "мячик"), ("🎸", "гитару"), ("🍌", "банан"), ("🐟", "рыбку"),
    ("🔑", "ключ"), ("☂️", "зонтик"), ("🧦", "носок"), ("🥒", "огурец"),
    ("🐸", "лягушку"), ("🎈", "шарик"), ("🍄", "грибок"), ("🧶", "клубок"),
]

NUMBER_WORDS = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]


@dataclass
class Challenge:
    task: str  # HTML-текст задания
    options: List[str]
    correct: int  # индекс правильного варианта


def _emoji() -> Challenge:
    picked = random.sample(EMOJI, 6)
    correct = random.randrange(len(picked))
    return Challenge(f"Нажми на <b>{picked[correct][1]}</b>.", [e for e, _ in picked], correct)


def _math() -> Challenge:
    a, b = random.randint(2, 9), random.randint(2, 9)
    answer = a + b
    options = {answer}
    while len(options) < 4:
        options.add(answer + random.choice([-3, -2, -1, 1, 2, 3]))
    shuffled = random.sample(sorted(options), len(options))
    return Challenge(f"Сколько будет <b>{a} + {b}</b>?", [str(o) for o in shuffled], shuffled.index(answer))


def _number_word() -> Challenge:
    digits = random.sample(range(10), 4)
    correct = random.randrange(len(digits))
    return Challenge(
        f"Нажми на число <b>«{NUMBER_WORDS[digits[correct]]}»</b>.", [str(d) for d in digits], correct
    )


def generate() -> Challenge:
    return random.choice([_emoji, _math, _number_word])()


def greeting() -> str:
    return random.choice(GREETINGS)
