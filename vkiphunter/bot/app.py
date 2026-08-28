"""Сборка и запуск бота."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from ..cloud import build_backend
from ..config import Settings, get_settings
from ..hunter import Hunter
from ..state import Store
from .context import AppContext
from .handlers import build_router
from .middleware import AccessMiddleware
from .notifier import Notifier

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="hunt", description="Запустить охоту за IP"),
    BotCommand(command="stop", description="Остановить охоту"),
    BotCommand(command="status", description="Текущее состояние"),
    BotCommand(command="history", description="Последние попытки"),
    BotCommand(command="ips", description="Выданные адреса"),
    BotCommand(command="ports", description="Порты внешней сети"),
    BotCommand(command="cleanup", description="Удалить свои probe-порты"),
    BotCommand(command="vm", description="Создать ВМ на найденном порту"),
    BotCommand(command="settings", description="Настройки цикла"),
    BotCommand(command="targets", description="Целевые диапазоны"),
    BotCommand(command="cloud", description="Проверить доступ к облаку"),
    BotCommand(command="whoami", description="Мой Telegram ID"),
    BotCommand(command="help", description="Справка"),
]


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("openstack").setLevel(logging.WARNING)
    logging.getLogger("keystoneauth").setLevel(logging.WARNING)


def build(settings: Settings) -> tuple[Dispatcher, AppContext, Notifier]:
    store = Store(settings.state_file)
    store.load()

    cloud = build_backend(settings)
    hunter = Hunter(cloud, store)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    ctx = AppContext(settings=settings, store=store, cloud=cloud, hunter=hunter, bot=bot)

    notifier = Notifier(bot, store, hunter, settings.simulate)
    hunter.add_listener(notifier.on_event)

    dp = Dispatcher(storage=MemoryStorage())
    dp["ctx"] = ctx
    dp["notifier"] = notifier

    # Inner middleware, а не outer: посторонний получает отказ только на
    # осмысленную команду, а не на каждое своё сообщение.
    access = AccessMiddleware(settings.admins)
    dp.message.middleware(access)
    dp.callback_query.middleware(access)
    dp.include_router(build_router())

    return dp, ctx, notifier


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    problems = settings.describe_problems()
    if problems:
        for problem in problems:
            log.error("конфигурация: %s", problem)
        raise SystemExit(
            "Не хватает конфигурации:\n  - " + "\n  - ".join(problems)
        )

    dp, ctx, notifier = build(settings)

    async def on_startup() -> None:
        me = await ctx.bot.get_me()
        try:
            await ctx.bot.set_my_commands(COMMANDS)
            await notifier.broadcast(
                "♻️ <b>Бот перезапущен.</b> Охота не возобновляется автоматически — "
                "нажмите «Запустить охоту», если нужно продолжить."
            )
        except TelegramNetworkError as exc:
            # Сеть может подняться позже — polling переподключится сам.
            log.warning("Telegram недоступен при старте: %s", exc)
        log.info(
            "бот @%s запущен; админов: %d; симуляция: %s",
            me.username,
            len(settings.admins),
            settings.simulate,
        )

    dp.startup.register(on_startup)

    try:
        await dp.start_polling(ctx.bot, allowed_updates=dp.resolve_used_update_types())
    except TelegramUnauthorizedError:
        raise SystemExit(
            "Telegram отклонил BOT_TOKEN. Проверьте значение в .env — "
            "новый токен выдаёт @BotFather командой /token."
        ) from None
    finally:
        log.info("останавливаюсь…")
        await ctx.hunter.stop()
        await ctx.store.save()
        await ctx.cloud.close()
        await ctx.bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code:
            raise
        log.info("выход")
