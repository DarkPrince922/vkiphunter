"""Меню, статус, история, справка."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ...cloud import CloudError
from .. import formatting as fmt
from ..context import AppContext
from ..keyboards import back_button, main_menu
from ..ui import show


Event = Message | CallbackQuery


async def render_menu(event: Event, ctx: AppContext) -> None:
    await show(
        event,
        fmt.status_text(ctx.state, ctx.hunter, ctx.settings.simulate),
        main_menu(ctx.hunter.running, bool(ctx.state.found)),
    )


async def cmd_start(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    ctx.state.subscribe(message.chat.id)
    await ctx.store.save()
    await message.answer(fmt.HELP)
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


async def cmd_whoami(message: Message) -> None:
    user = message.from_user
    await message.answer(
        f"Ваш ID: <code>{user.id if user else '—'}</code>\n"
        f"Чат: <code>{message.chat.id}</code>"
    )


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
    router.callback_query.register(cb_menu, F.data.in_({"ui:menu", "ui:status"}))
    router.callback_query.register(cb_history, F.data == "ui:history")
    router.callback_query.register(cb_ips, F.data == "ui:ips")
    router.callback_query.register(cb_cloud, F.data == "ui:cloud")
    router.callback_query.register(cb_cancel, F.data == "ui:cancel")
    return router
