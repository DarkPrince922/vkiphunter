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
    assert "Удалено портов: <b>1</b>" in session.last_text


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
