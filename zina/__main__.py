import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import BotCommand, BotCommandScopeAllChatAdministrators, BotCommandScopeAllPrivateChats

from . import texts
from .config import load_config
from .handlers import router
from .storage import Memory, Storage


async def main() -> None:
    cfg = load_config()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Логи aiogram по каждому апдейту не нужны — в логах не должно быть следов пользователей.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    bot = Bot(cfg.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher(cfg=cfg, mem=Memory(), store=Storage(cfg.db_path))
    dp.include_router(router)

    try:
        me = await bot.me()
    except TelegramUnauthorizedError:
        await bot.session.close()
        raise SystemExit("Telegram не принял токен. Проверьте его в @BotFather и запустите ./set_token.sh ещё раз.")

    await bot.set_my_short_description(texts.SHORT_DESCRIPTION)
    await bot.set_my_description(texts.DESCRIPTION)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Что умеет Зина и как подключить"),
            BotCommand(command="privacy", description="Какие данные хранятся"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await bot.set_my_commands(
        [
            BotCommand(command="zina", description="Статус охраны"),
            BotCommand(command="addword", description="Добавить стоп-слова через запятую"),
            BotCommand(command="delword", description="Убрать стоп-слова"),
            BotCommand(command="words", description="Список стоп-слов"),
            BotCommand(command="channels", description="on/off — пускать ли комментарии каналов"),
        ],
        scope=BotCommandScopeAllChatAdministrators(),
    )

    logging.getLogger("zina").info("Зина на посту: @%s", me.username)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
