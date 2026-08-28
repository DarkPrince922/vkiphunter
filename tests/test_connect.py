"""Мастер подключения к облаку, владение ботом и пересборка клиента."""

import asyncio

import pytest
from aiogram.methods import DeleteMessage

from vkiphunter.bot.app import build
from vkiphunter.bot.handlers.connect import parse_openrc
from vkiphunter.cloud import CloudProxy, NotConnected
from vkiphunter.cloud.base import CloudError
from vkiphunter.cloud.fake import FakeCloud
from vkiphunter.config import Settings
from vkiphunter.secrets import METHOD_PASSWORD, METHOD_SIMULATE, CloudCredentials, SecretStore

from .mocked import STRANGER, USER, callback_update, make_bot, text_update

OPENRC = """#!/usr/bin/env bash
export OS_AUTH_URL=https://infra.mail.ru:35357/v3/
export OS_PROJECT_ID=8b2c1f9a
export OS_PROJECT_NAME="mcs6434280924"
export OS_USER_DOMAIN_NAME="users"
export OS_USERNAME="owner@example.com"
echo "Please enter your OpenStack Password: "
read -sr OS_PASSWORD_INPUT
export OS_PASSWORD=$OS_PASSWORD_INPUT
export OS_REGION_NAME="RegionOne"
"""


@pytest.fixture
def app(tmp_path):
    """Бот без владельца и без подключённого облака — как после установки."""
    # _env_file=None и явный SIMULATE: тесты не должны зависеть от .env
    # и переменных окружения разработчика.
    settings = Settings(
        _env_file=None,
        SIMULATE="0",
        BOT_TOKEN="42:TESTTOKENTESTTOKENTESTTOKENTESTTOKEN",
        ALLOWED_USER_IDS="",
        STATE_FILE=str(tmp_path / "state.json"),
        CREDENTIALS_FILE=str(tmp_path / "credentials.json"),
    )
    dp, ctx, notifier = build(settings)
    bot, session = make_bot()
    ctx.bot = bot
    notifier.bot = bot
    return dp, ctx, session, bot


def use_fake_cloud(monkeypatch, failing: bool = False):
    """Подменить сборку бэкенда, чтобы мастер не ходил в настоящее облако."""
    import vkiphunter.cloud as cloud_module

    class Failing(FakeCloud):
        async def check(self, network):
            raise CloudError("401 Unauthorized: неверный пароль")

    factory = Failing if failing else FakeCloud
    monkeypatch.setattr(
        cloud_module, "build_backend", lambda credentials: factory(latency=0, seed=1)
    )


# --- владение -----------------------------------------------------------


@pytest.mark.asyncio
async def test_first_start_claims_ownership(app):
    dp, ctx, session, bot = app
    assert ctx.admins == set()

    await dp.feed_update(bot, text_update("/start"))

    assert ctx.admins == {USER.id}
    assert "Вы владелец" in "\n".join(session.texts)
    # владелец сохранён на диск
    assert SecretStore(ctx.secrets.path).load().admins == [USER.id]


@pytest.mark.asyncio
async def test_second_user_denied_after_claim(app):
    dp, ctx, session, bot = app
    await dp.feed_update(bot, text_update("/start"))
    session.clear()

    await dp.feed_update(bot, text_update("/start", user=STRANGER))

    assert "Доступ запрещён" in session.last_text
    assert ctx.admins == {USER.id}


@pytest.mark.asyncio
async def test_unclaimed_bot_answers_only_start(app):
    dp, ctx, session, bot = app
    await dp.feed_update(bot, text_update("/hunt"))
    assert "ещё не настроен" in session.last_text
    assert not ctx.hunter.running


# --- мастер подключения -------------------------------------------------


@pytest.mark.asyncio
async def test_menu_offers_connect_when_cloud_missing(app):
    dp, ctx, session, bot = app
    await dp.feed_update(bot, text_update("/start"))
    assert "Облако не подключено" in session.last_text


@pytest.mark.asyncio
async def test_connect_simulation_mode(app, monkeypatch):
    dp, ctx, session, bot = app
    use_fake_cloud(monkeypatch)
    await dp.feed_update(bot, text_update("/start"))

    await dp.feed_update(bot, callback_update("conn:simulate"))

    assert "Облако подключено" in session.last_text
    assert ctx.credentials.method == METHOD_SIMULATE
    assert SecretStore(ctx.secrets.path).load().cloud.simulate


@pytest.mark.asyncio
async def test_connect_via_openrc_paste(app, monkeypatch):
    dp, ctx, session, bot = app
    use_fake_cloud(monkeypatch)
    await dp.feed_update(bot, text_update("/start"))

    await dp.feed_update(bot, callback_update("conn:openrc"))
    await dp.feed_update(bot, text_update(OPENRC))
    assert "пришлите пароль" in session.last_text.lower()

    await dp.feed_update(bot, text_update("s3cret"))

    assert "Облако подключено" in session.last_text
    saved = SecretStore(ctx.secrets.path).load().cloud
    assert saved.method == METHOD_PASSWORD
    assert saved.username == "owner@example.com"
    assert saved.project_id == "8b2c1f9a"
    assert saved.password == "s3cret"
    # сообщение с паролем удалено из чата
    assert any(isinstance(m, DeleteMessage) for m in session.requests)


@pytest.mark.asyncio
async def test_bad_credentials_are_not_saved(app, monkeypatch):
    dp, ctx, session, bot = app
    use_fake_cloud(monkeypatch, failing=True)
    await dp.feed_update(bot, text_update("/start"))

    await dp.feed_update(bot, callback_update("conn:openrc"))
    await dp.feed_update(bot, text_update(OPENRC))
    await dp.feed_update(bot, text_update("wrong"))

    assert "Доступ не работает" in session.last_text
    assert not ctx.credentials.configured
    assert not SecretStore(ctx.secrets.path).load().cloud.configured


@pytest.mark.asyncio
async def test_disconnect_forgets_credentials(app, monkeypatch):
    dp, ctx, session, bot = app
    use_fake_cloud(monkeypatch)
    await dp.feed_update(bot, text_update("/start"))
    await dp.feed_update(bot, callback_update("conn:simulate"))
    assert ctx.credentials.configured

    await dp.feed_update(bot, callback_update("conn:forget"))

    assert not ctx.credentials.configured
    assert "Доступы удалены" in session.last_text


@pytest.mark.asyncio
async def test_openrc_without_auth_url_is_rejected(app, monkeypatch):
    dp, ctx, session, bot = app
    use_fake_cloud(monkeypatch)
    await dp.feed_update(bot, text_update("/start"))
    await dp.feed_update(bot, callback_update("conn:openrc"))

    await dp.feed_update(bot, text_update("совсем не тот файл"))

    assert "OS_AUTH_URL" in session.last_text
    assert not ctx.credentials.configured


# --- разбор openrc ------------------------------------------------------


def test_parse_openrc_extracts_values_and_skips_prompt():
    values = parse_openrc(OPENRC)
    assert values["OS_AUTH_URL"] == "https://infra.mail.ru:35357/v3/"
    assert values["OS_USERNAME"] == "owner@example.com"
    assert values["OS_PROJECT_NAME"] == "mcs6434280924"
    # пароль в файле подставляется из переменной — его брать нельзя
    assert "OS_PASSWORD" not in values


# --- прокси -------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_refuses_until_connected_then_rebuilds():
    credentials = CloudCredentials()
    proxy = CloudProxy(lambda: credentials)

    with pytest.raises(NotConnected):
        await proxy.check("internet")

    credentials.method = METHOD_SIMULATE
    first = await proxy.check("internet")
    assert first.network_name == "internet"

    backend = proxy._backend
    await proxy.reset()
    assert proxy._backend is None
    await proxy.check("internet")
    assert proxy._backend is not backend
