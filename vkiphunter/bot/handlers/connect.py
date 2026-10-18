"""Мастер подключения к VK Cloud прямо в чате.

Ничего не нужно класть в .env: бот спросит доступы, проверит их живым
запросом к API и только потом сохранит в файл с правами 0600. Сообщение
с паролем удаляется из чата сразу после чтения.
"""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ...cloud import CloudError
from ...openrc import parse_openrc
from ...secrets import (
    METHOD_APPCRED,
    METHOD_PASSWORD,
    METHOD_SIMULATE,
    CloudCredentials,
)
from ..context import AppContext
from ..keyboards import back_button, confirm
from ..ui import show

router_name = "connect"

Event = Message | CallbackQuery

VK_AUTH_URL = "https://infra.mail.ru:35357/v3/"


class Connect(StatesGroup):
    openrc = State()
    auth_url = State()
    username = State()
    password = State()
    project = State()
    appcred_id = State()
    appcred_secret = State()
    region = State()


def method_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Вставить openrc-файл", callback_data="conn:openrc")],
            [InlineKeyboardButton(text="🔑 Логин и пароль", callback_data="conn:password")],
            [InlineKeyboardButton(text="🎫 Application credential", callback_data="conn:appcred")],
            [InlineKeyboardButton(text="🧪 Режим симуляции", callback_data="conn:simulate")],
            [InlineKeyboardButton(text="⬅️ Меню", callback_data="ui:menu")],
        ]
    )


def _kb(*rows: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data)] for text, data in rows
        ]
        + [[InlineKeyboardButton(text="✖️ Отмена", callback_data="conn:cancel")]]
    )


async def connect_intro(event: Event, ctx: AppContext) -> None:
    current = ctx.credentials
    lines = [
        "☁️ <b>Подключение к VK Cloud</b>",
        "",
        f"Сейчас: <b>{escape(current.describe())}</b>",
        "",
        "Быстрее всего — прислать содержимое openrc-файла проекта: бот сам "
        "разберёт его и спросит только пароль.",
    ]
    await show(event, "\n".join(lines), method_menu())


async def _finish(event: Event, ctx: AppContext, state: FSMContext) -> None:
    """Проверить собранные креды живым запросом и сохранить при успехе."""
    data = await state.get_data()
    credentials = CloudCredentials(**data["cred"])
    network = ctx.state.settings.network

    await show(event, "⏳ Проверяю доступ к облаку…")

    previous = ctx.secrets.secrets.cloud
    ctx.secrets.secrets.cloud = credentials
    await ctx.cloud.reset()
    try:
        info = await ctx.cloud.check(network)
    except CloudError as exc:
        ctx.secrets.secrets.cloud = previous
        await ctx.cloud.reset()
        await state.clear()
        await show(
            event,
            f"❌ <b>Доступ не работает</b>\n<code>{escape(str(exc))}</code>\n\n"
            "Ничего не сохранено — прежнее подключение осталось как было.",
            _kb(("🔁 Ввести заново", "conn:menu")),
        )
        return

    ctx.secrets.save()
    await state.clear()
    await show(
        event,
        "\n".join(
            [
                "✅ <b>Облако подключено</b>",
                "",
                f"Проект: <code>{escape(info.project_name or info.project_id)}</code>",
                f"Пользователь: <code>{escape(info.username)}</code>",
                f"Регион: <code>{escape(info.region)}</code>",
                f"Сеть <code>{escape(network)}</code>: найдена",
                "",
                f"Доступ: {escape(credentials.describe())}",
                "Сохранено в файл с правами 0600.",
            ]
        ),
        back_button(),
    )


async def _ask_region(event: Event, ctx: AppContext, state: FSMContext) -> None:
    data = await state.get_data()
    region = data["cred"].get("region") or "RegionOne"
    await state.set_state(Connect.region)
    await show(
        event,
        f"Регион проекта. Обычно <code>{escape(region)}</code> — "
        "если он и нужен, нажмите кнопку.",
        _kb((f"✅ {region}", "conn:region_default")),
    )


# --- точки входа --------------------------------------------------------


async def cmd_connect(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await connect_intro(message, ctx)


async def cb_connect(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await connect_intro(callback, ctx)


async def cb_cancel(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await show(callback, "Подключение не менялось.", back_button())


# --- ветка: openrc ------------------------------------------------------


async def cb_openrc(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.set_state(Connect.openrc)
    await state.update_data(cred={})
    await show(
        callback,
        "Пришлите содержимое openrc-файла одним сообщением.\n\n"
        "На машине с файлом: <code>cat /root/mcs*-openrc.sh</code> — "
        "скопируйте вывод целиком и отправьте сюда.\n\n"
        "<i>Пароля в файле обычно нет, его спрошу отдельно.</i>",
        _kb(),
    )


async def on_openrc(message: Message, ctx: AppContext, state: FSMContext) -> None:
    values = parse_openrc(message.text or "")
    if not values.get("OS_AUTH_URL"):
        await message.answer(
            "В присланном тексте нет <code>OS_AUTH_URL</code>. "
            "Проверьте, что скопировали файл целиком."
        )
        return

    credentials = CloudCredentials(
        method=METHOD_PASSWORD,
        auth_url=values.get("OS_AUTH_URL", ""),
        region=values.get("OS_REGION_NAME", "RegionOne"),
        interface=values.get("OS_INTERFACE", "public"),
        username=values.get("OS_USERNAME", ""),
        password=values.get("OS_PASSWORD", ""),
        project_id=values.get("OS_PROJECT_ID", ""),
        project_name=values.get("OS_PROJECT_NAME", ""),
        user_domain_name=values.get("OS_USER_DOMAIN_NAME", "users"),
    )
    await state.update_data(cred=credentials.__dict__)

    summary = (
        f"Разобрал: пользователь <code>{escape(credentials.username or '—')}</code>, "
        f"проект <code>{escape(credentials.project_id or credentials.project_name or '—')}</code>, "
        f"регион <code>{escape(credentials.region)}</code>."
    )
    if credentials.password:
        await message.answer(summary)
        await _finish(message, ctx, state)
        return

    await state.set_state(Connect.password)
    await message.answer(
        summary + "\n\nТеперь пришлите пароль от VK Cloud. "
        "Сообщение с ним я удалю сразу после прочтения."
    )


# --- ветка: логин и пароль ---------------------------------------------


async def cb_password(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.update_data(cred={"method": METHOD_PASSWORD})
    await state.set_state(Connect.auth_url)
    await show(
        callback,
        "Адрес Keystone (<code>OS_AUTH_URL</code> из openrc-файла).",
        _kb((f"✅ {VK_AUTH_URL}", "conn:auth_default")),
    )


async def cb_auth_default(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await _store(state, auth_url=VK_AUTH_URL)
    await _after_auth_url(callback, ctx, state)


async def on_auth_url(message: Message, ctx: AppContext, state: FSMContext) -> None:
    url = (message.text or "").strip()
    if not url.startswith("http"):
        await message.answer("Нужен адрес вида <code>https://…/v3/</code>.")
        return
    await _store(state, auth_url=url)
    await _after_auth_url(message, ctx, state)


async def _after_auth_url(event: Event, ctx: AppContext, state: FSMContext) -> None:
    data = await state.get_data()
    if data["cred"].get("method") == METHOD_APPCRED:
        await state.set_state(Connect.appcred_id)
        await show(
            event,
            "Пришлите <b>id</b> application credential.\n\n"
            "Если его ещё нет:\n"
            "<code>openstack application credential create vkiphunter</code>",
            _kb(),
        )
        return
    await state.set_state(Connect.username)
    await show(event, "Логин VK Cloud (<code>OS_USERNAME</code>), обычно почта.", _kb())


async def on_username(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await _store(state, username=(message.text or "").strip())
    await state.set_state(Connect.password)
    await message.answer(
        "Пароль от VK Cloud. Сообщение с ним удалю сразу после прочтения."
    )


async def on_password(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await _store(state, password=(message.text or "").strip())
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 - без прав на удаление это не критично
        pass

    data = await state.get_data()
    if data["cred"].get("project_id") or data["cred"].get("project_name"):
        await _finish(message, ctx, state)
        return

    await state.set_state(Connect.project)
    await message.answer(
        "🔒 Пароль принят, сообщение удалено.\n\n"
        "Теперь ID проекта (<code>OS_PROJECT_ID</code>). "
        "Если проект в аккаунте один — можно пропустить.",
        reply_markup=_kb(("⏭ Пропустить", "conn:project_skip")),
    )


async def on_project(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await _store(state, project_id=(message.text or "").strip())
    await _finish(message, ctx, state)


async def cb_project_skip(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await _finish(callback, ctx, state)


# --- ветка: application credential -------------------------------------


async def cb_appcred(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.update_data(cred={"method": METHOD_APPCRED})
    await state.set_state(Connect.auth_url)
    await show(
        callback,
        "Адрес Keystone (<code>OS_AUTH_URL</code> из openrc-файла).",
        _kb((f"✅ {VK_AUTH_URL}", "conn:auth_default")),
    )


async def on_appcred_id(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await _store(state, app_cred_id=(message.text or "").strip())
    await state.set_state(Connect.appcred_secret)
    await message.answer("Теперь <b>secret</b>. Сообщение с ним удалю после прочтения.")


async def on_appcred_secret(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await _store(state, app_cred_secret=(message.text or "").strip())
    try:
        await message.delete()
    except Exception:  # noqa: BLE001
        pass
    await _ask_region(message, ctx, state)


async def on_region(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await _store(state, region=(message.text or "").strip())
    await _finish(message, ctx, state)


async def cb_region_default(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await _finish(callback, ctx, state)


# --- симуляция и отключение --------------------------------------------


async def cb_simulate(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.update_data(cred={"method": METHOD_SIMULATE})
    await _finish(callback, ctx, state)


async def cmd_disconnect(message: Message, ctx: AppContext) -> None:
    await show(
        message,
        "Забыть сохранённые доступы к облаку?\n\n"
        "Найденные порты и настройки останутся, но охота работать не будет, "
        "пока не пройдёте /connect заново.",
        confirm("conn:forget"),
    )


async def cb_forget(callback: CallbackQuery, ctx: AppContext) -> None:
    ctx.secrets.secrets.cloud = CloudCredentials()
    ctx.secrets.save()
    await ctx.cloud.reset()
    await show(callback, "Доступы удалены. Подключить заново: /connect", back_button())


async def _store(state: FSMContext, **values: str) -> None:
    data = await state.get_data()
    cred = dict(data.get("cred") or {})
    cred.update(values)
    await state.update_data(cred=cred)


def build_router():
    from aiogram import Router

    router = Router(name=router_name)
    router.message.register(cmd_connect, Command("connect"))
    router.message.register(cmd_disconnect, Command("disconnect"))
    router.callback_query.register(cb_connect, F.data == "conn:menu")
    router.callback_query.register(cb_cancel, F.data == "conn:cancel")
    router.callback_query.register(cb_openrc, F.data == "conn:openrc")
    router.callback_query.register(cb_password, F.data == "conn:password")
    router.callback_query.register(cb_appcred, F.data == "conn:appcred")
    router.callback_query.register(cb_simulate, F.data == "conn:simulate")
    router.callback_query.register(cb_auth_default, F.data == "conn:auth_default")
    router.callback_query.register(cb_project_skip, F.data == "conn:project_skip")
    router.callback_query.register(cb_region_default, F.data == "conn:region_default")
    router.callback_query.register(cb_forget, F.data == "conn:forget")
    router.message.register(on_openrc, Connect.openrc)
    router.message.register(on_auth_url, Connect.auth_url)
    router.message.register(on_username, Connect.username)
    router.message.register(on_password, Connect.password)
    router.message.register(on_project, Connect.project)
    router.message.register(on_appcred_id, Connect.appcred_id)
    router.message.register(on_appcred_secret, Connect.appcred_secret)
    router.message.register(on_region, Connect.region)
    return router

