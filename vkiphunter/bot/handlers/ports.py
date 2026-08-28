"""Просмотр портов внешней сети и уборка своих probe-портов."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from ...cloud import CloudError
from .. import formatting as fmt
from ..context import AppContext
from ..keyboards import back_button, confirm, ports_menu
from ..ui import show


Event = Message | CallbackQuery


def _protected_port_ids(ctx: AppContext) -> set[str]:
    """Порты с найденными адресами трогать нельзя ни при какой уборке."""
    return {f.port_id for f in ctx.state.found}


async def _list_ports(event: Event, ctx: AppContext) -> None:
    settings = ctx.state.settings
    try:
        ports = await ctx.cloud.list_ports(settings.network)
    except CloudError as exc:
        await show(event, f"❌ Не удалось получить порты:\n<code>{exc}</code>", back_button())
        return

    protected = _protected_port_ids(ctx)
    disposable = [
        p
        for p in ports
        if p.name.startswith(settings.prefix)
        and p.id not in protected
        and not p.device_id
    ]
    text = fmt.ports_text(ports, settings.prefix, settings.network)
    if protected:
        text += f"\n\n🔒 Защищено найденных портов: <b>{len(protected)}</b> — не удаляются."
    await show(event, text, ports_menu(len(disposable)))


async def cmd_ports(message: Message, ctx: AppContext) -> None:
    await _list_ports(message, ctx)


async def cb_ports(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer("Запрашиваю…")
    await _list_ports(callback, ctx)


async def cmd_cleanup(message: Message, ctx: AppContext) -> None:
    await show(
        message,
        "🧹 Удалить все свои probe-порты "
        f"(<code>{ctx.state.settings.prefix}-*</code>) в сети "
        f"<code>{ctx.state.settings.network}</code>?\n\n"
        "Найденные целевые порты и порты, подключённые к ВМ, не тронутся. "
        "Чужие порты не трогаются никогда.",
        confirm("ports:cleanup_yes"),
    )


async def cb_cleanup(callback: CallbackQuery, ctx: AppContext) -> None:
    await show(
        callback,
        "🧹 Удалить все свои probe-порты "
        f"(<code>{ctx.state.settings.prefix}-*</code>)?\n\n"
        "Найденные целевые порты и подключённые к ВМ не тронутся.",
        confirm("ports:cleanup_yes"),
    )


async def cb_cleanup_yes(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer("Убираю…")
    settings = ctx.state.settings
    protected = _protected_port_ids(ctx)

    try:
        ports = await ctx.cloud.list_ports(settings.network)
    except CloudError as exc:
        await show(callback, f"❌ Не удалось получить порты:\n<code>{exc}</code>", back_button())
        return

    deleted, failed, skipped = 0, [], 0
    for port in ports:
        if not port.name.startswith(settings.prefix):
            continue
        if port.id in protected or port.device_id:
            skipped += 1
            continue
        try:
            await ctx.cloud.delete_port(port.id)
            deleted += 1
        except CloudError as exc:
            failed.append(f"{port.id[:8]}: {exc}")

    lines = [f"🧹 Удалено портов: <b>{deleted}</b>"]
    if skipped:
        lines.append(f"Пропущено защищённых: {skipped}")
    if failed:
        lines.append("Не удалось удалить:")
        lines += [f"• <code>{f}</code>" for f in failed[:10]]
    await show(callback, "\n".join(lines), back_button())


def build_router() -> Router:
    router = Router(name="ports")
    router.message.register(cmd_ports, Command("ports"))
    router.message.register(cmd_cleanup, Command("cleanup"))
    router.callback_query.register(cb_ports, F.data == "ports:list")
    router.callback_query.register(cb_cleanup, F.data == "ports:cleanup")
    router.callback_query.register(cb_cleanup_yes, F.data == "ports:cleanup_yes")
    return router
