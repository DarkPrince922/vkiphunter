"""Меню, статус, история, справка."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from html import escape

from ...cloud import CloudError
from .. import formatting as fmt
from ..context import AppContext
from ..keyboards import back_button, log_menu, main_menu
from ..ui import show


Event = Message | CallbackQuery


async def render_menu(event: Event, ctx: AppContext) -> None:
    credentials = ctx.credentials
    await show(
        event,
        fmt.status_text(ctx.state, ctx.hunter, credentials),
        main_menu(
            ctx.hunter.running,
            bool(ctx.state.found),
            credentials.configured,
            ctx.state.settings.mode,
        ),
    )


async def cmd_start(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    claimed = ctx.claim_owner(user.id) if user else False

    ctx.state.subscribe(message.chat.id)
    await ctx.store.save()

    if claimed:
        await message.answer(
            "👑 <b>Вы владелец этого бота.</b>\n"
            f"Ваш ID <code>{user.id}</code> сохранён — больше никто "
            "управлять им не сможет."
        )
    await message.answer(fmt.HELP)

    if not ctx.credentials.configured:
        await message.answer(
            "Осталось подключить VK Cloud — доступы я спрошу сам."
        )
    await render_menu(message, ctx)


async def cmd_help(message: Message) -> None:
    await message.answer(fmt.HELP)


async def cmd_menu(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    ctx.state.subscribe(message.chat.id)
    await render_menu(message, ctx)


async def cb_menu(callback: CallbackQuery, ctx: AppContext) -> None:
    await render_menu(callback, ctx)


async def cmd_history(message: Message, ctx: AppContext) -> None:
    await message.answer(fmt.history_text(ctx.state), reply_markup=back_button())


async def cb_history(callback: CallbackQuery, ctx: AppContext) -> None:
    await show(callback, fmt.history_text(ctx.state), back_button())


async def cmd_ips(message: Message, ctx: AppContext) -> None:
    await message.answer(fmt.top_ips_text(ctx.state), reply_markup=back_button())


async def cb_ips(callback: CallbackQuery, ctx: AppContext) -> None:
    await show(callback, fmt.top_ips_text(ctx.state), back_button())


async def cmd_cloud(message: Message, ctx: AppContext) -> None:
    await _cloud_check(message, ctx)


async def cb_cloud(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer("Проверяю…")
    await _cloud_check(callback, ctx)


async def _cloud_check(event: Event, ctx: AppContext) -> None:
    network = ctx.state.settings.network
    try:
        info = await ctx.cloud.check(network)
    except CloudError as exc:
        await show(
            event,
            f"❌ <b>Облако недоступно</b>\n\n<code>{exc}</code>\n\n"
            "Проверьте OpenRC-переменные и что сеть "
            f"<code>{network}</code> существует.",
            back_button(),
        )
        return
    await show(event, fmt.cloud_info_text(info, network), back_button())


LOG_LINES = 25


def _tail(path, lines: int) -> str:
    """Последние строки лога. Файл читаем с конца, чтобы не тянуть мегабайты."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            chunk = min(size, 64 * 1024)
            handle.seek(size - chunk)
            text = handle.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"не прочитать лог: {exc}"
    return "\n".join(text.splitlines()[-lines:])


async def _show_log(event: Event, ctx: AppContext) -> None:
    path = ctx.settings.log_file
    tail = _tail(path, LOG_LINES)
    if not tail:
        await show(
            event,
            f"Лог пуст или не найден: <code>{escape(str(path))}</code>",
            back_button(),
        )
        return

    # 4096 — предел сообщения Telegram, оставляем запас на разметку.
    body = escape(tail)[-3500:]
    await show(
        event,
        f"📄 <b>Последние строки лога</b>\n<code>{escape(str(path))}</code>\n\n"
        f"<pre>{body}</pre>",
        log_menu(),
    )


async def cmd_log(message: Message, ctx: AppContext) -> None:
    await _show_log(message, ctx)


async def cb_log(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer("Читаю лог…")
    await _show_log(callback, ctx)


async def cmd_whoami(message: Message) -> None:
    user = message.from_user
    await message.answer(
        f"Ваш ID: <code>{user.id if user else '—'}</code>\n"
        f"Чат: <code>{message.chat.id}</code>"
    )


async def cb_help(callback: CallbackQuery) -> None:
    await show(callback, fmt.HELP, back_button())


async def cb_cancel(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await render_menu(callback, ctx)


def build_router() -> Router:
    router = Router(name="menu")
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_menu, Command("menu", "status"))
    router.message.register(cmd_history, Command("history"))
    router.message.register(cmd_ips, Command("ips"))
    router.message.register(cmd_cloud, Command("cloud"))
    router.message.register(cmd_whoami, Command("whoami"))
    router.message.register(cmd_log, Command("log", "logs"))
    router.callback_query.register(cb_menu, F.data.in_({"ui:menu", "ui:status"}))
    router.callback_query.register(cb_history, F.data == "ui:history")
    router.callback_query.register(cb_ips, F.data == "ui:ips")
    router.callback_query.register(cb_cloud, F.data == "ui:cloud")
    router.callback_query.register(cb_cancel, F.data == "ui:cancel")
    router.callback_query.register(cb_help, F.data == "ui:help")
    router.callback_query.register(cb_log, F.data == "ui:log")
    return router
