"""Сборка и запуск бота."""

from __future__ import annotations

import asyncio
import logging
import re
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from ..cloud import CloudProxy
from ..config import Settings, get_settings
from ..hunter import Hunter
from ..logs import apply_log_level, setup_logging
from ..secrets import SecretStore
from ..state import Store
from .context import AppContext
from .handlers import build_router
from .middleware import AccessMiddleware
from .notifier import Notifier

log = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"\d+:[A-Za-z0-9_-]{30,}")

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
    BotCommand(command="networks", description="Внешние сети и их пулы"),
    BotCommand(command="subnets", description="Выбрать подсети для охоты"),
    BotCommand(command="availability", description="Свободные адреса по пулам"),
    BotCommand(command="connect", description="Подключить VK Cloud"),
    BotCommand(command="disconnect", description="Забыть доступы к облаку"),
    BotCommand(command="cloud", description="Проверить доступ к облаку"),
    BotCommand(command="log", description="Последние строки лога"),
    BotCommand(command="debug", description="Подробный лог запросов к облаку"),
    BotCommand(command="whoami", description="Мой Telegram ID"),
    BotCommand(command="help", description="Справка"),
]


def resolve_token(settings: Settings, secrets: SecretStore) -> str:
    """Токен из окружения, из файла или спрошенный в терминале один раз."""
    if settings.bot_token:
        return settings.bot_token
    if secrets.secrets.bot_token:
        return secrets.secrets.bot_token

    if not sys.stdin or not sys.stdin.isatty():
        raise SystemExit(
            "Токен бота не задан.\n"
            "Запустите один раз в терминале — бот спросит его и запомнит, — "
            "либо укажите BOT_TOKEN в .env.\n"
            "Токен выдаёт @BotFather командой /newbot."
        )

    print("Токен бота ещё не сохранён.")
    print("Получите его у @BotFather (/newbot) и вставьте сюда.")
    while True:
        token = input("Токен: ").strip()
        if TOKEN_RE.fullmatch(token):
            break
        print("Не похоже на токен — ожидается вид 123456789:AA…")

    secrets.secrets.bot_token = token
    secrets.save()
    print(f"Токен сохранён в {secrets.path} (права 0600).")
    print("Дальше всё настраивается в самом боте: отправьте ему /start.")
    return token


def build(settings: Settings) -> tuple[Dispatcher, AppContext, Notifier]:
    store = Store(settings.state_file)
    store.load()

    secrets = SecretStore(settings.credentials_file)
    secrets.load()

    token = resolve_token(settings, secrets)

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    ctx = AppContext(
        settings=settings,
        store=store,
        secrets=secrets,
        cloud=CloudProxy(lambda: ctx.credentials),
        hunter=None,  # type: ignore[arg-type]
        bot=bot,
    )
    ctx.hunter = Hunter(ctx.cloud, store)

    notifier = Notifier(bot, store, ctx.hunter, lambda: ctx.credentials)
    ctx.hunter.add_listener(notifier.on_event)

    dp = Dispatcher(storage=MemoryStorage())
    dp["ctx"] = ctx
    dp["notifier"] = notifier

    # Inner middleware, а не outer: посторонний получает отказ только на
    # осмысленную команду, а не на каждое своё сообщение.
    access = AccessMiddleware(lambda: ctx.admins)
    dp.message.middleware(access)
    dp.callback_query.middleware(access)
    dp.include_router(build_router())

    return dp, ctx, notifier


async def wait_for_telegram(bot: Bot, shutdown: asyncio.Event):
    """Дождаться доступности Telegram.

    Телефон может проснуться раньше сети, а мобильный интернет — пропасть
    на часы. Падать из-за этого нельзя: сам aiogram при первом обращении
    к API не переживает сетевую ошибку, поэтому спрашиваем его здесь, с
    нарастающей паузой, и заодно наполняем кеш ``bot.me()``.
    """
    delay = 5
    while not shutdown.is_set():
        try:
            return await bot.me()
        except TelegramNetworkError as exc:
            log.warning("Telegram недоступен (%s). Повтор через %d с.", exc, delay)
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 60)
    return None


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_file)

    problem = settings.token_problem()
    if problem:
        raise SystemExit(problem)

    dp, ctx, notifier = build(settings)

    async def on_startup() -> None:
        try:
            await ctx.bot.set_my_commands(COMMANDS)
            if ctx.admins:
                await notifier.broadcast(
                    "♻️ <b>Бот перезапущен.</b> Охота не возобновляется "
                    "автоматически — нажмите «Запустить охоту», если нужно."
                )
        except TelegramNetworkError as exc:
            log.warning("не удалось объявить команды: %s", exc)

        log.info(
            "бот @%s запущен; владельцев: %d; облако: %s",
            me.username,
            len(ctx.admins),
            ctx.credentials.describe(),
        )
        if not ctx.admins:
            log.info("владельца ещё нет — первый, кто отправит /start, займёт место")

    dp.startup.register(on_startup)

    shutdown = asyncio.Event()

    def request_shutdown() -> None:
        shutdown.set()
        try:
            asyncio.create_task(dp.stop_polling())
        except RuntimeError:  # polling ещё не начался
            pass

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - не везде есть
            pass

    me = await wait_for_telegram(ctx.bot, shutdown)
    if me is None:
        log.info("остановлено до подключения к Telegram")
        await ctx.bot.session.close()
        return

    try:
        await dp.start_polling(ctx.bot, allowed_updates=dp.resolve_used_update_types())
    except TelegramUnauthorizedError:
        raise SystemExit(
            "Telegram отклонил токен. Удалите его из "
            f"{settings.credentials_file} или .env и запустите бота заново — "
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
    except KeyboardInterrupt:
        log.info("выход")
