"""Выбор подсети внешней сети, из которой просить адрес.

Внешняя сеть VK Cloud состоит из нескольких подсетей — это разные пулы
адресов. Без явного выбора Neutron отдаёт адрес из той, где сейчас есть
свободные, поэтому можно бесконечно получать один и тот же чужой пул.
Neutron принимает ``subnet_id`` при выделении, чем и пользуемся.
"""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ...cidr import overlapping_target
from ...cloud import CloudError
from ..context import AppContext
from ..ui import show

Event = Message | CallbackQuery


def subnets_keyboard(subnets, targets, chosen: set[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for subnet in subnets[:20]:
        hit = overlapping_target(subnet.cidr, targets)
        mark = "🎯 " if hit else ""
        tick = "✅ " if subnet.id in chosen else "▫️ "
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{tick}{mark}{subnet.cidr}",
                    callback_data=f"sub:tog:{subnet.id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="🎯 Все подходящие", callback_data="sub:all")]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🎲 Любая подсеть" + (" ✅" if not chosen else ""),
                callback_data="sub:none",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="⬅️ Меню", callback_data="ui:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def distribution_hint(chosen_count: int, workers: int) -> str:
    """Как выбранные подсети разложатся по воркерам."""
    if not chosen_count:
        return "Адрес просится из любой подсети — какую даст облако."
    if workers == 1:
        if chosen_count == 1:
            return "Один воркер, одна подсеть."
        return f"Один воркер обходит все {chosen_count} по очереди."
    if workers >= chosen_count:
        return (
            f"{workers} воркеров на {chosen_count} подсети — "
            "каждый стучится в свою."
        )
    return (
        f"{workers} воркеров на {chosen_count} подсетей — "
        "вместе обходят все по очереди."
    )


async def _show_subnets(event: Event, ctx: AppContext) -> None:
    settings = ctx.state.settings
    try:
        subnets = await ctx.cloud.list_subnets(settings.network)
    except CloudError as exc:
        await show(event, f"❌ Не получить подсети:\n<code>{exc}</code>")
        return

    if not subnets:
        await show(event, f"У сети <code>{escape(settings.network)}</code> нет подсетей.")
        return

    matching = [s for s in subnets if overlapping_target(s.cidr, settings.targets)]
    chosen = set(settings.subnet_ids)

    lines = [
        f"🗺 <b>Подсети сети {escape(settings.network)}</b>",
        "",
        "Это отдельные пулы адресов. Отметьте нужные — воркеры разложатся "
        "по ним и будут проверять несколько пулов сразу.",
        "",
    ]
    for subnet in subnets[:20]:
        hit = overlapping_target(subnet.cidr, settings.targets)
        mark = "🎯" if hit else "▫️"
        name = f" · {escape(subnet.name)}" if subnet.name else ""
        line = f"{mark} <code>{escape(subnet.cidr)}</code>{name}"
        if hit:
            line += f"\n    <i>пересекается с целью {escape(hit)}</i>"
        if subnet.id in chosen:
            line += "\n    <b>выбрана</b>"
        lines.append(line)

    lines.append("")
    lines.append(f"<i>{distribution_hint(len(chosen), settings.workers)}</i>")
    lines.append("")
    if matching:
        lines.append(
            f"🎯 Подходящих подсетей: <b>{len(matching)}</b> — отметьте их, "
            "и чужие пулы перестанут попадаться."
        )
    else:
        lines.append(
            "⚠️ <b>Ни одна подсеть не пересекается с целевыми диапазонами.</b>\n"
            "Значит, в этой сети нужных адресов нет вообще, и ждать бесполезно: "
            "проверьте /targets или посмотрите другую внешнюю сеть в /settings."
        )

    await show(
        event,
        "\n".join(lines),
        subnets_keyboard(subnets, settings.targets, chosen),
    )


# --- внешние сети -------------------------------------------------------


def networks_keyboard(networks, current: str) -> InlineKeyboardMarkup:
    rows = []
    for network in networks[:15]:
        tick = "✅ " if network.name == current else "▫️ "
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{tick}{network.name}",
                    callback_data=f"net:pick:{network.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="🗺 Подсети", callback_data="ui:subnets")])
    rows.append([InlineKeyboardButton(text="⬅️ Меню", callback_data="ui:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_networks(event: Event, ctx: AppContext) -> None:
    settings = ctx.state.settings
    try:
        networks = await ctx.cloud.list_networks()
    except CloudError as exc:
        await show(event, f"❌ Не получить сети:\n<code>{exc}</code>")
        return

    external = [n for n in networks if n.external] or networks
    lines = ["🌐 <b>Внешние сети проекта</b>", ""]
    hits: list[str] = []

    for network in external[:15]:
        mark = "✅" if network.name == settings.network else "▫️"
        lines.append(f"{mark} <b>{escape(network.name)}</b>")
        if not network.subnets:
            lines.append("    <i>без подсетей</i>")
        for subnet in network.subnets[:10]:
            hit = overlapping_target(subnet.cidr, settings.targets)
            if hit:
                hits.append(f"{network.name} → {subnet.cidr}")
            bullet = "🎯" if hit else "▫️"
            lines.append(f"    {bullet} <code>{escape(subnet.cidr)}</code>")
        lines.append("")

    if hits:
        lines.append("🎯 <b>Целевые диапазоны найдены здесь:</b>")
        lines += [f"• {escape(h)}" for h in hits]
        lines.append("")
        lines.append(
            "Выберите нужную сеть кнопкой, затем отметьте подсети в /subnets."
        )
    else:
        lines.append(
            "⚠️ <b>Ни в одной сети проекта нет целевых диапазонов.</b>\n\n"
            "Значит, этому проекту такие адреса сейчас не выдаются вообще — "
            "ни ожиданием, ни воркерами это не изменить. Проверьте /targets "
            "или заведите проект в регионе, где нужный пул есть."
        )

    await show(event, "\n".join(lines), networks_keyboard(external, settings.network))


async def cmd_networks(message: Message, ctx: AppContext) -> None:
    await _show_networks(message, ctx)


async def cb_networks(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer("Запрашиваю сети…")
    await _show_networks(callback, ctx)


async def cb_pick_network(callback: CallbackQuery, ctx: AppContext) -> None:
    network_id = (callback.data or "").split(":", 2)[2]
    try:
        networks = await ctx.cloud.list_networks()
    except CloudError as exc:
        await show(callback, f"❌ Не получить сети:\n<code>{exc}</code>")
        return

    network = next((n for n in networks if n.id == network_id), None)
    if network is None:
        await callback.answer("Сеть пропала из списка", show_alert=True)
        return

    settings = ctx.state.settings
    settings.network = network.name
    # Подсети принадлежали прежней сети — их выбор больше не действителен.
    settings.subnets = []
    await ctx.store.save()

    await callback.answer(f"Сеть: {network.name}")
    await _show_networks(callback, ctx)


async def cmd_subnets(message: Message, ctx: AppContext) -> None:
    await _show_subnets(message, ctx)


async def cb_subnets(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer("Запрашиваю подсети…")
    await _show_subnets(callback, ctx)


async def cb_toggle(callback: CallbackQuery, ctx: AppContext) -> None:
    subnet_id = (callback.data or "").split(":", 2)[2]
    settings = ctx.state.settings

    try:
        subnets = await ctx.cloud.list_subnets(settings.network)
    except CloudError as exc:
        await show(callback, f"❌ Не получить подсети:\n<code>{exc}</code>")
        return

    subnet = next((s for s in subnets if s.id == subnet_id), None)
    if subnet is None:
        await callback.answer("Подсеть пропала из списка", show_alert=True)
        return

    if subnet_id in set(settings.subnet_ids):
        settings.subnets = [s for s in settings.subnets if s.get("id") != subnet_id]
        await callback.answer(f"Убрана {subnet.cidr}")
    else:
        settings.subnets.append({"id": subnet.id, "cidr": subnet.cidr})
        if overlapping_target(subnet.cidr, settings.targets):
            await callback.answer(f"Добавлена {subnet.cidr}")
        else:
            await callback.answer(
                f"{subnet.cidr} не пересекается с целями — попаданий из неё не будет",
                show_alert=True,
            )

    await ctx.store.save()
    await _show_subnets(callback, ctx)


async def cb_all(callback: CallbackQuery, ctx: AppContext) -> None:
    settings = ctx.state.settings
    try:
        subnets = await ctx.cloud.list_subnets(settings.network)
    except CloudError as exc:
        await show(callback, f"❌ Не получить подсети:\n<code>{exc}</code>")
        return

    matching = [s for s in subnets if overlapping_target(s.cidr, settings.targets)]
    if not matching:
        await callback.answer("Подходящих подсетей нет", show_alert=True)
        return

    settings.subnets = [{"id": s.id, "cidr": s.cidr} for s in matching]
    await ctx.store.save()
    await callback.answer(f"Выбрано подсетей: {len(matching)}")
    await _show_subnets(callback, ctx)


async def cb_none(callback: CallbackQuery, ctx: AppContext) -> None:
    ctx.state.settings.subnets = []
    await ctx.store.save()
    await callback.answer("Адрес будет проситься из любой подсети")
    await _show_subnets(callback, ctx)


def build_router() -> Router:
    router = Router(name="subnets")
    router.message.register(cmd_subnets, Command("subnets", "pools"))
    router.message.register(cmd_networks, Command("networks", "nets"))
    router.callback_query.register(cb_networks, F.data == "ui:networks")
    router.callback_query.register(cb_pick_network, F.data.startswith("net:pick:"))
    router.callback_query.register(cb_subnets, F.data == "ui:subnets")
    router.callback_query.register(cb_toggle, F.data.startswith("sub:tog:"))
    router.callback_query.register(cb_all, F.data == "sub:all")
    router.callback_query.register(cb_none, F.data == "sub:none")
    return router
