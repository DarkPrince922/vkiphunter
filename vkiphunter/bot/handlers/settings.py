"""Настройки цикла и управление списками CIDR."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ...cidr import DEFAULT_EXCLUDES, DEFAULT_TARGETS, CidrError, parse_network
from .. import formatting as fmt
from ..context import AppContext
from ..keyboards import cancel_input, settings_menu, targets_menu
from ..ui import show


Event = Message | CallbackQuery


class Edit(StatesGroup):
    value = State()


#: поле -> (подпись, минимум, максимум)
NUMERIC_FIELDS: dict[str, tuple[str, int, int]] = {
    "interval": ("интервал между попытками, секунды", 5, 3600),
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
    "add_target": ("targets", "add", "целевой диапазон, например <code>217.16.16.0/21</code>"),
    "del_target": ("targets", "del", "какой целевой диапазон убрать"),
    "add_exclude": ("excludes", "add", "диапазон-исключение"),
    "del_exclude": ("excludes", "del", "какое исключение убрать"),
}


async def render_settings(event: Event, ctx: AppContext) -> None:
    await show(event, fmt.settings_text(ctx.state.settings), settings_menu(ctx.state.settings))


async def render_targets(event: Event, ctx: AppContext) -> None:
    await show(event, fmt.targets_text(ctx.state.settings), targets_menu())


async def cmd_settings(message: Message, ctx: AppContext) -> None:
    await render_settings(message, ctx)


async def cb_settings(callback: CallbackQuery, ctx: AppContext) -> None:
    await render_settings(callback, ctx)


async def cmd_targets(message: Message, ctx: AppContext) -> None:
    await render_targets(message, ctx)


async def cb_targets(callback: CallbackQuery, ctx: AppContext) -> None:
    await render_targets(callback, ctx)


# --- ввод значения настройки -------------------------------------------


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

    if action == "reset":
        ctx.state.settings.targets = list(DEFAULT_TARGETS)
        ctx.state.settings.excludes = list(DEFAULT_EXCLUDES)
        await ctx.store.save()
        await callback.answer("Списки сброшены к историческим")
        await render_targets(callback, ctx)
        return

    if action not in LIST_ACTIONS:
        await callback.answer("Неизвестное действие", show_alert=True)
        return

    list_name, mode, label = LIST_ACTIONS[action]
    current = getattr(ctx.state.settings, list_name)
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


async def _apply_list(
    message: Message,
    ctx: AppContext,
    state: FSMContext,
    list_name: str,
    mode: str,
    raw: str,
) -> None:
    settings = ctx.state.settings
    current: list[str] = getattr(settings, list_name)

    try:
        network = str(parse_network(raw))
    except CidrError as exc:
        await message.answer(f"❌ Не похоже на CIDR: {exc}\nПример: <code>217.16.16.0/21</code>")
        return

    if mode == "add":
        if network in current:
            await message.answer("Такой диапазон уже в списке.")
            return
        current.append(network)
        text = f"✅ Добавлено: <code>{network}</code>"
    else:
        if network not in current:
            await message.answer("Такого диапазона в списке нет.")
            return
        current.remove(network)
        text = f"✅ Убрано: <code>{network}</code>"

    await ctx.store.save()
    await state.clear()
    await message.answer(text)
    await show(message, fmt.targets_text(settings), targets_menu())


def build_router() -> Router:
    router = Router(name="settings")
    router.message.register(cmd_settings, Command("settings"))
    router.message.register(cmd_targets, Command("targets"))
    router.callback_query.register(cb_settings, F.data == "ui:settings")
    router.callback_query.register(cb_targets, F.data == "ui:targets")
    router.callback_query.register(cb_edit_field, F.data.startswith("set:"))
    router.callback_query.register(cb_targets_action, F.data.startswith("tgt:"))
    router.message.register(on_value, Edit.value)
    return router
