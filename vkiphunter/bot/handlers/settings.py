"""Настройки цикла и управление списками CIDR."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ...cidr import (
    DEFAULT_EXCLUDES,
    DEFAULT_TARGETS,
    VK_RANGES,
    CidrError,
    merge_networks,
    parse_network,
    split_networks,
)
from ...cidr import overlapping_target
from ...state import MODE_FLOATING_IP, MODE_PORT, MODE_TITLES
from .. import formatting as fmt
from ..context import AppContext
from ...cloud import CloudError
from ..keyboards import cancel_input, settings_menu, targets_menu
from ..ui import show


Event = Message | CallbackQuery


class Edit(StatesGroup):
    value = State()


#: поле -> (подпись, минимум, максимум)
NUMERIC_FIELDS: dict[str, tuple[str, int, int]] = {
    "interval": ("интервал между попытками, секунды", 5, 3600),
    "workers": ("сколько адресов пробовать одновременно", 1, 8),
    "port_timeout": ("тайм-аут создания порта, секунды", 10, 600),
    "server_timeout": ("тайм-аут создания ВМ, секунды", 60, 7200),
    "max_attempts": ("лимит попыток (0 — без лимита)", 0, 100000),
    "report_every": ("присылать сводку каждые N попыток", 1, 1000),
    "volume_size": ("размер диска новой ВМ, ГБ", 5, 2000),
}

TEXT_FIELDS: dict[str, str] = {
    "network": "имя внешней сети (исторически <code>internet</code>)",
    "prefix": "префикс имён probe-портов",
    "volume_type": "тип тома (пусто — по умолчанию проекта)",
}

LIST_ACTIONS: dict[str, tuple[str, str, str]] = {
    "add_target": (
        "targets",
        "add",
        "целевые диапазоны — можно вставить сразу список через запятую "
        "или с новой строки",
    ),
    "del_target": ("targets", "del", "какие целевые диапазоны убрать"),
    "add_exclude": ("excludes", "add", "диапазоны-исключения, можно списком"),
    "del_exclude": ("excludes", "del", "какие исключения убрать"),
}


async def render_settings(event: Event, ctx: AppContext) -> None:
    await show(event, fmt.settings_text(ctx.state.settings), settings_menu(ctx.state.settings))


async def subnet_note(ctx: AppContext) -> str:
    """Подсказать, что выбор подсетей отстал от списка целей.

    Цели изменились — значит, подходящих пулов могло стать больше или
    меньше. Молча оставлять старый выбор нельзя: охота пойдёт не туда.
    """
    if not ctx.credentials.configured:
        return ""

    settings = ctx.state.settings
    try:
        from .subnets import collect_subnets

        subnets, _ = await collect_subnets(ctx, settings.network)
    except CloudError:
        return ""

    matching = {
        s.id for s in subnets if overlapping_target(s.cidr, settings.targets)
    }
    chosen = set(settings.subnet_ids)

    if not matching:
        return (
            "\n\n⚠️ Под эти цели в сети "
            f"<code>{settings.network}</code> не подходит ни одна подсеть — "
            "загляните в /networks."
        )
    if chosen == matching:
        return ""
    if not chosen:
        return (
            f"\n\n🎯 Под эти цели подходит подсетей: <b>{len(matching)}</b>, "
            "а сейчас адрес просится из любой. Нажмите «Подсети под эти цели»."
        )
    return (
        f"\n\n🎯 Подходящих подсетей: <b>{len(matching)}</b>, выбрано "
        f"<b>{len(chosen)}</b>. Нажмите «Подсети под эти цели», чтобы "
        "привести выбор в соответствие."
    )


async def render_targets(event: Event, ctx: AppContext) -> None:
    text = fmt.targets_text(ctx.state.settings) + await subnet_note(ctx)
    await show(event, text, targets_menu())


async def cmd_settings(message: Message, ctx: AppContext) -> None:
    await render_settings(message, ctx)


async def cb_settings(callback: CallbackQuery, ctx: AppContext) -> None:
    await render_settings(callback, ctx)


async def cmd_targets(
    message: Message, ctx: AppContext, command: CommandObject
) -> None:
    """Без аргументов — экран, с аргументами — сразу добавить.

        /targets 94.100.176.0/20, 130.49.224.0/19
    """
    if command.args:
        await _add_to_list(message, ctx, "targets", command.args)
        return
    await render_targets(message, ctx)


async def cmd_untarget(
    message: Message, ctx: AppContext, command: CommandObject
) -> None:
    if not command.args:
        await message.answer(
            "Укажите, что убрать:\n<code>/untarget 217.16.16.0/20</code>"
        )
        return
    await _remove_from_list(message, ctx, "targets", command.args)


async def cmd_exclude(
    message: Message, ctx: AppContext, command: CommandObject
) -> None:
    if not command.args:
        await message.answer(
            "Укажите диапазоны-исключения:\n<code>/exclude 90.156.0.0/16</code>"
        )
        return
    await _add_to_list(message, ctx, "excludes", command.args)


async def cb_targets(callback: CallbackQuery, ctx: AppContext) -> None:
    await render_targets(callback, ctx)


# --- ввод значения настройки -------------------------------------------


async def cb_toggle_mode(callback: CallbackQuery, ctx: AppContext) -> None:
    """Переключить способ добычи адреса. Применяется со следующей попытки."""
    settings = ctx.state.settings
    settings.mode = MODE_FLOATING_IP if settings.mode == MODE_PORT else MODE_PORT
    await ctx.store.save()
    await callback.answer(f"Теперь: {MODE_TITLES[settings.mode]}")
    await render_settings(callback, ctx)


async def cb_edit_field(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    field = (callback.data or "").split(":", 1)[1]
    current = getattr(ctx.state.settings, field, None)

    if field in NUMERIC_FIELDS:
        label, low, high = NUMERIC_FIELDS[field]
        prompt = (
            f"Введите {label}.\nДопустимо от {low} до {high}.\n"
            f"Сейчас: <b>{current}</b>"
        )
    elif field in TEXT_FIELDS:
        prompt = (
            f"Введите {TEXT_FIELDS[field]}.\n"
            f"Сейчас: <code>{current or '—'}</code>\n"
            "Для пустого значения отправьте <code>-</code>."
        )
    else:
        await callback.answer("Неизвестное поле", show_alert=True)
        return

    await state.set_state(Edit.value)
    await state.update_data(kind="setting", field=field)
    await show(callback, prompt, cancel_input())


async def cb_targets_action(
    callback: CallbackQuery, ctx: AppContext, state: FSMContext
) -> None:
    action = (callback.data or "").split(":", 1)[1]
    settings = ctx.state.settings

    if action == "vk":
        result = merge_networks(settings.targets, VK_RANGES)
        settings.targets = result.final
        await ctx.store.save()
        await callback.answer(f"Добавлено: {len(result.added)}")

        lines = [f"📋 <b>Диапазоны VK добавлены</b>", ""]
        lines.append(f"Новых: <b>{len(result.added)}</b>")
        if result.duplicates:
            lines.append(f"Уже были: {len(result.duplicates)}")
        if result.absorbed:
            lines.append(
                f"Свёрнуто в более широкие: {len(result.absorbed)} — "
                + ", ".join(f"<code>{a}</code>" for a, _ in result.absorbed[:5])
            )
        lines.append(f"\nВсего целей: <b>{len(settings.targets)}</b>")
        lines.append(await subnet_note(ctx))
        await show(callback, "\n".join(lines), targets_menu())
        return

    if action == "reset":
        settings.targets = list(DEFAULT_TARGETS)
        settings.excludes = list(DEFAULT_EXCLUDES)
        await ctx.store.save()
        await callback.answer("Списки сброшены к историческим")
        await render_targets(callback, ctx)
        return

    if action not in LIST_ACTIONS:
        await callback.answer("Неизвестное действие", show_alert=True)
        return

    list_name, mode, label = LIST_ACTIONS[action]
    current = getattr(settings, list_name)
    listing = "\n".join(f"• <code>{c}</code>" for c in current) or "<i>пусто</i>"

    await state.set_state(Edit.value)
    await state.update_data(kind="list", list_name=list_name, mode=mode)
    await show(
        callback,
        f"Укажите {label}.\n\nТекущий список:\n{listing}",
        cancel_input(),
    )


async def on_value(message: Message, ctx: AppContext, state: FSMContext) -> None:
    data = await state.get_data()
    raw = (message.text or "").strip()

    if data.get("kind") == "setting":
        await _apply_setting(message, ctx, state, data["field"], raw)
    else:
        await _apply_list(message, ctx, state, data["list_name"], data["mode"], raw)


async def _apply_setting(
    message: Message, ctx: AppContext, state: FSMContext, field: str, raw: str
) -> None:
    settings = ctx.state.settings

    if field in NUMERIC_FIELDS:
        label, low, high = NUMERIC_FIELDS[field]
        try:
            value = int(raw)
        except ValueError:
            await message.answer(f"Нужно целое число ({label}). Попробуйте ещё раз.")
            return
        if not low <= value <= high:
            await message.answer(f"Значение должно быть от {low} до {high}.")
            return
        setattr(settings, field, value)
    else:
        value_text = "" if raw == "-" else raw
        if field in ("network", "prefix") and not value_text:
            await message.answer("Это поле не может быть пустым.")
            return
        if field == "prefix" and (" " in value_text or "/" in value_text):
            await message.answer("Префикс без пробелов и слэшей.")
            return
        setattr(settings, field, value_text)

    await ctx.store.save()
    await state.clear()

    note = ""
    if field in ("interval", "port_timeout", "network", "prefix") and ctx.hunter.running:
        note = "\n<i>Применится со следующей попытки.</i>"
    await message.answer(f"✅ Сохранено.{note}")
    await show(message, fmt.settings_text(settings), settings_menu(settings))


async def _add_to_list(
    message: Message, ctx: AppContext, list_name: str, raw: str
) -> None:
    """Добавить диапазоны и показать экран целей."""
    await _apply_list(message, ctx, None, list_name, "add", raw)


async def _remove_from_list(
    message: Message, ctx: AppContext, list_name: str, raw: str
) -> None:
    await _apply_list(message, ctx, None, list_name, "del", raw)


async def _apply_list(
    message: Message,
    ctx: AppContext,
    state: FSMContext | None,
    list_name: str,
    mode: str,
    raw: str,
) -> None:
    settings = ctx.state.settings
    current: list[str] = getattr(settings, list_name)
    values = split_networks(raw)

    if not values:
        await message.answer("Не вижу ни одного диапазона. Пример: <code>217.16.16.0/20</code>")
        return

    if mode == "add":
        result = merge_networks(current, values)
        if not result.added and result.invalid and not result.duplicates:
            await message.answer(
                "❌ Не похоже на CIDR: "
                + ", ".join(f"<code>{v[:40]}</code>" for v in result.invalid[:5])
                + "\nПример: <code>217.16.16.0/20</code>"
            )
            return

        setattr(settings, list_name, result.final)
        lines = [f"✅ Добавлено: <b>{len(result.added)}</b>"]
        if result.added:
            lines.append(", ".join(f"<code>{n}</code>" for n in result.added[:10]))
            if len(result.added) > 10:
                lines.append(f"<i>…и ещё {len(result.added) - 10}</i>")
        if result.duplicates:
            lines.append(f"Уже были: {len(result.duplicates)}")
        if result.nested:
            lines.append(
                "Вложены в существующие и пропущены: "
                + ", ".join(f"<code>{a}</code> ⊂ <code>{b}</code>" for a, b in result.nested[:5])
            )
        if result.absorbed:
            lines.append(
                "Свёрнуто в более широкие: "
                + ", ".join(f"<code>{a}</code> ⊂ <code>{b}</code>" for a, b in result.absorbed[:5])
            )
        if result.invalid:
            lines.append(
                "Не разобрал: "
                + ", ".join(f"<code>{v[:30]}</code>" for v in result.invalid[:5])
            )
        text = "\n".join(lines)
    else:
        removed, missing, invalid = [], [], []
        for value in values:
            try:
                network = str(parse_network(value))
            except CidrError:
                invalid.append(value)
                continue
            if network in current:
                current.remove(network)
                removed.append(network)
            else:
                missing.append(network)

        if not removed and not missing:
            await message.answer("Не разобрал ни одного диапазона.")
            return

        lines = [f"✅ Убрано: <b>{len(removed)}</b>"]
        if removed:
            lines.append(", ".join(f"<code>{n}</code>" for n in removed[:10]))
        if missing:
            lines.append(f"Не было в списке: {len(missing)}")
        if invalid:
            lines.append(f"Не разобрал: {len(invalid)}")
        text = "\n".join(lines)

    await ctx.store.save()
    if state is not None:
        await state.clear()
    await message.answer(text)
    await render_targets(message, ctx)


def build_router() -> Router:
    router = Router(name="settings")
    router.message.register(cmd_settings, Command("settings"))
    router.message.register(cmd_targets, Command("targets"))
    router.message.register(cmd_untarget, Command("untarget", "rmtarget"))
    router.message.register(cmd_exclude, Command("exclude"))
    router.callback_query.register(cb_settings, F.data == "ui:settings")
    router.callback_query.register(cb_targets, F.data == "ui:targets")
    router.callback_query.register(cb_toggle_mode, F.data == "set:mode")
    router.callback_query.register(cb_edit_field, F.data.startswith("set:"))
    router.callback_query.register(cb_targets_action, F.data.startswith("tgt:"))
    router.message.register(on_value, Edit.value)
    return router
