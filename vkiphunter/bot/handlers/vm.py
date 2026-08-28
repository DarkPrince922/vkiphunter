"""Мастер создания ВМ на найденном порту.

Порт с целевым адресом становится сетевым интерфейсом новой машины.
При любой ошибке создания порт сохраняется — иначе адрес уйдёт в пул.
"""

from __future__ import annotations

import time
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ...cloud import CloudError, FlavorInfo, ImageInfo
from ...state import MODE_FLOATING_IP
from ..context import AppContext
from ..keyboards import back_button, confirm, flavor_list, image_list, keypair_list
from ..ui import show


Event = Message | CallbackQuery

#: исторические параметры ВМ из контекста задачи
DEFAULT_VCPUS = 1
DEFAULT_RAM_MB = 2048
DEFAULT_IMAGE_TOKENS = ("ubuntu", "24.04")


class VM(StatesGroup):
    flavor = State()
    image = State()
    key = State()
    confirm = State()


def _default_flavors(flavors: list[FlavorInfo]) -> list[FlavorInfo]:
    picked = [
        f for f in flavors if f.vcpus == DEFAULT_VCPUS and f.ram == DEFAULT_RAM_MB
    ]
    return picked or flavors


def _default_images(images: list[ImageInfo]) -> list[ImageInfo]:
    picked = [
        i
        for i in images
        if all(token in i.name.lower() for token in DEFAULT_IMAGE_TOKENS)
    ]
    return picked or images


async def _start(event: Event, ctx: AppContext, state: FSMContext) -> None:
    found = ctx.state.last_found()
    if found is None:
        await show(
            event,
            "Пока нет найденного порта — сначала дождитесь целевого IP.",
            back_button(),
        )
        return
    if found.kind == MODE_FLOATING_IP:
        await show(
            event,
            "Найденный адрес — floating IP: его привязывают к существующей "
            "машине, а не делают интерфейсом новой. Это /attach.",
            back_button(),
        )
        return
    if found.server_id:
        await show(
            event,
            f"На порту <code>{escape(found.ip)}</code> уже создана ВМ "
            f"<code>{escape(found.server_name or found.server_id)}</code>.",
            back_button(),
        )
        return

    await state.clear()
    await state.update_data(port_id=found.port_id, ip=found.ip, show_all_flavors=False)
    await _ask_flavor(event, ctx, state, show_all=False)


async def _ask_flavor(
    event: Event, ctx: AppContext, state: FSMContext, show_all: bool
) -> None:
    try:
        flavors = await ctx.cloud.list_flavors()
    except CloudError as exc:
        await show(event, f"❌ Не получить флейворы:\n<code>{exc}</code>", back_button())
        return

    shown = flavors if show_all else _default_flavors(flavors)
    await state.set_state(VM.flavor)
    await state.update_data(show_all_flavors=show_all)
    data = await state.get_data()
    hint = "все флейворы" if show_all else f"{DEFAULT_VCPUS} vCPU / {DEFAULT_RAM_MB} МБ"
    await show(
        event,
        f"🖥 <b>Создание ВМ</b> на порту <code>{escape(data['ip'])}</code>\n\n"
        f"Шаг 1/4 — флейвор ({hint}):",
        flavor_list(shown, show_all),
    )


async def _ask_image(
    event: Event, ctx: AppContext, state: FSMContext, show_all: bool
) -> None:
    try:
        images = await ctx.cloud.list_images()
    except CloudError as exc:
        await show(event, f"❌ Не получить образы:\n<code>{exc}</code>", back_button())
        return

    shown = images if show_all else _default_images(images)
    await state.set_state(VM.image)
    await state.update_data(show_all_images=show_all)
    hint = "все активные" if show_all else "Ubuntu 24.04"
    await show(event, f"Шаг 2/4 — образ ({hint}):", image_list(shown, show_all))


async def _ask_key(event: Event, ctx: AppContext, state: FSMContext) -> None:
    try:
        keys = await ctx.cloud.list_keypairs()
    except CloudError as exc:
        await show(event, f"❌ Не получить ключи:\n<code>{exc}</code>", back_button())
        return

    await state.set_state(VM.key)
    await show(event, "Шаг 3/4 — SSH-ключ:", keypair_list(keys))


async def _ask_confirm(event: Event, ctx: AppContext, state: FSMContext) -> None:
    data = await state.get_data()
    settings = ctx.state.settings
    volume = f"{settings.volume_size} ГБ"
    if settings.volume_type:
        volume += f" ({escape(settings.volume_type)})"

    await state.set_state(VM.confirm)
    await show(
        event,
        "\n".join(
            [
                "Шаг 4/4 — проверьте параметры:",
                "",
                f"IP: <code>{escape(data['ip'])}</code>",
                f"Порт: <code>{escape(data['port_id'])}</code>",
                f"Флейвор: <code>{escape(data.get('flavor_name', ''))}</code>",
                f"Образ: <code>{escape(data.get('image_name', ''))}</code>",
                f"Ключ: <code>{escape(data.get('key_name') or 'без ключа')}</code>",
                f"Диск: <b>{volume}</b>",
                f"Имя ВМ: <code>{escape(data['server_name'])}</code>",
                "",
                f"Создание ждём до {settings.server_timeout} с.",
            ]
        ),
        confirm("vm:create", cancel="vm:cancel"),
    )


async def cmd_vm(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await _start(message, ctx, state)


async def cb_vm(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await callback.answer("Собираю ресурсы…")
    await _start(callback, ctx, state)


async def cb_flavor_mode(
    callback: CallbackQuery, ctx: AppContext, state: FSMContext
) -> None:
    show_all = (callback.data or "").endswith(":all")
    await _ask_flavor(callback, ctx, state, show_all=show_all)


async def cb_flavor(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    flavor_id = (callback.data or "").split(":", 2)[2]
    try:
        flavors = await ctx.cloud.list_flavors()
    except CloudError as exc:
        await show(callback, f"❌ <code>{exc}</code>", back_button())
        return
    flavor = next((f for f in flavors if f.id == flavor_id), None)
    if flavor is None:
        await callback.answer("Флейвор пропал из списка", show_alert=True)
        return

    await state.update_data(flavor_id=flavor.id, flavor_name=flavor.name)
    await _ask_image(callback, ctx, state, show_all=False)


async def cb_image_mode(
    callback: CallbackQuery, ctx: AppContext, state: FSMContext
) -> None:
    show_all = (callback.data or "").endswith(":all")
    await _ask_image(callback, ctx, state, show_all=show_all)


async def cb_image(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    image_id = (callback.data or "").split(":", 2)[2]
    try:
        images = await ctx.cloud.list_images()
    except CloudError as exc:
        await show(callback, f"❌ <code>{exc}</code>", back_button())
        return
    image = next((i for i in images if i.id == image_id), None)
    if image is None:
        await callback.answer("Образ пропал из списка", show_alert=True)
        return

    await state.update_data(image_id=image.id, image_name=image.name)
    await _ask_key(callback, ctx, state)


async def cb_key(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    key_name = (callback.data or "").split(":", 2)[2]
    data = await state.get_data()
    server_name = f"vk-direct-{data['ip'].replace('.', '-')}-{int(time.time()) % 10000}"
    await state.update_data(key_name=key_name, server_name=server_name)
    await _ask_confirm(callback, ctx, state)


async def cb_cancel(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await show(
        callback,
        "Создание ВМ отменено. Найденный порт остаётся нетронутым.",
        back_button(),
    )


async def cb_create(callback: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    data = await state.get_data()
    settings = ctx.state.settings
    await callback.answer("Создаю…")
    await show(
        callback,
        f"⏳ Создаю ВМ <code>{escape(data['server_name'])}</code>…\n"
        f"Это может занять до {settings.server_timeout} с.",
    )

    try:
        server = await ctx.cloud.create_server(
            name=data["server_name"],
            flavor_id=data["flavor_id"],
            image_id=data["image_id"],
            port_id=data["port_id"],
            key_name=data.get("key_name") or None,
            volume_size=settings.volume_size,
            volume_type=settings.volume_type,
            timeout=settings.server_timeout,
        )
    except CloudError as exc:
        await state.clear()
        await show(
            callback,
            f"❌ <b>ВМ не создана</b>\n<code>{escape(str(exc))}</code>\n\n"
            f"Порт <code>{escape(data['port_id'])}</code> сохранён — "
            "удалять его нельзя, иначе потеряете адрес.",
            back_button(),
        )
        return

    found = ctx.state.last_found()
    if found and found.port_id == data["port_id"]:
        found.server_id = server.id
        found.server_name = server.name
        await ctx.store.save()

    await state.clear()
    await show(
        callback,
        "\n".join(
            [
                "✅ <b>ВМ создана</b>",
                "",
                f"Имя: <code>{escape(server.name)}</code>",
                f"ID: <code>{escape(server.id)}</code>",
                f"Статус: <b>{escape(server.status)}</b>",
                f"IP: <code>{escape(data['ip'])}</code>",
                "",
                "Дальше: откройте нужный порт в security group и на самой ВМ, "
                "поднимите health-endpoint и проверьте адрес "
                "<b>через целевую SIM с выключенным Wi-Fi</b>.",
            ]
        ),
        back_button(),
    )


def build_router() -> Router:
    router = Router(name="vm")
    router.message.register(cmd_vm, Command("vm"))
    router.callback_query.register(cb_vm, F.data == "vm:start")
    router.callback_query.register(cb_flavor_mode, F.data.startswith("vm:flavors:"))
    router.callback_query.register(cb_flavor, F.data.startswith("vm:flavor:"))
    router.callback_query.register(cb_image_mode, F.data.startswith("vm:images:"))
    router.callback_query.register(cb_image, F.data.startswith("vm:image:"))
    router.callback_query.register(cb_key, F.data.startswith("vm:key:"))
    router.callback_query.register(cb_cancel, F.data == "vm:cancel")
    router.callback_query.register(cb_create, F.data == "vm:create", VM.confirm)
    return router
