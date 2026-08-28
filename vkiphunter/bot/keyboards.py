"""Инлайн-клавиатуры."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..cloud.base import FlavorInfo, ImageInfo
from ..state import MODE_FLOATING_IP, MODE_TITLES, HuntSettings


def main_menu(
    running: bool,
    has_found: bool,
    connected: bool = True,
    mode: str = "port",
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if not connected:
        kb.button(text="☁️ Подключить VK Cloud", callback_data="conn:menu")
        kb.button(text="❓ Справка", callback_data="ui:help")
        kb.adjust(1)
        return kb.as_markup()
    if running:
        kb.button(text="⏹ Остановить охоту", callback_data="hunt:stop")
    else:
        kb.button(text="▶️ Запустить охоту", callback_data="hunt:start")
    kb.button(text="🔄 Статус", callback_data="ui:status")
    kb.button(text="📜 История", callback_data="ui:history")
    kb.button(text="📈 Адреса", callback_data="ui:ips")
    kb.button(text="🎯 Диапазоны", callback_data="ui:targets")
    kb.button(text="🗺 Подсети", callback_data="ui:subnets")
    kb.button(text="⚙️ Настройки", callback_data="ui:settings")
    kb.button(
        text="🎈 Floating IP" if mode == MODE_FLOATING_IP else "🔌 Порты",
        callback_data="ports:list",
    )
    kb.button(text="📄 Логи", callback_data="ui:log")
    kb.button(text="☁️ Проверка облака", callback_data="ui:cloud")
    kb.button(text="🔌 Доступы к облаку", callback_data="conn:menu")
    if has_found:
        if mode == MODE_FLOATING_IP:
            kb.button(text="🔗 Привязать найденный IP к ВМ", callback_data="fip:attach")
        else:
            kb.button(text="🖥 Создать ВМ на найденном IP", callback_data="vm:start")
    kb.adjust(1, 2, 2, 2, 1, 2, 1, 1)
    return kb.as_markup()


def log_menu(verbose: bool = False) -> InlineKeyboardMarkup:
    toggle = "📋 Обычный лог" if verbose else "🔬 Подробный лог API"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="ui:log")],
            [InlineKeyboardButton(text=toggle, callback_data="ui:debug")],
            [InlineKeyboardButton(text="⬅️ Меню", callback_data="ui:menu")],
        ]
    )


def back_button(target: str = "ui:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Меню", callback_data=target)]]
    )


def settings_menu(settings: HuntSettings) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"🧭 Способ: {MODE_TITLES[settings.mode]}", callback_data="set:mode"
    )
    kb.button(text=f"⏱ Интервал: {settings.interval} с", callback_data="set:interval")
    kb.button(text=f"👥 Воркеров: {settings.workers}", callback_data="set:workers")
    kb.button(text=f"🌐 Сеть: {settings.network}", callback_data="set:network")
    kb.button(
        text=f"🗺 Подсети: {settings.subnet_summary()}",
        callback_data="ui:subnets",
    )
    kb.button(text=f"🏷 Префикс: {settings.prefix}", callback_data="set:prefix")
    kb.button(
        text=f"⌛️ Тайм-аут порта: {settings.port_timeout} с",
        callback_data="set:port_timeout",
    )
    kb.button(
        text=f"⌛️ Тайм-аут ВМ: {settings.server_timeout} с",
        callback_data="set:server_timeout",
    )
    kb.button(
        text=f"🔢 Лимит попыток: {settings.max_attempts or '∞'}",
        callback_data="set:max_attempts",
    )
    kb.button(
        text=f"🔔 Сводка каждые: {settings.report_every}",
        callback_data="set:report_every",
    )
    kb.button(text=f"💾 Диск ВМ: {settings.volume_size} ГБ", callback_data="set:volume_size")
    kb.button(
        text=f"💽 Тип диска: {settings.volume_type or 'по умолчанию'}",
        callback_data="set:volume_type",
    )
    kb.button(text="⬅️ Меню", callback_data="ui:menu")
    kb.adjust(1)
    return kb.as_markup()


def targets_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить цель", callback_data="tgt:add_target")
    kb.button(text="➖ Убрать цель", callback_data="tgt:del_target")
    kb.button(text="🚫 Добавить исключение", callback_data="tgt:add_exclude")
    kb.button(text="♻️ Убрать исключение", callback_data="tgt:del_exclude")
    kb.button(text="↩️ Сбросить к историческим", callback_data="tgt:reset")
    kb.button(text="⬅️ Меню", callback_data="ui:menu")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def ports_menu(own_count: int, mode: str = "port") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data="ports:list")
    if own_count:
        what = "floating IP" if mode == MODE_FLOATING_IP else "probe-порты"
        kb.button(
            text=f"🧹 Освободить свои {what} ({own_count})",
            callback_data="ports:cleanup",
        )
    kb.button(text="⬅️ Меню", callback_data="ui:menu")
    kb.adjust(1)
    return kb.as_markup()


def confirm(action: str, cancel: str = "ui:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data=action),
                InlineKeyboardButton(text="✖️ Отмена", callback_data=cancel),
            ]
        ]
    )


def found_actions(mode: str = "port") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if mode == MODE_FLOATING_IP:
        kb.button(text="🔗 Привязать этот IP к ВМ", callback_data="fip:attach")
    else:
        kb.button(text="🖥 Создать ВМ на этом порту", callback_data="vm:start")
    kb.button(text="⬅️ Меню", callback_data="ui:menu")
    kb.adjust(1)
    return kb.as_markup()


def flavor_list(flavors: list[FlavorInfo], show_all: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for flavor in flavors[:24]:
        kb.button(
            text=f"{flavor.name} · {flavor.vcpus} vCPU · {flavor.ram} МБ",
            callback_data=f"vm:flavor:{flavor.id}",
        )
    kb.button(
        text="📋 Все флейворы" if not show_all else "🎯 Только 1 vCPU / 2 ГБ",
        callback_data=f"vm:flavors:{'all' if not show_all else 'default'}",
    )
    kb.button(text="✖️ Отмена", callback_data="vm:cancel")
    kb.adjust(1)
    return kb.as_markup()


def image_list(images: list[ImageInfo], show_all: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for image in images[:24]:
        kb.button(text=image.name, callback_data=f"vm:image:{image.id}")
    kb.button(
        text="📋 Все образы" if not show_all else "🎯 Только Ubuntu 24.04",
        callback_data=f"vm:images:{'all' if not show_all else 'default'}",
    )
    kb.button(text="✖️ Отмена", callback_data="vm:cancel")
    kb.adjust(1)
    return kb.as_markup()


def server_list(servers) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for server in servers[:24]:
        kb.button(
            text=f"🖥 {server.name} · {server.status}",
            callback_data=f"fip:srv:{server.id}",
        )
    kb.button(text="✖️ Отмена", callback_data="ui:menu")
    kb.adjust(1)
    return kb.as_markup()


def keypair_list(keys: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key in keys[:24]:
        kb.button(text=f"🔑 {key}", callback_data=f"vm:key:{key}")
    kb.button(text="🚫 Без ключа", callback_data="vm:key:")
    kb.button(text="✖️ Отмена", callback_data="vm:cancel")
    kb.adjust(1)
    return kb.as_markup()


def cancel_input() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="ui:cancel")]]
    )
