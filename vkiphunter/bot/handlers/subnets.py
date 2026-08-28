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


def subnets_keyboard(subnets, targets, current: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for subnet in subnets[:20]:
        hit = overlapping_target(subnet.cidr, targets)
        mark = "🎯 " if hit else ""
        chosen = " ✅" if subnet.id == current else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{subnet.cidr}{chosen}",
                    callback_data=f"sub:pick:{subnet.id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🎲 Любая подсеть" + (" ✅" if not current else ""),
                callback_data="sub:pick:any",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="⬅️ Меню", callback_data="ui:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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

    lines = [
        f"🗺 <b>Подсети сети {escape(settings.network)}</b>",
        "",
        "Это отдельные пулы адресов. Можно просить адрес из конкретного —",
        "тогда чужие пулы перестанут попадаться.",
        "",
    ]
    for subnet in subnets[:20]:
        hit = overlapping_target(subnet.cidr, settings.targets)
        mark = "🎯" if hit else "▫️"
        name = f" · {escape(subnet.name)}" if subnet.name else ""
        line = f"{mark} <code>{escape(subnet.cidr)}</code>{name}"
        if hit:
            line += f"\n    <i>пересекается с целью {escape(hit)}</i>"
        if subnet.id == settings.subnet_id:
            line += "\n    <b>выбрана сейчас</b>"
        lines.append(line)

    lines.append("")
    if matching:
        lines.append(
            f"🎯 Подходящих подсетей: <b>{len(matching)}</b> — выберите одну, "
            "и адреса будут приходить только из неё."
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
        subnets_keyboard(subnets, settings.targets, settings.subnet_id),
    )


async def cmd_subnets(message: Message, ctx: AppContext) -> None:
    await _show_subnets(message, ctx)


async def cb_subnets(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer("Запрашиваю подсети…")
    await _show_subnets(callback, ctx)


async def cb_pick(callback: CallbackQuery, ctx: AppContext) -> None:
    choice = (callback.data or "").split(":", 2)[2]
    settings = ctx.state.settings

    if choice == "any":
        settings.subnet_id = ""
        settings.subnet_label = ""
        await ctx.store.save()
        await callback.answer("Адрес будет проситься из любой подсети")
        await _show_subnets(callback, ctx)
        return

    try:
        subnets = await ctx.cloud.list_subnets(settings.network)
    except CloudError as exc:
        await show(callback, f"❌ Не получить подсети:\n<code>{exc}</code>")
        return

    subnet = next((s for s in subnets if s.id == choice), None)
    if subnet is None:
        await callback.answer("Подсеть пропала из списка", show_alert=True)
        return

    settings.subnet_id = subnet.id
    settings.subnet_label = subnet.cidr
    await ctx.store.save()

    note = ""
    if not overlapping_target(subnet.cidr, settings.targets):
        note = (
            "\n\n⚠️ Эта подсеть не пересекается с целевыми диапазонами — "
            "попадания из неё не будет."
        )
    await callback.answer(f"Выбрана {subnet.cidr}")
    await show(
        callback,
        f"✅ Адрес будет проситься из <code>{escape(subnet.cidr)}</code>."
        f"{note}\n\n<i>Применится со следующей попытки.</i>",
        subnets_keyboard(subnets, settings.targets, settings.subnet_id),
    )


def build_router() -> Router:
    router = Router(name="subnets")
    router.message.register(cmd_subnets, Command("subnets", "pools"))
    router.callback_query.register(cb_subnets, F.data == "ui:subnets")
    router.callback_query.register(cb_pick, F.data.startswith("sub:pick:"))
    return router
