"""Просмотр выданных ресурсов и уборка своих пробных адресов.

В режиме порта показываются порты внешней сети, в режиме floating IP —
floating IP проекта. Чужое не удаляется никогда: свои ресурсы опознаются
по префиксу имени (порт) или описания (floating IP).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from ...cloud import CloudError
from ...state import MODE_FLOATING_IP
from .. import formatting as fmt
from ..context import AppContext
from ..keyboards import back_button, confirm, ports_menu
from ..ui import show

Event = Message | CallbackQuery


def _protected(ctx: AppContext) -> set[str]:
    """Найденные ресурсы не трогаем ни при какой уборке."""
    return {f.port_id for f in ctx.state.found}


async def _list_resources(event: Event, ctx: AppContext) -> None:
    settings = ctx.state.settings
    protected = _protected(ctx)

    try:
        if settings.mode == MODE_FLOATING_IP:
            fips = await ctx.cloud.list_floating_ips()
            text = fmt.floating_ips_text(fips, settings.prefix)
            disposable = [
                f
                for f in fips
                if f.description.startswith(settings.prefix)
                and f.id not in protected
                and not f.attached
            ]
        else:
            ports = await ctx.cloud.list_ports(settings.network)
            text = fmt.ports_text(ports, settings.prefix, settings.network)
            disposable = [
                p
                for p in ports
                if p.name.startswith(settings.prefix)
                and p.id not in protected
                and not p.device_id
            ]
    except CloudError as exc:
        await show(event, f"❌ Не удалось получить список:\n<code>{exc}</code>", back_button())
        return

    if protected:
        text += f"\n\n🔒 Защищено найденных: <b>{len(protected)}</b> — не удаляются."
    await show(event, text, ports_menu(len(disposable), settings.mode))


async def cmd_ports(message: Message, ctx: AppContext) -> None:
    await _list_resources(message, ctx)


async def cb_ports(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer("Запрашиваю…")
    await _list_resources(callback, ctx)


def _cleanup_question(ctx: AppContext) -> str:
    settings = ctx.state.settings
    if settings.mode == MODE_FLOATING_IP:
        what = f"floating IP с описанием <code>{settings.prefix}-*</code>"
        skipped = "Найденный адрес и привязанные к ВМ не тронутся."
    else:
        what = (
            f"probe-порты <code>{settings.prefix}-*</code> в сети "
            f"<code>{settings.network}</code>"
        )
        skipped = "Найденный порт и подключённые к ВМ не тронутся."
    return (
        f"🧹 Освободить все свои {what}?\n\n{skipped} "
        "Чужие ресурсы не трогаются никогда."
    )


async def cmd_cleanup(message: Message, ctx: AppContext) -> None:
    await show(message, _cleanup_question(ctx), confirm("ports:cleanup_yes"))


async def cb_cleanup(callback: CallbackQuery, ctx: AppContext) -> None:
    await show(callback, _cleanup_question(ctx), confirm("ports:cleanup_yes"))


async def cb_cleanup_yes(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer("Убираю…")
    settings = ctx.state.settings
    protected = _protected(ctx)
    floating = settings.mode == MODE_FLOATING_IP

    try:
        if floating:
            items = [
                (f.id, f.description, f.attached)
                for f in await ctx.cloud.list_floating_ips()
            ]
        else:
            items = [
                (p.id, p.name, bool(p.device_id))
                for p in await ctx.cloud.list_ports(settings.network)
            ]
    except CloudError as exc:
        await show(callback, f"❌ Не удалось получить список:\n<code>{exc}</code>", back_button())
        return

    deleted, failed, skipped = 0, [], 0
    for resource_id, label, attached in items:
        if not label.startswith(settings.prefix):
            continue
        if resource_id in protected or attached:
            skipped += 1
            continue
        try:
            if floating:
                await ctx.cloud.delete_floating_ip(resource_id)
            else:
                await ctx.cloud.delete_port(resource_id)
            deleted += 1
        except CloudError as exc:
            failed.append(f"{resource_id[:8]}: {exc}")

    lines = [f"🧹 Освобождено: <b>{deleted}</b>"]
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
