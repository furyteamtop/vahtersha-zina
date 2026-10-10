"""Сценарии целиком: апдейты Telegram идут через диспетчер, ответы API подменены."""

import asyncio
import itertools
import tempfile
import time
import unittest
from types import SimpleNamespace

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    BanChatMember,
    DeleteMessage,
    GetChat,
    GetChatAdministrators,
    SendMessage,
)
from aiogram.types import Message, Update

from zina.config import Config
from zina.handlers import router
from zina.storage import Memory, Storage

GROUP = -1001
CHANNEL = -1002
ALIEN_CHANNEL = -1003
ADMIN = 10
USER = 20
THREAD_ROOT = 500


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self._ids = itertools.count(1000)

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetChatAdministrators):
            return [SimpleNamespace(user=SimpleNamespace(id=ADMIN))]
        if isinstance(method, GetChat):
            return SimpleNamespace(linked_chat_id=CHANNEL)
        if isinstance(method, SendMessage):
            return Message.model_validate(
                {"message_id": next(self._ids), "date": int(time.time()),
                 "chat": {"id": method.chat_id, "type": "supergroup"}, "text": method.text}
            )
        return True

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    def of(self, kind):
        return [c for c in self.calls if isinstance(c, kind)]


_update_ids = itertools.count(1)
_message_ids = itertools.count(1)


def comment(text, user_id=USER, sender_chat=None, name="Вася", **extra):
    msg = {
        "message_id": next(_message_ids),
        "date": int(time.time()),
        "chat": {"id": GROUP, "type": "supergroup", "title": "Обсуждение"},
        "from": {"id": user_id, "is_bot": False, "first_name": name},
        "text": text,
        "message_thread_id": THREAD_ROOT,
        "reply_to_message": {
            "message_id": THREAD_ROOT, "date": int(time.time()),
            "chat": {"id": GROUP, "type": "supergroup"}, "text": "пост",
        },
    }
    if sender_chat:
        msg["from"] = {"id": 136817688, "is_bot": True, "first_name": "Channel_Bot"}
        msg["sender_chat"] = {"id": sender_chat, "type": "channel", "title": "Канал"}
    msg.update(extra)
    return Update.model_validate({"update_id": next(_update_ids), "message": msg})


def press(user_id, data, message_id):
    return Update.model_validate({
        "update_id": next(_update_ids),
        "callback_query": {
            "id": str(next(_update_ids)), "chat_instance": "x", "data": data,
            "from": {"id": user_id, "is_bot": False, "first_name": "Кто-то"},
            "message": {"message_id": message_id, "date": int(time.time()),
                        "chat": {"id": GROUP, "type": "supergroup"}, "text": "капча"},
        },
    })


class FlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = FakeSession()
        self.bot = Bot("42:TEST", session=self.session)
        self.mem = Memory()
        self.cfg = Config(token="x", captcha_timeout=60, fail_ban_minutes=15, db_path="")
        self.dp = Dispatcher(cfg=self.cfg, mem=self.mem, store=Storage(f"{self.tmp.name}/z.db"))
        self.dp.include_router(router)

    async def asyncTearDown(self):
        for p in self.mem.pending.values():
            p.task.cancel()
        router._parent_router = None  # чтобы следующий тест мог подключить router заново
        self.tmp.cleanup()

    async def feed(self, update):
        await self.dp.feed_update(self.bot, update)

    def captcha(self):
        return self.mem.pending[(GROUP, USER)]

    async def test_newcomer_gets_captcha_and_passes(self):
        await self.feed(comment("Привет всем"))
        self.assertEqual(len(self.session.of(DeleteMessage)), 1)
        sent = self.session.of(SendMessage)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].reply_parameters.message_id, THREAD_ROOT)
        self.assertIn("пропуск", sent[0].text)

        # пока висит капча — новые сообщения удаляются, новой капчи нет
        await self.feed(comment("ау?"))
        self.assertEqual(len(self.session.of(DeleteMessage)), 2)
        self.assertEqual(len(self.session.of(SendMessage)), 1)

        p = self.captcha()
        # чужой человек жмёт кнопку — не засчитывается
        await self.feed(press(999, f"z:{USER}:{p.correct}", p.message_id))
        self.assertIn((GROUP, USER), self.mem.pending)

        await self.feed(press(USER, f"z:{USER}:{p.correct}", p.message_id))
        self.assertTrue(self.mem.is_verified((GROUP, USER)))
        self.assertEqual(self.session.of(BanChatMember), [])

        deleted_before = len(self.session.of(DeleteMessage))
        await self.feed(comment("Нормальный комментарий"))
        self.assertEqual(len(self.session.of(DeleteMessage)), deleted_before)
        await self.feed(comment("Горячие фото в моём профиле"))
        self.assertEqual(len(self.session.of(DeleteMessage)), deleted_before + 1)

    async def test_wrong_answer_bans_temporarily(self):
        await self.feed(comment("Привет"))
        p = self.captcha()
        wrong = (p.correct + 1) % 3
        await self.feed(press(USER, f"z:{USER}:{wrong}", p.message_id))
        bans = self.session.of(BanChatMember)
        self.assertEqual(len(bans), 1)
        self.assertGreater(bans[0].until_date, time.time() + 14 * 60)
        self.assertFalse(self.mem.is_verified((GROUP, USER)))

    async def test_timeout_bans(self):
        self.dp["cfg"] = Config(token="x", captcha_timeout=0, fail_ban_minutes=15, db_path="")
        await self.feed(comment("Привет"))
        await asyncio.sleep(0.05)
        self.assertNotIn((GROUP, USER), self.mem.pending)
        self.assertEqual(len(self.session.of(BanChatMember)), 1)

    async def test_spam_from_newcomer_is_banned_without_captcha(self):
        await self.feed(comment("пoрнo бесплатно, пиши в лс"))
        self.assertEqual(len(self.session.of(DeleteMessage)), 1)
        self.assertEqual(self.session.of(SendMessage), [])
        bans = self.session.of(BanChatMember)
        self.assertEqual(len(bans), 1)
        self.assertIsNone(bans[0].until_date)

    async def test_spammy_name_is_banned(self):
        await self.feed(comment("привет", name="Катя 🔞 порно"))
        self.assertEqual(len(self.session.of(BanChatMember)), 1)

    async def test_channel_posts_admins_and_channels(self):
        await self.feed(comment("пост канала", is_automatic_forward=True, user_id=777000))
        await self.feed(comment("ответ от имени своего канала", sender_chat=CHANNEL))
        await self.feed(comment("я админ", user_id=ADMIN))
        self.assertEqual(self.session.of(DeleteMessage), [])

        await self.feed(comment("заходите ко мне", sender_chat=ALIEN_CHANNEL))
        self.assertEqual(len(self.session.of(DeleteMessage)), 1)

    async def test_admin_adds_custom_word(self):
        await self.feed(comment("/addword казино, ставки", user_id=ADMIN))
        self.assertEqual(self.dp["store"].get(GROUP).words, ["казино", "ставки"])
        # не-админ команду выполнить не может — это обычное сообщение новичка
        await self.feed(comment("/addword привет"))
        self.assertEqual(self.dp["store"].get(GROUP).words, ["казино", "ставки"])
        self.assertIn((GROUP, USER), self.mem.pending)

    async def test_channel_owner_can_use_commands(self):
        await self.feed(comment("/addword казино", sender_chat=CHANNEL))
        self.assertEqual(self.dp["store"].get(GROUP).words, ["казино"])
        await self.feed(comment("/addword чужое", sender_chat=ALIEN_CHANNEL))
        self.assertEqual(self.dp["store"].get(GROUP).words, ["казино"])

    async def test_answers_callback(self):
        await self.feed(comment("Привет"))
        p = self.captcha()
        await self.feed(press(USER, f"z:{USER}:{p.correct}", p.message_id))
        self.assertTrue(self.session.of(AnswerCallbackQuery))


if __name__ == "__main__":
    unittest.main()
