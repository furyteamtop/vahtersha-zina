from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Set

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.methods import SendMessage
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
    User,
)

from . import captcha, poster, spam, texts
from .config import Config
from .storage import Key, Memory, Pending, Storage

log = logging.getLogger("zina")
router = Router()

GROUPS = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})
TELEGRAM_SERVICE_ID = 777000  # «Telegram»: автопересылка постов канала в обсуждение
MAX_HELD_MESSAGES = 10  # сколько комментариев новичка держим до проверки (альбом — до 10 штук)
TEXT_LIMIT, CAPTION_LIMIT = 4096, 1024


# ---------- вспомогательное ----------

async def safe_delete(bot: Bot, chat_id: int, message_id: Optional[int]) -> bool:
    if message_id is None:
        return False
    try:
        return await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        return False


async def get_admins(bot: Bot, mem: Memory, chat_id: int) -> Set[int]:
    admins = mem.cached_admins(chat_id)
    if admins is None:
        try:
            members = await bot.get_chat_administrators(chat_id)
            admins = {m.user.id for m in members}
        except TelegramAPIError:
            admins = set()
        mem.set_admins(chat_id, admins)
    return admins


async def get_linked_channel(bot: Bot, mem: Memory, chat_id: int) -> Optional[int]:
    known, linked = mem.cached_linked(chat_id)
    if not known:
        try:
            linked = (await bot.get_chat(chat_id)).linked_chat_id
        except TelegramAPIError:
            linked = None
        mem.set_linked(chat_id, linked)
    return linked


async def is_admin_message(message: Message, bot: Bot, mem: Memory) -> bool:
    if message.sender_chat:
        # анонимный администратор или владелец канала, пишущий от имени своего канала
        return message.sender_chat.id in (
            message.chat.id,
            await get_linked_channel(bot, mem, message.chat.id),
        )
    if message.from_user is None:
        return False
    return message.from_user.id in await get_admins(bot, mem, message.chat.id)


class IsAdmin(Filter):
    async def __call__(self, message: Message, bot: Bot, mem: Memory) -> bool:
        return await is_admin_message(message, bot, mem)


async def punish(bot: Bot, cfg: Config, chat_id: int, user_id: int) -> None:
    if cfg.fail_ban_minutes <= 0:
        return
    try:
        await bot.ban_chat_member(
            chat_id,
            user_id,
            until_date=int(time.time()) + cfg.fail_ban_minutes * 60,
            revoke_messages=True,
        )
    except TelegramAPIError:
        pass


async def ban_forever(bot: Bot, chat_id: int, user_id: int) -> None:
    try:
        await bot.ban_chat_member(chat_id, user_id, revoke_messages=True)
    except TelegramAPIError:
        pass


def text_of(message: Message) -> str:
    parts = [message.text or "", message.caption or ""]
    if message.poll:
        parts.append(message.poll.question)
        parts.extend(o.text for o in message.poll.options)
    return "\n".join(p for p in parts if p)


def user_name_is_spam(user: User, words) -> bool:
    return spam.is_spam(user.full_name, words)


# ---------- публикация комментария после проверки ----------

def reply_target(message: Message) -> dict:
    """Куда отвечать, чтобы сообщение попало в ту же ветку комментариев."""
    if message.chat.is_forum and message.message_thread_id:
        return {"message_thread_id": message.message_thread_id}
    target = (
        message.reply_to_message.message_id if message.reply_to_message else message.message_thread_id
    )
    if not target:
        return {}
    return {"reply_parameters": ReplyParameters(message_id=target, allow_sending_without_reply=True)}


def build_copies(message: Message, chat_id: int, header: str) -> list:
    """Копия комментария от имени бота с подписью автора сверху."""
    where = reply_target(message)
    copy = message.send_copy(chat_id, parse_mode=ParseMode.HTML, **where)
    body_len = len(message.text or message.caption or "")
    if isinstance(copy, SendMessage) and body_len + len(header) < TEXT_LIMIT:
        return [copy.model_copy(update={"text": f"{header}\n{message.html_text}", "entities": None})]
    if "caption" in type(copy).model_fields and body_len + len(header) < CAPTION_LIMIT:
        caption = f"{header}\n{message.html_text}" if message.caption else header
        return [copy.model_copy(update={"caption": caption, "caption_entities": None})]
    # стикер, кружок и т.п. без подписи — сначала имя автора, потом само сообщение
    return [SendMessage(chat_id=chat_id, text=header, parse_mode=ParseMode.HTML, **where), copy]


async def republish(bot: Bot, chat_id: int, user: User, messages: list) -> bool:
    header = texts.REPOST_HEADER.format(mention=user.mention_html())
    ok = True
    for message in messages:
        try:
            for method in build_copies(message, chat_id, header):
                await bot(method)
        except (TelegramAPIError, TypeError) as e:  # TypeError — тип сообщения нельзя скопировать
            log.warning("Не удалось опубликовать комментарий: %s", type(e).__name__)
            ok = False
    return ok


# ---------- капча ----------

async def expire_captcha(bot: Bot, cfg: Config, mem: Memory, key: Key) -> None:
    await asyncio.sleep(cfg.captcha_timeout)
    pending = mem.pending.pop(key, None)
    if pending is None:
        return
    chat_id, user_id = key
    mem.stat(chat_id).failed += 1
    await safe_delete(bot, chat_id, pending.message_id)
    await punish(bot, cfg, chat_id, user_id)


async def send_captcha(message: Message, bot: Bot, cfg: Config, mem: Memory, key: Key) -> None:
    user = message.from_user
    challenge = captcha.generate()
    pending = Pending(correct=challenge.correct, messages=[message])
    mem.pending[key] = pending

    buttons = [
        InlineKeyboardButton(text=option, callback_data=f"z:{user.id}:{i}")
        for i, option in enumerate(challenge.options)
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[buttons[i:i + 3] for i in range(0, len(buttons), 3)])
    text = texts.CAPTCHA.format(
        greeting=captcha.greeting(),
        mention=user.mention_html(user.first_name),
        task=challenge.task,
        timeout=cfg.captcha_timeout,
    )

    # Комментарии к посту — это ветка, растущая из пересланного поста.
    # Отвечаем в ту же ветку, чтобы человек увидел вопрос под постом.
    kwargs = {}
    if message.chat.is_forum and message.message_thread_id:
        kwargs["message_thread_id"] = message.message_thread_id
    else:
        root = message.message_thread_id or (
            message.reply_to_message.message_id if message.reply_to_message else None
        )
        if root:
            kwargs["reply_parameters"] = ReplyParameters(message_id=root, allow_sending_without_reply=True)

    try:
        sent = await bot.send_message(message.chat.id, text, reply_markup=keyboard, **kwargs)
    except TelegramAPIError as e:
        log.warning("Не удалось отправить капчу: %s", e.message)
        mem.pending.pop(key, None)
        return
    pending.message_id = sent.message_id
    pending.task = asyncio.create_task(expire_captcha(bot, cfg, mem, key))


@router.callback_query(F.data.startswith("z:"))
async def on_captcha_answer(cb: CallbackQuery, bot: Bot, cfg: Config, mem: Memory) -> None:
    try:
        _, uid, idx = cb.data.split(":")
        user_id, answer = int(uid), int(idx)
    except ValueError:
        await cb.answer()
        return
    if cb.from_user.id != user_id:
        await cb.answer(texts.NOT_YOURS, show_alert=True)
        return
    if cb.message is None:
        await cb.answer()
        return

    chat_id = cb.message.chat.id
    key = (chat_id, user_id)
    pending = mem.pending.pop(key, None)
    if pending is None:
        await cb.answer(texts.EXPIRED, show_alert=True)
        await safe_delete(bot, chat_id, cb.message.message_id)
        return
    if pending.task:
        pending.task.cancel()

    if answer == pending.correct:
        mem.verify(key)
        mem.stat(chat_id).passed += 1
        await safe_delete(bot, chat_id, cb.message.message_id)
        published = await republish(bot, chat_id, cb.from_user, pending.messages)
        await cb.answer(texts.PASSED if published else texts.PASSED_RESEND, show_alert=True)
    else:
        mem.stat(chat_id).failed += 1
        await cb.answer(texts.failed(cfg.fail_ban_minutes), show_alert=True)
        await safe_delete(bot, chat_id, cb.message.message_id)
        await punish(bot, cfg, chat_id, user_id)


# ---------- команды админов в группе ----------

@router.message(GROUPS, Command("zina", "start", "help"), IsAdmin())
async def cmd_status(message: Message, bot: Bot, mem: Memory, store: Storage) -> None:
    s = store.get(message.chat.id)
    st = mem.stat(message.chat.id)
    try:
        me = await bot.get_chat_member(message.chat.id, bot.id)
        rights_ok = me.status == ChatMemberStatus.CREATOR or (
            me.status == ChatMemberStatus.ADMINISTRATOR
            and me.can_delete_messages
            and me.can_restrict_members
        )
    except TelegramAPIError:
        rights_ok = False
    await message.reply(
        texts.STATUS.format(
            rights="✅ всё в порядке" if rights_ok else "❌ " + texts.NEED_RIGHTS,
            channels="удаляю" if s.block_channels else "пропускаю (но проверяю на спам)",
            words=len(s.words),
            deleted=st.deleted,
            passed=st.passed,
            failed=st.failed,
        )
    )


@router.message(GROUPS, Command("addword"), IsAdmin())
async def cmd_addword(message: Message, command: CommandObject, store: Storage) -> None:
    new = [w.strip().lower() for w in (command.args or "").split(",") if w.strip()]
    if not new:
        await message.reply("Напишите так: <code>/addword казино, ставки</code>")
        return
    s = store.get(message.chat.id)
    s.words = sorted(set(s.words) | set(new))
    store.save(message.chat.id, s)
    await message.reply(f"Записала. Стоп-слов теперь: {len(s.words)}.")


@router.message(GROUPS, Command("delword"), IsAdmin())
async def cmd_delword(message: Message, command: CommandObject, store: Storage) -> None:
    gone = {w.strip().lower() for w in (command.args or "").split(",") if w.strip()}
    s = store.get(message.chat.id)
    s.words = [w for w in s.words if w not in gone]
    store.save(message.chat.id, s)
    await message.reply(f"Вычеркнула. Стоп-слов осталось: {len(s.words)}.")


@router.message(GROUPS, Command("words"), IsAdmin())
async def cmd_words(message: Message, store: Storage) -> None:
    words = store.get(message.chat.id).words
    if not words:
        await message.reply("Своих стоп-слов нет — работаю по встроенному списку.")
        return
    listing = ", ".join(f"<code>{w}</code>" for w in words)
    await message.reply(f"Стоп-слова этого чата: {listing}")


@router.message(GROUPS, Command("channels"), IsAdmin())
async def cmd_channels(message: Message, command: CommandObject, store: Storage) -> None:
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off"):
        await message.reply(
            "<code>/channels on</code> — удалять комментарии от имени чужих каналов\n"
            "<code>/channels off</code> — пропускать их (спам-фильтр всё равно работает)"
        )
        return
    s = store.get(message.chat.id)
    s.block_channels = arg == "on"
    store.save(message.chat.id, s)
    await message.reply("Каналы теперь не пускаю." if s.block_channels else "Каналы пускаю.")


# ---------- охрана ----------

@router.message(GROUPS, F.new_chat_members | F.left_chat_member)
async def drop_service_message(message: Message, bot: Bot) -> None:
    await safe_delete(bot, message.chat.id, message.message_id)


@router.message(GROUPS)
async def guard(message: Message, bot: Bot, cfg: Config, mem: Memory, store: Storage) -> None:
    await check_message(message, bot, cfg, mem, store, edited=False)


@router.edited_message(GROUPS)
async def guard_edited(message: Message, bot: Bot, cfg: Config, mem: Memory, store: Storage) -> None:
    await check_message(message, bot, cfg, mem, store, edited=True)


async def check_message(
    message: Message, bot: Bot, cfg: Config, mem: Memory, store: Storage, edited: bool
) -> None:
    chat_id = message.chat.id
    if message.is_automatic_forward:
        # пост канала, пересланный в обсуждение: запоминаем, где ветка его комментариев
        await poster.learn_thread_root(bot, mem, store, message)
        return
    if message.reply_to_message and message.reply_to_message.is_automatic_forward:
        await poster.learn_thread_root(bot, mem, store, message.reply_to_message)
    settings = store.get(chat_id)
    stats = mem.stat(chat_id)
    text = text_of(message)

    # Комментарий от имени канала (или анонимного админа)
    if message.sender_chat:
        sender_id = message.sender_chat.id
        if sender_id == chat_id or sender_id == await get_linked_channel(bot, mem, chat_id):
            return
        if settings.block_channels or spam.is_spam(text, settings.words):
            if await safe_delete(bot, chat_id, message.message_id):
                stats.deleted += 1
        return

    user = message.from_user
    if user is None or user.is_bot or user.id == TELEGRAM_SERVICE_ID:
        return
    if user.id in await get_admins(bot, mem, chat_id):
        return

    key = (chat_id, user.id)
    is_spam = spam.is_spam(text, settings.words)

    if mem.is_verified(key):
        if is_spam and await safe_delete(bot, chat_id, message.message_id):
            stats.deleted += 1
        return

    # Непроверенный: сообщение убираем сразу, чтобы спам никто не увидел.
    # Если человек пройдёт проверку, Зина опубликует его комментарий сама.
    if await safe_delete(bot, chat_id, message.message_id):
        stats.deleted += 1
    if edited:
        return
    pending = mem.pending.get(key)
    if pending is not None:
        if is_spam:
            mem.pending.pop(key, None)
            if pending.task:
                pending.task.cancel()
            await safe_delete(bot, chat_id, pending.message_id)
            stats.failed += 1
            await ban_forever(bot, chat_id, user.id)
        elif len(pending.messages) < MAX_HELD_MESSAGES:
            pending.messages.append(message)
        return
    if is_spam or user_name_is_spam(user, settings.words):
        stats.failed += 1
        await ban_forever(bot, chat_id, user.id)
        return
    await send_captcha(message, bot, cfg, mem, key)


# ---------- бота добавили / удалили / поменяли права ----------

@router.my_chat_member(GROUPS)
async def on_my_status(event: ChatMemberUpdated, bot: Bot, mem: Memory, store: Storage) -> None:
    chat_id = event.chat.id
    new = event.new_chat_member
    if new.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        store.delete(chat_id)  # убрали из чата — стираем и настройки
        mem.forget_chat(chat_id)
        return

    if new.status == ChatMemberStatus.ADMINISTRATOR:
        if new.can_delete_messages and new.can_restrict_members:
            text = texts.ON_DUTY
        else:
            text = texts.NEED_RIGHTS
    elif new.status == ChatMemberStatus.MEMBER:
        text = texts.NEED_RIGHTS
    else:
        return
    try:
        await bot.send_message(chat_id, text)
    except TelegramAPIError:
        pass


# ---------- личка ----------

@router.message(F.chat.type == ChatType.PRIVATE, CommandStart())
@router.message(F.chat.type == ChatType.PRIVATE, Command("help"))
async def cmd_start(message: Message, bot: Bot, state: FSMContext) -> None:
    await state.clear()
    me = await bot.me()
    await message.answer(texts.HELP.format(username=me.username))


@router.message(F.chat.type == ChatType.PRIVATE, Command("privacy"))
async def cmd_privacy(message: Message) -> None:
    await message.answer(texts.PRIVACY)


@router.message(F.chat.type == ChatType.PRIVATE)
async def private_other(message: Message, bot: Bot) -> None:
    me = await bot.me()
    await message.answer(texts.HELP.format(username=me.username))
