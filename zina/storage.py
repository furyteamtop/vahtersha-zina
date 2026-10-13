"""Хранилище.

На диск пишутся ТОЛЬКО настройки, которые админ поменял сам:
- для группы: id, список стоп-слов и флаг «удалять комментарии от имени каналов»;
- для канала: id, подпись к постам, кнопки-ссылки и флаг автоподписи.
Никаких данных о пользователях и сообщениях здесь нет.

Всё про пользователей (кто прошёл проверку, кто сейчас проходит и его
комментарий, ждущий проверки, черновик поста) живёт только в оперативной
памяти и исчезает после ответа на вопрос / публикации или при перезапуске бота.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

if TYPE_CHECKING:
    from aiogram.types import Message

Key = Tuple[int, int]  # (chat_id, user_id)


@dataclass
class ChatSettings:
    words: List[str] = field(default_factory=list)
    block_channels: bool = True


Buttons = List[List[Dict[str, str]]]  # ряды кнопок: [[{"text": ..., "url": ...}, ...], ...]


@dataclass
class ChannelSettings:
    signature: str = ""  # HTML, дописывается в конец поста
    buttons: Buttons = field(default_factory=list)
    auto: bool = True  # дописывать к постам, опубликованным прямо в канале
    comments: bool = True  # своя кнопка «Комментарии»: у постов с кнопками Telegram прячет свою


class Storage:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS chats ("
            " chat_id INTEGER PRIMARY KEY,"
            " words TEXT NOT NULL DEFAULT '[]',"
            " block_channels INTEGER NOT NULL DEFAULT 1)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS channels ("
            " chat_id INTEGER PRIMARY KEY,"
            " signature TEXT NOT NULL DEFAULT '',"
            " buttons TEXT NOT NULL DEFAULT '[]',"
            " auto INTEGER NOT NULL DEFAULT 1)"
        )
        try:  # колонка добавлена позже — докидываем в старые базы
            self._db.execute("ALTER TABLE channels ADD COLUMN comments INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        self._db.commit()
        self._cache: Dict[int, ChatSettings] = {}
        self._channels: Dict[int, ChannelSettings] = {}

    def channel(self, chat_id: int) -> ChannelSettings:
        if chat_id not in self._channels:
            row = self._db.execute(
                "SELECT signature, buttons, auto, comments FROM channels WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            self._channels[chat_id] = (
                ChannelSettings(row[0], json.loads(row[1]), bool(row[2]), bool(row[3])) if row else ChannelSettings()
            )
        return self._channels[chat_id]

    def save_channel(self, chat_id: int, settings: ChannelSettings) -> None:
        self._channels[chat_id] = settings
        self._db.execute(
            "INSERT OR REPLACE INTO channels (chat_id, signature, buttons, auto, comments) VALUES (?, ?, ?, ?, ?)",
            (chat_id, settings.signature, json.dumps(settings.buttons, ensure_ascii=False),
             int(settings.auto), int(settings.comments)),
        )
        self._db.commit()

    def delete_channel(self, chat_id: int) -> None:
        self._channels.pop(chat_id, None)
        self._db.execute("DELETE FROM channels WHERE chat_id = ?", (chat_id,))
        self._db.commit()

    def get(self, chat_id: int) -> ChatSettings:
        if chat_id not in self._cache:
            row = self._db.execute(
                "SELECT words, block_channels FROM chats WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            self._cache[chat_id] = (
                ChatSettings(json.loads(row[0]), bool(row[1])) if row else ChatSettings()
            )
        return self._cache[chat_id]

    def save(self, chat_id: int, settings: ChatSettings) -> None:
        self._cache[chat_id] = settings
        self._db.execute(
            "INSERT OR REPLACE INTO chats (chat_id, words, block_channels) VALUES (?, ?, ?)",
            (chat_id, json.dumps(settings.words, ensure_ascii=False), int(settings.block_channels)),
        )
        self._db.commit()

    def delete(self, chat_id: int) -> None:
        self._cache.pop(chat_id, None)
        self._db.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
        self._db.commit()


@dataclass
class Pending:
    correct: int
    message_id: Optional[int] = None
    task: Optional[asyncio.Task] = None
    # Комментарии новичка, ждущие проверки. Только в памяти: после ответа или таймаута стираются.
    messages: List["Message"] = field(default_factory=list)


@dataclass
class Album:
    items: List["Message"] = field(default_factory=list)
    done: bool = False
    task: Optional[asyncio.Task] = None


@dataclass
class ChatStats:
    deleted: int = 0
    passed: int = 0
    failed: int = 0


class Memory:
    """Состояние в оперативной памяти. На диск не попадает."""

    VERIFIED_LIMIT = 200_000
    CACHE_TTL = 600

    def __init__(self) -> None:
        self.verified: Dict[Key, None] = {}  # dict как упорядоченное множество
        self.pending: Dict[Key, Pending] = {}
        self.stats: Dict[int, ChatStats] = {}
        # кого Зина забанила: chat_id -> {user_id: (имя, до какого времени или None)}. Только для /unban.
        self.bans: Dict[int, Dict[int, Tuple[str, Optional[int]]]] = {}
        self._admins: Dict[int, Tuple[float, Set[int]]] = {}
        self._linked: Dict[int, Tuple[float, Optional[int]]] = {}
        # постер
        self.drafts: Dict[int, Tuple[int, "Message"]] = {}  # user_id -> (канал, черновик поста)
        self.user_channels: Dict[int, List[int]] = {}  # недавние каналы админа — для быстрого выбора
        self.albums: Dict[str, Album] = {}  # альбомы, которые сейчас подписываются
        # (канал, пост) -> номер копии поста в группе обсуждений, корень ветки комментариев
        self.thread_roots: Dict[Key, int] = {}
        # посты с нашими кнопками, ждущие кнопку «Комментарии»: (канал, пост) -> (чат канала, кнопки)
        self.awaiting_comments: Dict[Key, Tuple[object, list]] = {}

    def trim_threads(self, limit: int = 2000) -> None:
        for d in (self.thread_roots, self.awaiting_comments):
            for old in list(d)[: max(0, len(d) - limit)]:
                del d[old]

    def remember_channel(self, user_id: int, chat_id: int) -> None:
        recent = [c for c in self.user_channels.get(user_id, []) if c != chat_id]
        self.user_channels[user_id] = ([chat_id] + recent)[:5]

    def is_verified(self, key: Key) -> bool:
        return key in self.verified

    def verify(self, key: Key) -> None:
        self.verified.pop(key, None)
        self.verified[key] = None
        if len(self.verified) > self.VERIFIED_LIMIT:
            for old in list(self.verified)[: self.VERIFIED_LIMIT // 10]:
                del self.verified[old]

    def record_ban(self, chat_id: int, user_id: int, name: str, until: Optional[int]) -> None:
        bans = self.bans.setdefault(chat_id, {})
        bans.pop(user_id, None)
        bans[user_id] = (name or "Без имени", until)
        for old in list(bans)[:-30]:
            del bans[old]

    def active_bans(self, chat_id: int) -> Dict[int, Tuple[str, Optional[int]]]:
        bans = self.bans.get(chat_id, {})
        now = time.time()
        for user_id in [u for u, (_, until) in bans.items() if until is not None and until <= now]:
            del bans[user_id]  # временный бан уже закончился
        return bans

    def forget_ban(self, chat_id: int, user_id: int) -> Optional[str]:
        entry = self.bans.get(chat_id, {}).pop(user_id, None)
        return entry[0] if entry else None

    def stat(self, chat_id: int) -> ChatStats:
        return self.stats.setdefault(chat_id, ChatStats())

    def cached_admins(self, chat_id: int) -> Optional[Set[int]]:
        entry = self._admins.get(chat_id)
        return entry[1] if entry and entry[0] > time.monotonic() else None

    def set_admins(self, chat_id: int, admins: Set[int]) -> None:
        self._admins[chat_id] = (time.monotonic() + self.CACHE_TTL, admins)

    def cached_linked(self, chat_id: int) -> Tuple[bool, Optional[int]]:
        entry = self._linked.get(chat_id)
        if entry and entry[0] > time.monotonic():
            return True, entry[1]
        return False, None

    def set_linked(self, chat_id: int, linked: Optional[int]) -> None:
        self._linked[chat_id] = (time.monotonic() + self.CACHE_TTL, linked)

    def forget_chat(self, chat_id: int) -> None:
        for key in [k for k in self.verified if k[0] == chat_id]:
            del self.verified[key]
        for key in [k for k in self.pending if k[0] == chat_id]:
            p = self.pending.pop(key)
            if p.task:
                p.task.cancel()
        self.stats.pop(chat_id, None)
        self.bans.pop(chat_id, None)
        self._admins.pop(chat_id, None)
        self._linked.pop(chat_id, None)
