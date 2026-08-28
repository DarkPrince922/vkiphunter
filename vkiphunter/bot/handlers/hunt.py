"""Запуск и остановка охоты."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from ...cloud import CloudError
from .. import formatting as fmt
from ..context import AppContext
from ..keyboards import back_button, main_menu
from ..ui import show


Event = Message | CallbackQuery


async def start_hunt(event: Event, ctx: AppContext) -> None:
    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    ctx.state.subscribe(chat_id)

    if ctx.hunter.running:
        await show(
            event,
            "Охота уже идёт.\n\n"
            + fmt.status_text(ctx.state, ctx.hunter, ctx.settings.simulate),
            main_menu(True, bool(ctx.state.found)),
        )
        return

    if not ctx.state.settings.targets:
        await show(
            event,
            "🎯 Список целевых диапазонов пуст — искать нечего.\n"
            "Добавьте хотя бы один через /targets.",
            back_button(),
        )
        return

    # Проверяем облако до старта, чтобы не крутить цикл вхолостую.
    try:
        await ctx.cloud.check(ctx.state.settings.network)
    except CloudError as exc:
        await show(
            event,
            f"❌ Не запускаю: облако недоступно.\n\n<code>{exc}</code>",
            back_button(),
        )
        return

    await ctx.hunter.start()
    await ctx.store.save()
    if isinstance(event, CallbackQuery):
        await event.answer("Охота запущена")


async def stop_hunt(event: Event, ctx: AppContext) -> None:
    if not ctx.hunter.running:
        await show(
            event,
            "Охота и так не идёт.\n\n"
            + fmt.status_text(ctx.state, ctx.hunter, ctx.settings.simulate),
            main_menu(False, bool(ctx.state.found)),
        )
        return

    if isinstance(event, CallbackQuery):
        await event.answer("Останавливаю после текущей попытки…")
    else:
        await event.answer("⏹ Останавливаю после текущей попытки…")
    await ctx.hunter.stop(wait=False)


async def cmd_hunt(message: Message, ctx: AppContext) -> None:
    await start_hunt(message, ctx)


async def cb_hunt(callback: CallbackQuery, ctx: AppContext) -> None:
    await start_hunt(callback, ctx)


async def cmd_stop(message: Message, ctx: AppContext) -> None:
    await stop_hunt(message, ctx)


async def cb_stop(callback: CallbackQuery, ctx: AppContext) -> None:
    await stop_hunt(callback, ctx)


def build_router() -> Router:
    router = Router(name="hunt")
    router.message.register(cmd_hunt, Command("hunt", "start_hunt"))
    router.message.register(cmd_stop, Command("stop"))
    router.callback_query.register(cb_hunt, F.data == "hunt:start")
    router.callback_query.register(cb_stop, F.data == "hunt:stop")
    return router
