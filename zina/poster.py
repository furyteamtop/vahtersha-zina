"""Постер: подпись со ссылками и кнопки к постам канала.

Два режима:
- автоподпись — админ публикует пост прямо в канале, Зина дописывает подпись и кнопки;
- постинг через бота — админ присылает пост Зине в личку, она публикует его уже с подписью.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import List, Optional, Tuple

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType, MessageEntityType, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.methods import SendMessage
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    MessageOriginChannel,
)

from . import texts
from .storage import Album, Buttons, ChannelSettings, Memory, Storage

log = logging.getLogger("zina.poster")
router = Router()

PRIVATE = F.chat.type == ChatType.PRIVATE
CHANNEL = F.chat.type == ChatType.CHANNEL
NOT_COMMAND = ~F.text.startswith("/")
TEXT_LIMIT, CAPTION_LIMIT = 4096, 1024
SIGNATURE_LIMIT = 500
ALBUM_WAIT = 2.0  # сек: столько ждём остальные фото альбома


class Poster(StatesGroup):
    channel = State()  # ждём пересланный пост или @username канала
    signature = State()
    buttons = State()
    post = State()


# ---------- подпись и кнопки ----------

_TAG = re.compile(r"<[^>]+>")
_BUTTON = re.compile(r"^\s*(.+?)\s+[-—–]\s+(\S+)\s*$")
_URL = re.compile(r"^(https?://|tg://)\S+$", re.I)
_USERNAME = re.compile(r"^@([A-Za-z0-9_]{4,32})$")
_BARE_URL = re.compile(r"^(t\.me/|[\w-]+(\.[\w-]+)*\.[a-z]{2,}(/|$))\S*$", re.I)


def plain(html_text: str) -> str:
    return html.unescape(_TAG.sub("", html_text))


def parse_buttons(text: str) -> Tuple[Optional[Buttons], str]:
    """«Сайт - https://site.ru | Чат - t.me/chat» -> ряды кнопок. Возвращает (кнопки, ошибка)."""
    rows: Buttons = []
    for line in text.splitlines():
        if not line.strip():
            continue
        row = []
        for part in line.split("|"):
            m = _BUTTON.match(part)
            if not m:
                return None, part.strip()
            label, url = m.group(1).strip(), m.group(2)
            username = _USERNAME.match(url)
            if username:  # @channel -> ссылка на канал или чат
                url = f"https://t.me/{username.group(1)}"
            elif not _URL.match(url):
                if not _BARE_URL.match(url):
                    return None, part.strip()
                url = "https://" + url
            row.append({"text": label[:64], "url": url})
        rows.append(row[:8])
    return (rows[:10] or None), ""


COMMENTS_BUTTON = "💬 Комментарии"


def keyboard(buttons: Buttons, comments_url: Optional[str] = None) -> Optional[InlineKeyboardMarkup]:
    """Кнопки поста. comments_url — добавить последним рядом кнопку «Комментарии»."""
    if not buttons:
        return None  # без кнопок Telegram сам показывает панель комментариев
    rows = [[InlineKeyboardButton(text=b["text"], url=b["url"]) for b in row] for row in buttons]
    if comments_url:
        rows.append([InlineKeyboardButton(text=COMMENTS_BUTTON, url=comments_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def has_body_slot(message: Message) -> bool:
    """Можно ли к сообщению дописать текст: это текст или медиа с подписью."""
    return bool(
        message.text
        or message.photo
        or message.video
        or message.animation
        or message.document
        or message.audio
        or message.voice
    )


def body_html(message: Message) -> str:
    return message.html_text if (message.text or message.caption) else ""


def signed_body(message: Message, signature: str) -> Tuple[str, bool]:
    """Текст/подпись поста с добавленной подписью канала. Второе значение — поместилась ли подпись."""
    body = body_html(message)
    if not signature or not has_body_slot(message):
        return body, False
    current = message.text or message.caption or ""
    sig_plain = plain(signature)
    if sig_plain.strip() and sig_plain.strip() in current:
        return body, True  # уже подписан
    limit = TEXT_LIMIT if message.text else CAPTION_LIMIT
    if len(current) + 2 + len(sig_plain) > limit:
        return body, False
    return (f"{body}\n\n{signature}" if body else signature), True


def preview_options(message: Message) -> LinkPreviewOptions:
    """Сохраняем превью ссылки как было у автора, чтобы ссылки из подписи его не перебивали."""
    if message.link_preview_options:
        return message.link_preview_options
    for entity in message.entities or []:
        if entity.type == MessageEntityType.TEXT_LINK and entity.url:
            return LinkPreviewOptions(url=entity.url)
        if entity.type == MessageEntityType.URL:
            url = entity.extract_from(message.text)
            return LinkPreviewOptions(url=url if "://" in url else "https://" + url)
    return LinkPreviewOptions(is_disabled=True)


def compose(message: Message, settings: ChannelSettings, chat_id: int, with_buttons: bool = True,
            comments_url: Optional[str] = None):
    """Метод отправки копии поста с подписью и кнопками. Возвращает (метод, поместилась ли подпись)."""
    copy = message.send_copy(chat_id, parse_mode=ParseMode.HTML)
    body, fitted = signed_body(message, settings.signature)
    fields = type(copy).model_fields
    update = {}
    if isinstance(copy, SendMessage):
        update.update(text=body, entities=None, link_preview_options=preview_options(message))
    elif "caption" in fields:
        update.update(caption=body or None, caption_entities=None)
    markup = keyboard(settings.buttons, comments_url) if with_buttons else None
    if markup and "reply_markup" in fields:
        update["reply_markup"] = markup
    return copy.model_copy(update=update), fitted


# ---------- права ----------

async def check_access(bot: Bot, channel_id: int, user_id: int) -> Tuple[Optional[str], List[str]]:
    """(ошибка, предупреждения). Пускаем только админов канала, где Зина тоже админ."""
    try:
        user = await bot.get_chat_member(channel_id, user_id)
        me = await bot.get_chat_member(channel_id, bot.id)
    except TelegramAPIError:
        return texts.POSTER_NO_CHANNEL, []
    if user.status not in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR):
        return texts.POSTER_NOT_ADMIN, []
    if me.status != ChatMemberStatus.ADMINISTRATOR:
        return texts.POSTER_BOT_NOT_ADMIN, []
    warnings = []
    if not getattr(me, "can_post_messages", False):
        warnings.append(texts.POSTER_NEED_POST)
    if not getattr(me, "can_edit_messages", False):
        warnings.append(texts.POSTER_NEED_EDIT)
    return None, warnings


async def get_channel(bot: Bot, channel_id: int):
    try:
        return await bot.get_chat(channel_id)
    except TelegramAPIError:
        return None


async def channel_title(bot: Bot, channel_id: int) -> str:
    chat = await get_channel(bot, channel_id)
    return (chat.title if chat else None) or "канал"


def post_link(chat, message_id: Optional[int] = None) -> str:
    base = (f"https://t.me/{chat.username}" if getattr(chat, "username", None)
            else f"https://t.me/c/{str(chat.id).replace('-100', '', 1)}")
    return f"{base}/{message_id}" if message_id else base


async def comments_url(bot: Bot, settings: ChannelSettings, channel_id: int,
                       message_id: Optional[int]) -> Optional[str]:
    """Ссылка, открывающая комментарии к посту. None — если кнопка выключена или обсуждения нет.
    Без message_id — ссылка на канал (для предпросмотра, пока поста ещё нет)."""
    if not settings.comments or not settings.buttons:
        return None
    chat = await get_channel(bot, channel_id)
    if chat is None or not getattr(chat, "linked_chat_id", None):
        return None
    return f"{post_link(chat, message_id)}?comment=1" if message_id else post_link(chat)


# ---------- автоподпись постов в канале ----------

async def sign_post(bot: Bot, message: Message, settings: ChannelSettings, with_buttons: bool = True) -> None:
    body, fitted = signed_body(message, settings.signature)
    markup = message.reply_markup
    if markup is None and with_buttons:
        url = await comments_url(bot, settings, message.chat.id, message.message_id)
        markup = keyboard(settings.buttons, url)
    text_changed = fitted and body != body_html(message)
    chat_id, message_id = message.chat.id, message.message_id
    try:
        if text_changed and message.text:
            await bot.edit_message_text(
                text=body, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.HTML,
                link_preview_options=preview_options(message), reply_markup=markup,
            )
        elif text_changed:
            await bot.edit_message_caption(
                caption=body, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.HTML,
                reply_markup=markup, show_caption_above_media=message.show_caption_above_media,
            )
        elif markup and not message.reply_markup:
            # только кнопки — Telegram не ставит пометку «изменено»
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=markup)
    except TelegramAPIError as e:
        log.warning("Не удалось подписать пост: %s", e.message)


async def finish_album(bot: Bot, mem: Memory, group_id: str, settings: ChannelSettings) -> None:
    await asyncio.sleep(ALBUM_WAIT)
    album = mem.albums.pop(group_id, None)
    if album and not album.done and album.items:
        # ни у одного фото нет подписи — добавляем её к первому
        await sign_post(bot, min(album.items, key=lambda m: m.message_id), settings, with_buttons=False)


@router.channel_post()
async def on_channel_post(message: Message, bot: Bot, mem: Memory, store: Storage) -> None:
    settings = store.channel(message.chat.id)
    if not settings.auto or not (settings.signature or settings.buttons):
        return
    if message.forward_origin or message.via_bot:
        return  # чужие пересланные посты не трогаем
    if not message.media_group_id:
        await sign_post(bot, message, settings)
        return
    # Альбом приходит отдельными сообщениями. Кнопки к альбому прикрепить нельзя — только подпись.
    album = mem.albums.get(message.media_group_id)
    if album is None:
        album = mem.albums[message.media_group_id] = Album()
        album.task = asyncio.create_task(finish_album(bot, mem, message.media_group_id, settings))
    if album.done:
        return
    if message.caption:
        album.done = True
        await sign_post(bot, message, settings, with_buttons=False)
    else:
        album.items.append(message)


# ---------- бота добавили в канал ----------

@router.my_chat_member(CHANNEL)
async def on_channel_status(event: ChatMemberUpdated, bot: Bot, mem: Memory, store: Storage) -> None:
    status = event.new_chat_member.status
    if status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        store.delete_channel(event.chat.id)
        return
    if status != ChatMemberStatus.ADMINISTRATOR or event.old_chat_member.status == ChatMemberStatus.ADMINISTRATOR:
        return
    user = event.from_user
    if user is None or user.is_bot:
        return
    mem.remember_channel(user.id, event.chat.id)
    try:
        await bot.send_message(
            user.id,
            texts.POSTER_ADDED.format(title=html.escape(event.chat.title or "канал")),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⚙️ Настроить", callback_data=f"p:menu:{event.chat.id}")
            ]]),
        )
    except TelegramAPIError:
        pass  # человек ещё не писал боту — написать ему первым нельзя


# ---------- меню в личке ----------

def menu_markup(channel_id: int, s: ChannelSettings) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✏️ Подпись", callback_data=f"p:sig:{channel_id}"),
            InlineKeyboardButton(text="🔘 Кнопки", callback_data=f"p:btn:{channel_id}"),
        ],
        [InlineKeyboardButton(
            text=f"🤖 Автоподпись: {'вкл' if s.auto else 'выкл'}", callback_data=f"p:auto:{channel_id}"
        )],
        [InlineKeyboardButton(
            text=f"💬 Кнопка «Комментарии»: {'вкл' if s.comments else 'выкл'}", callback_data=f"p:comm:{channel_id}"
        )],
        [
            InlineKeyboardButton(text="👁 Предпросмотр", callback_data=f"p:prev:{channel_id}"),
            InlineKeyboardButton(text="📝 Новый пост", callback_data=f"p:new:{channel_id}"),
        ],
    ]
    clean = []
    if s.signature:
        clean.append(InlineKeyboardButton(text="🧹 Убрать подпись", callback_data=f"p:delsig:{channel_id}"))
    if s.buttons:
        clean.append(InlineKeyboardButton(text="🧹 Убрать кнопки", callback_data=f"p:delbtn:{channel_id}"))
    if clean:
        rows.append(clean)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def menu_text(title: str, s: ChannelSettings, warnings: List[str]) -> str:
    buttons = "\n".join(
        " | ".join(f"{html.escape(b['text'])} → {html.escape(b['url'])}" for b in row) for row in s.buttons
    )
    return texts.POSTER_MENU.format(
        title=html.escape(title),
        signature=s.signature or "<i>не задана</i>",
        buttons=buttons or "<i>нет</i>",
        auto=texts.POSTER_AUTO_ON if s.auto else texts.POSTER_AUTO_OFF,
        comments=texts.POSTER_COMMENTS_ON if s.comments else texts.POSTER_COMMENTS_OFF,
        warnings=("\n\n" + "\n".join(f"⚠️ {w}" for w in warnings)) if warnings else "",
    )


async def send_sample(bot: Bot, chat_id: int, channel_id: int, s: ChannelSettings) -> None:
    """Пример поста с текущей подписью и кнопками — как он будет выглядеть в канале."""
    if not (s.signature or s.buttons):
        await bot.send_message(chat_id, texts.POSTER_SAMPLE_EMPTY)
        return
    text = texts.POSTER_SAMPLE + (f"\n\n{s.signature}" if s.signature else "")
    await bot.send_message(
        chat_id, text, reply_markup=keyboard(s.buttons, await comments_url(bot, s, channel_id, None)),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def show_menu(bot: Bot, store: Storage, chat_id: int, channel_id: int, warnings: List[str],
                    edit: Optional[Message] = None) -> None:
    s = store.channel(channel_id)
    text = menu_text(await channel_title(bot, channel_id), s, warnings)
    markup = menu_markup(channel_id, s)
    if edit is not None:
        try:
            await edit.edit_text(text, reply_markup=markup)
            return
        except TelegramAPIError:
            pass
    await bot.send_message(chat_id, text, reply_markup=markup)


@router.message(PRIVATE, Command("poster"))
async def cmd_poster(message: Message, bot: Bot, mem: Memory, state: FSMContext) -> None:
    await state.set_state(Poster.channel)
    recent = mem.user_channels.get(message.from_user.id, [])
    rows = [
        [InlineKeyboardButton(text=f"📢 {await channel_title(bot, c)}", callback_data=f"p:menu:{c}")]
        for c in recent
    ]
    await message.answer(
        texts.POSTER_PICK, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    )


async def resolve_channel(bot: Bot, message: Message) -> Optional[int]:
    origin = message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        return origin.chat.id
    m = re.search(r"(?:@|t\.me/)([A-Za-z0-9_]{4,})", message.text or "")
    if m:
        try:
            chat = await bot.get_chat(f"@{m.group(1)}")
            if chat.type == ChatType.CHANNEL:
                return chat.id
        except TelegramAPIError:
            pass
    return None


@router.message(PRIVATE, StateFilter(Poster.channel), NOT_COMMAND)
async def got_channel(message: Message, bot: Bot, mem: Memory, store: Storage, state: FSMContext) -> None:
    channel_id = await resolve_channel(bot, message)
    if channel_id is None:
        await message.answer(texts.POSTER_PICK_FAIL)
        return
    error, warnings = await check_access(bot, channel_id, message.from_user.id)
    if error:
        await message.answer(error)
        return
    mem.remember_channel(message.from_user.id, channel_id)
    await state.clear()
    await show_menu(bot, store, message.chat.id, channel_id, warnings)


@router.callback_query(F.data.startswith("p:"))
async def on_menu(cb: CallbackQuery, bot: Bot, mem: Memory, store: Storage, state: FSMContext) -> None:
    try:
        _, action, raw_id = cb.data.split(":")
        channel_id = int(raw_id)
    except ValueError:
        await cb.answer()
        return
    error, warnings = await check_access(bot, channel_id, cb.from_user.id)
    if error:
        await cb.answer(plain(error), show_alert=True)
        return
    mem.remember_channel(cb.from_user.id, channel_id)
    s = store.channel(channel_id)
    chat_id = cb.message.chat.id

    if action == "menu":
        await state.clear()
        await cb.answer()
        await show_menu(bot, store, chat_id, channel_id, warnings)
    elif action == "prev":
        await cb.answer()
        await send_sample(bot, chat_id, channel_id, s)
    elif action == "comm":
        s.comments = not s.comments
        store.save_channel(channel_id, s)
        await cb.answer("Кнопка «Комментарии» включена" if s.comments else "Кнопка «Комментарии» выключена")
        await show_menu(bot, store, chat_id, channel_id, warnings, edit=cb.message)
    elif action == "auto":
        s.auto = not s.auto
        store.save_channel(channel_id, s)
        await cb.answer("Автоподпись включена" if s.auto else "Автоподпись выключена")
        await show_menu(bot, store, chat_id, channel_id, warnings, edit=cb.message)
    elif action in ("delsig", "delbtn"):
        if action == "delsig":
            s.signature = ""
        else:
            s.buttons = []
        store.save_channel(channel_id, s)
        await cb.answer("Убрала")
        await show_menu(bot, store, chat_id, channel_id, warnings, edit=cb.message)
    elif action in ("sig", "btn", "new"):
        target = {"sig": Poster.signature, "btn": Poster.buttons, "new": Poster.post}[action]
        prompt = {"sig": texts.POSTER_ASK_SIGNATURE, "btn": texts.POSTER_ASK_BUTTONS, "new": texts.POSTER_ASK_POST}
        await state.set_state(target)
        await state.update_data(channel_id=channel_id)
        await cb.answer()
        await bot.send_message(chat_id, prompt[action])
    elif action == "pub":
        await publish(cb, bot, mem, store, state, channel_id)
    elif action == "cancel":
        mem.drafts.pop(cb.from_user.id, None)
        await state.clear()
        await cb.answer("Отменила")
        await cb.message.edit_text("Пост отменён.")
    else:
        await cb.answer()


async def state_channel(state: FSMContext) -> Optional[int]:
    return (await state.get_data()).get("channel_id")


@router.message(PRIVATE, StateFilter(Poster.signature), NOT_COMMAND)
async def got_signature(message: Message, bot: Bot, store: Storage, state: FSMContext) -> None:
    channel_id = await state_channel(state)
    if channel_id is None or not message.text:
        await message.answer(texts.POSTER_SIGNATURE_TEXT_ONLY)
        return
    if len(message.text) > SIGNATURE_LIMIT:
        await message.answer(f"Длинновато: {len(message.text)} символов, можно до {SIGNATURE_LIMIT}.")
        return
    error, warnings = await check_access(bot, channel_id, message.from_user.id)
    if error:
        await message.answer(error)
        return
    s = store.channel(channel_id)
    s.signature = message.html_text
    store.save_channel(channel_id, s)
    await state.clear()
    await message.answer("Подпись сохранила ✅ Вот как будет выглядеть пост:")
    await send_sample(bot, message.chat.id, channel_id, s)
    await show_menu(bot, store, message.chat.id, channel_id, warnings)


@router.message(PRIVATE, StateFilter(Poster.buttons), NOT_COMMAND)
async def got_buttons(message: Message, bot: Bot, store: Storage, state: FSMContext) -> None:
    channel_id = await state_channel(state)
    buttons, bad = parse_buttons(message.text or "")
    if channel_id is None or buttons is None:
        await message.answer(texts.POSTER_BUTTONS_BAD.format(bad=html.escape(bad or "пусто")))
        return
    error, warnings = await check_access(bot, channel_id, message.from_user.id)
    if error:
        await message.answer(error)
        return
    s = store.channel(channel_id)
    s.buttons = buttons
    store.save_channel(channel_id, s)
    await state.clear()
    await message.answer("Кнопки сохранила ✅ Вот как будет выглядеть пост:")
    await send_sample(bot, message.chat.id, channel_id, s)
    await show_menu(bot, store, message.chat.id, channel_id, warnings)


@router.message(PRIVATE, StateFilter(Poster.post), NOT_COMMAND)
async def got_post(message: Message, bot: Bot, mem: Memory, store: Storage, state: FSMContext) -> None:
    channel_id = await state_channel(state)
    if channel_id is None:
        await state.clear()
        return
    if message.media_group_id:
        # альбом приходит пачкой сообщений — предупреждаем один раз
        if (await state.get_data()).get("album_warned") != message.media_group_id:
            await state.update_data(album_warned=message.media_group_id)
            await message.answer(texts.POSTER_NO_ALBUMS)
        return
    s = store.channel(channel_id)
    try:
        preview, fitted = compose(message, s, message.chat.id,
                                  comments_url=await comments_url(bot, s, channel_id, None))
    except TypeError:
        await message.answer(texts.POSTER_UNSUPPORTED)
        return
    mem.drafts[message.from_user.id] = (channel_id, message)
    await bot(preview)
    note = "" if fitted or not s.signature else "\n\n⚠️ " + texts.POSTER_SIGNATURE_DIDNT_FIT
    await message.answer(
        texts.POSTER_CONFIRM.format(title=html.escape(await channel_title(bot, channel_id))) + note,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"p:pub:{channel_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"p:cancel:{channel_id}"),
        ]]),
    )


async def publish(cb: CallbackQuery, bot: Bot, mem: Memory, store: Storage, state: FSMContext,
                  channel_id: int) -> None:
    draft = mem.drafts.get(cb.from_user.id)
    if draft is None or draft[0] != channel_id:
        await cb.answer("Черновик не найден — пришлите пост ещё раз.", show_alert=True)
        return
    settings = store.channel(channel_id)
    method, _ = compose(draft[1], settings, channel_id)
    try:
        sent = await bot(method)
    except TelegramAPIError as e:
        await cb.answer(f"Не получилось: {e.message}", show_alert=True)
        return
    # Ссылка на комментарии зависит от номера поста, он известен только после публикации.
    # Правка одних кнопок не даёт пометку «изменено».
    url = await comments_url(bot, settings, channel_id, sent.message_id)
    if url and "reply_markup" in type(method).model_fields:
        try:
            await bot.edit_message_reply_markup(
                chat_id=channel_id, message_id=sent.message_id, reply_markup=keyboard(settings.buttons, url)
            )
        except TelegramAPIError as e:
            log.warning("Не удалось добавить кнопку комментариев: %s", e.message)
    mem.drafts.pop(cb.from_user.id, None)
    await state.clear()
    await cb.answer("Опубликовала ✅")
    link = post_link(sent.chat, sent.message_id)
    await cb.message.edit_text(
        texts.POSTER_PUBLISHED.format(link=link),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📝 Ещё пост", callback_data=f"p:new:{channel_id}"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data=f"p:menu:{channel_id}"),
        ]]),
    )
