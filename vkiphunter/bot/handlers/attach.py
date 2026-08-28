"""Привязка найденного floating IP к существующей ВМ.

В режиме порта адрес становится интерфейсом новой машины (мастер /vm).
В режиме floating IP машина уже есть — адрес нужно к ней привязать.
"""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from ...cloud import CloudError
from ...state import MODE_FLOATING_IP
from ..context import AppContext
from ..keyboards import back_button, server_list
from ..ui import show

Event = Message | CallbackQuery


async def _start(event: Event, ctx: AppContext) -> None:
    found = ctx.state.last_found()

    if found is None:
        await show(event, "Пока нет найденного адреса — дождитесь целевого IP.", back_button())
        return
    if found.kind != MODE_FLOATING_IP:
        await show(
            event,
            "Найденный адрес — это порт внешней сети, его не привязывают, "
            "а делают интерфейсом новой машины: /vm",
            back_button(),
        )
        return
    if found.server_id:
        await show(
            event,
            f"Адрес <code>{escape(found.ip)}</code> уже привязан к "
            f"<code>{escape(found.server_name or found.server_id)}</code>.",
            back_button(),
        )
        return

    try:
        servers = await ctx.cloud.list_servers()
    except CloudError as exc:
        await show(event, f"❌ Не получить список ВМ:\n<code>{exc}</code>", back_button())
        return

    if not servers:
        await show(
            event,
            "В проекте нет ни одной ВМ — привязывать адрес не к чему.\n\n"
            "Создайте машину в панели VK Cloud и вернитесь сюда.",
            back_button(),
        )
        return

    await show(
        event,
        f"🔗 К какой ВМ привязать <code>{escape(found.ip)}</code>?",
        server_list(servers),
    )


async def cmd_attach(message: Message, ctx: AppContext) -> None:
    await _start(message, ctx)


async def cb_attach(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer("Собираю список ВМ…")
    await _start(callback, ctx)


async def cb_pick_server(callback: CallbackQuery, ctx: AppContext) -> None:
    server_id = (callback.data or "").split(":", 2)[2]
    found = ctx.state.last_found()
    if found is None:
        await callback.answer("Найденный адрес пропал", show_alert=True)
        return

    try:
        servers = await ctx.cloud.list_servers()
        server = next((s for s in servers if s.id == server_id), None)
        if server is None:
            await callback.answer("ВМ пропала из списка", show_alert=True)
            return

        await show(callback, f"⏳ Привязываю <code>{escape(found.ip)}</code>…")
        await ctx.cloud.attach_floating_ip(found.port_id, server_id)
    except CloudError as exc:
        await show(
            callback,
            f"❌ <b>Не привязалось</b>\n<code>{escape(str(exc))}</code>\n\n"
            f"Адрес <code>{escape(found.ip)}</code> сохранён — "
            "удалять его нельзя, иначе потеряете IP.",
            back_button(),
        )
        return

    found.server_id = server.id
    found.server_name = server.name
    await ctx.store.save()

    await show(
        callback,
        "\n".join(
            [
                "✅ <b>Floating IP привязан</b>",
                "",
                f"IP: <code>{escape(found.ip)}</code>",
                f"ВМ: <code>{escape(server.name)}</code>",
                "",
                "Дальше: откройте нужный порт в security group и на самой ВМ, "
                "поднимите health-endpoint и проверьте адрес "
                "<b>через целевую SIM с выключенным Wi-Fi</b>.",
            ]
        ),
        back_button(),
    )


def build_router() -> Router:
    router = Router(name="attach")
    router.message.register(cmd_attach, Command("attach"))
    router.callback_query.register(cb_attach, F.data == "fip:attach")
    router.callback_query.register(cb_pick_server, F.data.startswith("fip:srv:"))
    return router
