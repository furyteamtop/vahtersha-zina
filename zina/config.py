"""Настройки бота из переменных окружения (или файла .env)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    token: str
    captcha_timeout: int  # сколько секунд даётся на ответ
    fail_ban_minutes: int  # бан за неверный ответ / молчание (0 — не банить)
    db_path: str  # где хранить настройки чатов


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not os.environ.get(key):  # пустая переменная окружения не должна перекрывать .env
            os.environ[key] = value.strip().strip("\"'")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def load_config() -> Config:
    _load_dotenv(Path(".env"))
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Не задан BOT_TOKEN. Получите токен у @BotFather и положите в .env")
    if not re.fullmatch(r"\d+:[\w-]{30,}", token):
        raise SystemExit(
            "BOT_TOKEN в .env не похож на токен. Он выглядит так: 1234567890:AAH... "
            "— скопируйте его из @BotFather целиком."
        )
    return Config(
        token=token,
        captcha_timeout=max(30, _int("CAPTCHA_TIMEOUT", 120)),
        fail_ban_minutes=max(0, _int("FAIL_BAN_MINUTES", 15)),
        db_path=os.environ.get("DB_PATH", "data/zina.db"),
    )
