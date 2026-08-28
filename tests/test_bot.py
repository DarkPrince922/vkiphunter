import asyncio

import pytest

from vkiphunter.bot.app import build
from vkiphunter.cloud.fake import FakeCloud
from vkiphunter.config import Settings
from vkiphunter.state import FoundPort

from .mocked import STRANGER, callback_update, make_bot, text_update


@pytest.fixture
def app(tmp_path, monkeypatch):
    # _env_file=None и явный SIMULATE: тесты не должны зависеть от .env
    # и переменных окружения разработчика.
    settings = Settings(
        _env_file=None,
        SIMULATE="1",
        BOT_TOKEN="42:TESTTOKENTESTTOKENTESTTOKENTESTTOKEN",
        ALLOWED_USER_IDS="7",
        STATE_FILE=str(tmp_path / "state.json"),
        LOG_FILE=str(tmp_path / "vkiphunter.log"),
    )
    dp, ctx, notifier = build(settings)
    bot, session = make_bot()
    ctx.bot = bot
    notifier.bot = bot
    # Симулятор без пауз и без случайных 409 — прокидываем внутрь прокси.
    ctx.cloud._backend = FakeCloud(latency=0, seed=1, pool_empty_chance=0)
    ctx.state.settings.interval = 0
    return dp, ctx, session, bot


async def feed(dp, bot, update):
    await dp.feed_update(bot, update)


@pytest.mark.asyncio
async def test_start_shows_help_and_menu(app):
    dp, ctx, session, bot = app
    await feed(dp, bot, text_update("/start"))
    joined = "\n".join(session.texts)
    assert "VK IP Hunter" in joined
    assert "Охота остановлена" in joined
    assert ctx.state.subscribers == [7]


@pytest.mark.asyncio
async def test_stranger_is_rejected(app):
    dp, ctx, session, bot = app
    await feed(dp, bot, text_update("/start", user=STRANGER))
    assert "Доступ запрещён" in session.last_text
    assert ctx.state.subscribers == []


@pytest.mark.asyncio
async def test_hunt_runs_and_stops(app):
    dp, ctx, session, bot = app
    ctx.state.settings.max_attempts = 3
    await feed(dp, bot, text_update("/hunt"))
    await asyncio.wait_for(ctx.hunter._task, timeout=10)

    assert ctx.state.stats.attempts == 3
    assert not ctx.hunter.running
    assert any("Охота запущена" in t for t in session.texts)


@pytest.mark.asyncio
async def test_stop_button_stops_hunt(app):
    dp, ctx, session, bot = app
    ctx.state.settings.interval = 30
    await feed(dp, bot, text_update("/hunt"))
    assert ctx.hunter.running
    await feed(dp, bot, callback_update("hunt:stop"))
    await asyncio.wait_for(_until_stopped(ctx), timeout=10)
    assert not ctx.hunter.running


async def _until_stopped(ctx):
    while ctx.hunter.running:
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_settings_edit_flow(app):
    dp, ctx, session, bot = app
    await feed(dp, bot, callback_update("set:interval"))
    session.clear()
    await feed(dp, bot, text_update("45"))
    assert ctx.state.settings.interval == 45
    assert "Сохранено" in "\n".join(session.texts)


@pytest.mark.asyncio
async def test_settings_edit_rejects_out_of_range(app):
    dp, ctx, session, bot = app
    await feed(dp, bot, callback_update("set:interval"))
    session.clear()
    await feed(dp, bot, text_update("1"))
    assert ctx.state.settings.interval == 0  # значение не изменилось
    assert "от 5 до 3600" in session.last_text


@pytest.mark.asyncio
async def test_targets_add_and_remove(app):
    dp, ctx, session, bot = app
    await feed(dp, bot, callback_update("tgt:add_target"))
    await feed(dp, bot, text_update("1.2.3.0/24"))
    assert "1.2.3.0/24" in ctx.state.settings.targets

    await feed(dp, bot, callback_update("tgt:del_target"))
    await feed(dp, bot, text_update("1.2.3.0/24"))
    assert "1.2.3.0/24" not in ctx.state.settings.targets


@pytest.mark.asyncio
async def test_targets_rejects_garbage(app):
    dp, ctx, session, bot = app
    await feed(dp, bot, callback_update("tgt:add_exclude"))
    session.clear()
    await feed(dp, bot, text_update("не-сеть"))
    assert "Не похоже на CIDR" in session.last_text
    assert ctx.state.settings.excludes == []


@pytest.mark.asyncio
async def test_hunt_refuses_without_targets(app):
    dp, ctx, session, bot = app
    ctx.state.settings.targets = []
    await feed(dp, bot, text_update("/hunt"))
    assert not ctx.hunter.running
    assert "пуст" in session.last_text


@pytest.mark.asyncio
async def test_vm_wizard_end_to_end(app):
    dp, ctx, session, bot = app
    ctx.state.found.append(
        FoundPort(
            port_id="port-1",
            ip="217.16.22.90",
            name="vk-direct-probe-1",
            network="internet",
            matched_cidr="217.16.16.0/21",
        )
    )
    await feed(dp, bot, callback_update("vm:start"))
    assert "Шаг 1/4" in session.last_text

    await feed(dp, bot, callback_update("vm:flavor:flv-1"))
    assert "Шаг 2/4" in session.last_text

    await feed(dp, bot, callback_update("vm:image:img-1"))
    assert "Шаг 3/4" in session.last_text

    await feed(dp, bot, callback_update("vm:key:sim-key"))
    assert "Шаг 4/4" in session.last_text

    await feed(dp, bot, callback_update("vm:create"))
    assert "ВМ создана" in session.last_text
    assert ctx.state.last_found().server_id


@pytest.mark.asyncio
async def test_vm_without_found_port(app):
    dp, ctx, session, bot = app
    await feed(dp, bot, text_update("/vm"))
    assert "нет найденного порта" in session.last_text


@pytest.mark.asyncio
async def test_cleanup_never_touches_foreign_or_found_ports(app):
    dp, ctx, session, bot = app
    cloud = ctx.cloud._backend
    probe = await cloud.create_port("internet", "vk-direct-probe-1", 10)
    keeper = await cloud.create_port("internet", "vk-direct-probe-2", 10)
    foreign = await cloud.create_port("internet", "production-lb", 10)
    ctx.state.found.append(
        FoundPort(
            port_id=keeper.id,
            ip=keeper.ip or "217.16.22.90",
            name=keeper.name,
            network="internet",
            matched_cidr="217.16.16.0/21",
        )
    )

    await feed(dp, bot, callback_update("ports:cleanup_yes"))

    remaining = {p.id for p in await cloud.list_ports("internet")}
    assert keeper.id in remaining
    assert foreign.id in remaining
    assert probe.id not in remaining
    assert "Освобождено: <b>1</b>" in session.last_text


@pytest.mark.asyncio
async def test_status_and_history_screens(app):
    dp, ctx, session, bot = app
    await feed(dp, bot, text_update("/status"))
    assert "Охота" in session.last_text
    await feed(dp, bot, text_update("/history"))
    assert "История пуста" in session.last_text
    await feed(dp, bot, text_update("/cloud"))
    assert "Облако доступно" in session.last_text
    await feed(dp, bot, text_update("/whoami"))
    assert "7" in session.last_text


# --- режим floating IP --------------------------------------------------


@pytest.mark.asyncio
async def test_mode_switch_persists(app):
    dp, ctx, session, bot = app
    assert ctx.state.settings.mode == "port"

    await feed(dp, bot, callback_update("set:mode"))
    assert ctx.state.settings.mode == "floatingip"
    assert "floating IP" in session.last_text

    await feed(dp, bot, callback_update("set:mode"))
    assert ctx.state.settings.mode == "port"


@pytest.mark.asyncio
async def test_floating_ip_cleanup_spares_foreign_and_attached(app):
    dp, ctx, session, bot = app
    ctx.state.settings.mode = "floatingip"
    cloud = ctx.cloud._backend

    mine = await cloud.create_floating_ip("internet", "vk-direct-probe-1", 10)
    keeper = await cloud.create_floating_ip("internet", "vk-direct-probe-2", 10)
    foreign = await cloud.create_floating_ip("internet", "production-fip", 10)
    attached = await cloud.create_floating_ip("internet", "vk-direct-probe-3", 10)
    await cloud.attach_floating_ip(attached.id, "sim-server")

    ctx.state.found.append(
        FoundPort(
            port_id=keeper.id,
            ip=keeper.ip or "217.16.22.90",
            name=keeper.description,
            network="internet",
            matched_cidr="217.16.16.0/21",
            kind="floatingip",
        )
    )

    await feed(dp, bot, callback_update("ports:cleanup_yes"))

    remaining = {f.id for f in await cloud.list_floating_ips()}
    assert keeper.id in remaining, "найденный адрес удалять нельзя"
    assert foreign.id in remaining, "чужой floating IP трогать нельзя"
    assert attached.id in remaining, "привязанный к ВМ удалять нельзя"
    assert mine.id not in remaining
    assert "Освобождено: <b>1</b>" in session.last_text


@pytest.mark.asyncio
async def test_attach_floating_ip_to_server(app):
    dp, ctx, session, bot = app
    ctx.state.settings.mode = "floatingip"
    cloud = ctx.cloud._backend
    fip = await cloud.create_floating_ip("internet", "vk-direct-probe-1", 10)
    ctx.state.found.append(
        FoundPort(
            port_id=fip.id,
            ip="217.16.22.90",
            name=fip.description,
            network="internet",
            matched_cidr="217.16.16.0/21",
            kind="floatingip",
        )
    )

    await feed(dp, bot, callback_update("fip:attach"))
    assert "К какой ВМ привязать" in session.last_text

    await feed(dp, bot, callback_update("fip:srv:sim-server"))
    assert "Floating IP привязан" in session.last_text
    assert ctx.state.last_found().server_name == "existing-vm"


@pytest.mark.asyncio
async def test_vm_wizard_refuses_floating_ip(app):
    dp, ctx, session, bot = app
    ctx.state.settings.mode = "floatingip"
    ctx.state.found.append(
        FoundPort(
            port_id="fip-1",
            ip="217.16.22.90",
            name="vk-direct-probe-1",
            network="internet",
            matched_cidr="217.16.16.0/21",
            kind="floatingip",
        )
    )
    await feed(dp, bot, text_update("/vm"))
    assert "/attach" in session.last_text


@pytest.mark.asyncio
async def test_attach_refuses_port_mode(app):
    dp, ctx, session, bot = app
    ctx.state.found.append(
        FoundPort(
            port_id="port-1",
            ip="217.16.22.90",
            name="vk-direct-probe-1",
            network="internet",
            matched_cidr="217.16.16.0/21",
        )
    )
    await feed(dp, bot, text_update("/attach"))
    assert "/vm" in session.last_text


# --- воркеры, аккаунт и логи --------------------------------------------


@pytest.mark.asyncio
async def test_start_message_names_mode_account_and_workers(app):
    dp, ctx, session, bot = app
    ctx.state.settings.workers = 4
    ctx.state.settings.max_attempts = 4

    await feed(dp, bot, text_update("/hunt"))
    await asyncio.wait_for(ctx.hunter._task, timeout=10)

    start = next(t for t in session.texts if "Охота запущена" in t)
    assert "Способ:" in start
    assert "simulated" in start, "в сообщении должен быть виден аккаунт"
    assert "Воркеров: <b>4</b>" in start


@pytest.mark.asyncio
async def test_status_shows_account_and_worker_numbers(app):
    dp, ctx, session, bot = app
    ctx.state.settings.workers = 3
    ctx.state.settings.max_attempts = 6

    await feed(dp, bot, text_update("/hunt"))
    await asyncio.wait_for(ctx.hunter._task, timeout=10)
    await feed(dp, bot, text_update("/status"))
    assert "Аккаунт:" in session.last_text

    await feed(dp, bot, text_update("/history"))
    assert "[в1]" in session.last_text or "[в2]" in session.last_text


@pytest.mark.asyncio
async def test_log_command_reads_file(app, tmp_path):
    dp, ctx, session, bot = app
    ctx.settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.settings.log_file.write_text(
        "2026-08-28 20:20:01 INFO vkiphunter.hunter: воркер 2 · mcs64 / owner · "
        "161.104.107.234 · вне целевых диапазонов\n",
        encoding="utf-8",
    )

    await feed(dp, bot, text_update("/log"))
    assert "воркер 2" in session.last_text
    assert "161.104.107.234" in session.last_text


@pytest.mark.asyncio
async def test_log_command_without_file(app):
    dp, ctx, session, bot = app
    await feed(dp, bot, text_update("/log"))
    assert "Лог пуст" in session.last_text


# --- выбор подсети ------------------------------------------------------


@pytest.mark.asyncio
async def test_subnets_screen_marks_matching_pools(app):
    dp, ctx, session, bot = app
    await feed(dp, bot, text_update("/subnets"))

    text = session.last_text
    assert "161.104.107.0/24" in text
    assert "217.16.16.0/20" in text
    assert "пересекается с целью" in text
    assert "Подходящих подсетей" in text


@pytest.mark.asyncio
async def test_subnets_toggle_and_reset(app):
    dp, ctx, session, bot = app
    await feed(dp, bot, callback_update("sub:tog:subnet-ext-3"))
    assert ctx.state.settings.subnet_ids == ["subnet-ext-3"]

    await feed(dp, bot, callback_update("sub:tog:subnet-ext-4"))
    assert ctx.state.settings.subnet_ids == ["subnet-ext-3", "subnet-ext-4"]

    # повторное нажатие снимает выбор
    await feed(dp, bot, callback_update("sub:tog:subnet-ext-3"))
    assert ctx.state.settings.subnet_ids == ["subnet-ext-4"]

    await feed(dp, bot, callback_update("sub:none"))
    assert ctx.state.settings.subnets == []


@pytest.mark.asyncio
async def test_select_all_matching_subnets(app):
    dp, ctx, session, bot = app
    ctx.state.settings.workers = 4

    await feed(dp, bot, callback_update("sub:all"))

    assert ctx.state.settings.subnet_cidrs == ["217.16.16.0/20", "5.188.140.0/22"]
    assert "каждый стучится в свою" in session.last_text


@pytest.mark.asyncio
async def test_warns_when_chosen_subnet_misses_targets(app):
    dp, ctx, session, bot = app
    from aiogram.methods import AnswerCallbackQuery

    await feed(dp, bot, callback_update("sub:tog:subnet-ext-1"))

    alerts = [
        m.text
        for m in session.requests
        if isinstance(m, AnswerCallbackQuery) and m.text
    ]
    assert any("не пересекается с целями" in a for a in alerts)


@pytest.mark.asyncio
async def test_warns_when_no_subnet_can_ever_match(app):
    dp, ctx, session, bot = app
    ctx.state.settings.targets = ["203.0.113.0/24"]
    await feed(dp, bot, text_update("/subnets"))
    assert "Ни одна подсеть не пересекается" in session.last_text


@pytest.mark.asyncio
async def test_chosen_subnet_reaches_the_cloud_from_the_bot(app):
    dp, ctx, session, bot = app
    ctx.state.settings.mode = "floatingip"
    ctx.state.settings.max_attempts = 6
    await feed(dp, bot, callback_update("sub:tog:subnet-ext-3"))

    await feed(dp, bot, text_update("/hunt"))
    await asyncio.wait_for(ctx.hunter._task, timeout=10)

    # подсеть 217.16.16.0/20 целиком внутри целей — попадание с первой попытки
    found = ctx.state.last_found()
    assert found is not None
    assert found.ip.startswith("217.16.")
