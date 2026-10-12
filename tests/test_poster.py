"""Постер: автоподпись постов канала и публикация через бота."""

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
    EditMessageCaption,
    EditMessageReplyMarkup,
    EditMessageText,
    GetChat,
    GetMe,
    GetChatMember,
    SendMessage,
    SendPhoto,
)
from aiogram.types import Message, Update

from zina import poster
from zina.config import Config
from zina.handlers import router as main_router
from zina.poster import parse_buttons, router as poster_router
from zina.storage import ChannelSettings, Memory, Storage

CHANNEL = -1005
ADMIN = 10
STRANGER = 30
BOT_ID = 42
SIGNATURE = '🌐 <a href="https://site.ru">Сайт</a> · 💬 <a href="https://t.me/chat">Чат</a>'
BUTTONS = [[{"text": "Сайт", "url": "https://site.ru"}]]

_ids = itertools.count(1)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetChatMember):
            if method.user_id in (ADMIN, BOT_ID):
                return SimpleNamespace(status="administrator", can_post_messages=True, can_edit_messages=True)
            return SimpleNamespace(status="member")
        if isinstance(method, GetMe):
            return SimpleNamespace(id=BOT_ID, username="vahtershazina_bot")
        if isinstance(method, GetChat):
            return SimpleNamespace(id=CHANNEL, title="Мой канал", username="mychan", type="channel")
        if isinstance(method, (SendMessage, SendPhoto)):
            chat_type = "channel" if method.chat_id == CHANNEL else "private"
            return Message.model_validate({
                "message_id": next(_ids), "date": int(time.time()),
                "chat": {"id": method.chat_id, "type": chat_type, "username": "mychan"}, "text": "x",
            })
        return True

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    def of(self, kind):
        return [c for c in self.calls if isinstance(c, kind)]


def channel_post(**fields):
    msg = {"message_id": next(_ids), "date": int(time.time()),
           "chat": {"id": CHANNEL, "type": "channel", "title": "Мой канал"}}
    msg.update(fields)
    return Update.model_validate({"update_id": next(_ids), "channel_post": msg})


def private(user_id=ADMIN, **fields):
    msg = {"message_id": next(_ids), "date": int(time.time()),
           "chat": {"id": user_id, "type": "private"},
           "from": {"id": user_id, "is_bot": False, "first_name": "Админ"}}
    msg.update(fields)
    return Update.model_validate({"update_id": next(_ids), "message": msg})


def press(data, user_id=ADMIN):
    return Update.model_validate({"update_id": next(_ids), "callback_query": {
        "id": str(next(_ids)), "chat_instance": "x", "data": data,
        "from": {"id": user_id, "is_bot": False, "first_name": "Админ"},
        "message": {"message_id": next(_ids), "date": int(time.time()),
                    "chat": {"id": user_id, "type": "private"}, "text": "меню"},
    }})


PHOTO = [{"file_id": "PHOTO", "file_unique_id": "p", "width": 10, "height": 10}]


class ButtonsParsingTest(unittest.TestCase):
    def test_parse(self):
        rows, _ = parse_buttons("Сайт - https://site.ru | Чат — t.me/chat\nYouTube - youtube.com/@me")
        self.assertEqual(rows, [
            [{"text": "Сайт", "url": "https://site.ru"}, {"text": "Чат", "url": "https://t.me/chat"}],
            [{"text": "YouTube", "url": "https://youtube.com/@me"}],
        ])

    def test_telegram_username(self):
        rows, _ = parse_buttons(
            "Сайт - https://shapovalov.digital | Сервис ПФ - https://gopf.ru\n"
            "FuryCRM - https://furycrm.ru | Khinkali Games - @khinkaligames"
        )
        self.assertEqual(rows[1][1], {"text": "Khinkali Games", "url": "https://t.me/khinkaligames"})
        self.assertEqual(len(rows), 2)

    def test_bad(self):
        rows, bad = parse_buttons("Сайт https://site.ru")
        self.assertIsNone(rows)
        self.assertEqual(bad, "Сайт https://site.ru")


class PosterTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = FakeSession()
        self.bot = Bot(f"{BOT_ID}:TEST", session=self.session)
        self.mem = Memory()
        self.store = Storage(f"{self.tmp.name}/z.db")
        cfg = Config(token="x", captcha_timeout=60, fail_ban_minutes=15, db_path="")
        self.dp = Dispatcher(cfg=cfg, mem=self.mem, store=self.store)
        self.dp.include_router(poster_router)
        self.dp.include_router(main_router)

    async def asyncTearDown(self):
        poster_router._parent_router = None
        main_router._parent_router = None
        self.tmp.cleanup()

    async def feed(self, update):
        await self.dp.feed_update(self.bot, update)

    def configure(self, signature=SIGNATURE, buttons=BUTTONS, auto=True):
        self.store.save_channel(CHANNEL, ChannelSettings(signature, buttons, auto))

    # --- автоподпись ---

    async def test_text_post_gets_signature_and_buttons(self):
        self.configure()
        text = "Новый кейс: example.com"
        await self.feed(channel_post(text=text, entities=[{"type": "url", "offset": 12, "length": 11}]))
        edit = self.session.of(EditMessageText)[0]
        self.assertTrue(edit.text.startswith("Новый кейс: example.com\n\n🌐 <a href"))
        self.assertEqual(edit.link_preview_options.url, "https://example.com")  # превью автора, а не ссылки из подписи
        self.assertEqual(edit.reply_markup.inline_keyboard[0][0].url, "https://site.ru")

    async def test_text_without_links_has_no_preview(self):
        self.configure()
        await self.feed(channel_post(text="Просто текст"))
        self.assertTrue(self.session.of(EditMessageText)[0].link_preview_options.is_disabled)

    async def test_photo_without_caption_gets_caption(self):
        self.configure()
        await self.feed(channel_post(photo=PHOTO))
        edit = self.session.of(EditMessageCaption)[0]
        self.assertEqual(edit.caption, SIGNATURE)

    async def test_buttons_only_does_not_touch_text(self):
        self.configure(signature="")
        await self.feed(channel_post(text="Пост"))
        self.assertEqual(self.session.of(EditMessageText), [])
        self.assertEqual(len(self.session.of(EditMessageReplyMarkup)), 1)

    async def test_already_signed_post_only_gets_buttons(self):
        self.configure()
        await self.feed(channel_post(text="Пост\n\n🌐 Сайт · 💬 Чат"))
        self.assertEqual(self.session.of(EditMessageText), [])
        self.assertEqual(len(self.session.of(EditMessageReplyMarkup)), 1)

    async def test_auto_off_and_forwards_are_skipped(self):
        self.configure(auto=False)
        await self.feed(channel_post(text="Пост"))
        self.configure()
        await self.feed(channel_post(text="Чужое", forward_origin={
            "type": "channel", "chat": {"id": -1009, "type": "channel", "title": "x"},
            "message_id": 1, "date": int(time.time())}))
        self.assertEqual(self.session.calls, [])

    async def test_album_signed_once(self):
        poster.ALBUM_WAIT = 0.01
        self.configure()
        first = channel_post(photo=PHOTO, media_group_id="A")
        await self.feed(first)
        await self.feed(channel_post(photo=PHOTO, media_group_id="A"))
        await asyncio.sleep(0.05)
        edits = self.session.of(EditMessageCaption)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].message_id, first.channel_post.message_id)
        self.assertIsNone(edits[0].reply_markup)  # к альбому кнопки не прикрепить

    async def test_album_caption_gets_signature(self):
        poster.ALBUM_WAIT = 0.01
        self.configure()
        await self.feed(channel_post(photo=PHOTO, media_group_id="B"))
        captioned = channel_post(photo=PHOTO, media_group_id="B", caption="Фотоотчёт")
        await self.feed(captioned)
        await asyncio.sleep(0.05)
        edits = self.session.of(EditMessageCaption)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].message_id, captioned.channel_post.message_id)
        self.assertTrue(edits[0].caption.startswith("Фотоотчёт\n\n"))

    # --- настройка и публикация через личку ---

    async def test_full_private_flow(self):
        await self.feed(private(text="/poster"))
        await self.feed(private(text="пересылаю", forward_origin={
            "type": "channel", "chat": {"id": CHANNEL, "type": "channel", "title": "Мой канал"},
            "message_id": 7, "date": int(time.time())}))
        menu = self.session.of(SendMessage)[-1]
        self.assertIn("Мой канал", menu.text)

        await self.feed(press(f"p:sig:{CHANNEL}"))
        await self.feed(private(text="🌐 Сайт", entities=[
            {"type": "text_link", "offset": 3, "length": 4, "url": "https://site.ru"}]))
        self.assertEqual(self.store.channel(CHANNEL).signature, '🌐 <a href="https://site.ru">Сайт</a>')

        await self.feed(press(f"p:btn:{CHANNEL}"))
        await self.feed(private(text="Чат - t.me/chat"))
        self.assertEqual(self.store.channel(CHANNEL).buttons, [[{"text": "Чат", "url": "https://t.me/chat"}]])

        await self.feed(press(f"p:new:{CHANNEL}"))
        await self.feed(private(text="Свежий пост"))
        preview = [m for m in self.session.of(SendMessage) if m.chat_id == ADMIN and "Свежий пост" in m.text][0]
        self.assertIn('<a href="https://site.ru">Сайт</a>', preview.text)
        self.assertIsNotNone(preview.reply_markup)
        self.assertFalse([m for m in self.session.of(SendMessage) if m.chat_id == CHANNEL])

        await self.feed(press(f"p:pub:{CHANNEL}"))
        published = [m for m in self.session.of(SendMessage) if m.chat_id == CHANNEL]
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].text, 'Свежий пост\n\n🌐 <a href="https://site.ru">Сайт</a>')
        self.assertEqual(published[0].reply_markup.inline_keyboard[0][0].url, "https://t.me/chat")
        done = self.session.of(EditMessageText)[-1]
        self.assertIn("https://t.me/mychan/", done.text)

    async def test_photo_post_via_bot(self):
        self.configure()
        await self.feed(press(f"p:new:{CHANNEL}"))
        await self.feed(private(photo=PHOTO, caption="Кот"))
        await self.feed(press(f"p:pub:{CHANNEL}"))
        sent = [m for m in self.session.of(SendPhoto) if m.chat_id == CHANNEL][0]
        self.assertEqual(sent.caption, "Кот\n\n" + SIGNATURE)

    async def test_stranger_cannot_configure(self):
        await self.feed(press(f"p:sig:{CHANNEL}", user_id=STRANGER))
        answer = self.session.of(AnswerCallbackQuery)[-1]
        self.assertTrue(answer.show_alert)
        self.assertIn("только админы", answer.text)

    async def test_start_resets_poster_step(self):
        await self.feed(press(f"p:sig:{CHANNEL}"))
        await self.feed(private(text="/start"))
        await self.feed(private(text="это уже не подпись"))
        self.assertEqual(self.store.channel(CHANNEL).signature, "")


if __name__ == "__main__":
    unittest.main()
