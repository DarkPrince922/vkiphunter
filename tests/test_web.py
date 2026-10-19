"""HTTP integration tests: real sessions and engine, scenario cloud only."""
import asyncio
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from vkiphunter.cloud.base import CloudError, FloatingIpInfo, PortInfo, ServerBrief
from vkiphunter.cloud.fake import FakeCloud
from vkiphunter.config import Settings
from vkiphunter.secrets import CloudCredentials
from vkiphunter.state import FoundPort
from vkiphunter.web.app import COOKIE, RUNTIME, create_app

PASSWORD = "a-strong-test-password-12345"


@pytest.fixture
async def panel(tmp_path, aiohttp_client):
    cfg = Settings(_env_file=None, WEB_PASSWORD=PASSWORD,
                   STATE_FILE=tmp_path / 'state.json', CREDENTIALS_FILE=tmp_path / 'creds.json',
                   LOG_FILE=tmp_path / 'test.log')
    app = create_app(cfg)
    rt = app[RUNTIME]
    rt.secrets.secrets.cloud = CloudCredentials(method='simulate')
    rt.secrets.save()
    rt.cloud._backend = FakeCloud(latency=0, seed=1, pool_empty_chance=0)
    client = await aiohttp_client(app)
    return client, rt


async def authenticate(client):
    result = await client.post('/api/login', json={'password': PASSWORD}, headers={'X-VKH-Request': '1'})
    assert result.status == 200
    assert result.cookies[COOKIE]['httponly']
    assert result.cookies[COOKIE]['samesite'] == 'Strict'
    csrf = (await result.json())['csrf']
    return {'X-VKH-Request': '1', 'X-CSRF-Token': csrf}


async def test_auth_csrf_and_logout(panel):
    client, rt = panel
    assert (await client.get('/api/state')).status == 401
    assert (await client.get('/healthz')).status == 200
    assert (await client.post('/api/login', json={'password': PASSWORD})).status == 403
    headers = await authenticate(client)
    assert (await client.post('/api/hunt/start', json={}, headers={'X-VKH-Request': '1'})).status == 403
    bad = dict(headers, Origin='https://evil.example')
    assert (await client.post('/api/hunt/start', json={}, headers=bad)).status == 403
    state = await client.get('/api/state')
    assert state.headers['X-Frame-Options'] == 'DENY'
    assert state.headers['Cache-Control'] == 'no-store'
    assert (await client.post('/api/logout', json={}, headers=headers)).status == 200
    assert (await client.get('/api/state')).status == 401
    assert not rt.sessions.records


async def test_rate_limit_and_expired_session(panel):
    client, rt = panel
    for _ in range(10):
        response = await client.post('/api/login', json={'password': 'wrong'}, headers={'X-VKH-Request': '1'})
        assert response.status == 401
    assert (await client.post('/api/login', json={'password': PASSWORD}, headers={'X-VKH-Request': '1'})).status == 429
    rt.failures.clear()
    await authenticate(client)
    session = next(iter(rt.sessions.records.values()))
    session.expires = 0
    assert (await client.get('/api/state')).status == 401


async def test_settings_validate_persist_and_clear_stale_subnets(panel):
    client, rt = panel
    headers = await authenticate(client)
    data = asdict(rt.store.state.settings)
    for update in ({'prefix': ''}, {'workers': 100}, {'targets': ['bad']}, {'targets': ['::/0']}, {'interval': '1'}):
        response = await client.post('/api/settings', json=dict(data, **update), headers=headers)
        assert response.status == 400
    data.update(interval=1, subnets=[{'id': 'old', 'cidr': '1.2.3.0/24'}])
    assert (await client.post('/api/settings', json=data, headers=headers)).status == 200
    assert rt.store.state.settings.subnet_ids == ['old']
    data['network'] = 'another-network'
    assert (await client.post('/api/settings', json=data, headers=headers)).status == 200
    assert not rt.store.state.settings.subnets
    assert 'another-network' in rt.settings.state_file.read_text()


async def test_hunt_match_and_cleanup_preserve_resources(panel):
    client, rt = panel
    headers = await authenticate(client)
    rt.store.state.settings.targets = ['0.0.0.0/0']
    assert (await client.post('/api/hunt/start', json={}, headers=headers)).status == 200
    for _ in range(100):
        if rt.store.state.found and not rt.hunter.running:
            break
        await asyncio.sleep(.01)
    assert rt.store.state.found
    cloud = rt.cloud._backend
    prefix = rt.store.state.settings.prefix
    cloud._ports['trial'] = PortInfo('trial', prefix+'-trial', '1.2.3.4')
    cloud._ports['attached'] = PortInfo('attached', prefix+'-busy', '1.2.3.5', device_id='vm')
    cloud._ports['foreign'] = PortInfo('foreign', prefix+'someone-else', '1.2.3.6')
    cloud._ports['other'] = PortInfo('other', 'someone-else', '1.2.3.7')
    assert (await client.post('/api/cleanup', json={}, headers=headers)).status == 400
    response = await client.post('/api/cleanup', json={'confirm': True}, headers=headers)
    assert (await response.json())['deleted'] == ['trial']
    assert rt.store.state.found[0].port_id in cloud._ports
    assert {'attached', 'foreign', 'other'} <= cloud._ports.keys()
    assert rt.settings.state_file.exists()


async def test_running_blocks_reconfiguration_and_can_stop(panel):
    client, rt = panel
    headers = await authenticate(client)
    rt.store.state.settings.targets = ['192.0.2.0/24']
    await client.post('/api/hunt/start', json={}, headers=headers)
    assert (await client.post('/api/settings', json=asdict(rt.store.state.settings), headers=headers)).status == 409
    assert (await client.post('/api/connect', json={'method':'simulate'}, headers=headers)).status == 409
    assert (await client.post('/api/cleanup', json={'confirm': True}, headers=headers)).status == 409
    assert (await client.post('/api/hunt/stop', json={}, headers=headers)).status == 200
    await rt.hunter.stop(wait=True)
    assert not rt.hunter.running


async def test_failed_connect_keeps_credentials_and_hides_secrets(panel, monkeypatch):
    client, rt = panel
    headers = await authenticate(client)
    previous = rt.secrets.secrets.cloud
    backend = FakeCloud(latency=0)
    backend.check = AsyncMock(side_effect=CloudError('secret-value'))
    backend.close = AsyncMock()
    monkeypatch.setattr('vkiphunter.web.app.build_backend', lambda _: backend)
    result = await client.post('/api/connect', json={'method':'password','auth_url':'https://cloud.example/v3',
                    'username':'alice','password':'secret-value'}, headers=headers)
    assert result.status == 400 and 'secret-value' not in await result.text()
    assert rt.secrets.secrets.cloud is previous
    backend.close.assert_awaited_once()
    invalid = await client.post('/api/login', json={'password':'secret-value','extra':'secret-value'}, headers={'X-VKH-Request':'1'})
    assert 'secret-value' not in await invalid.text()


async def test_openrc_connection_and_secret_permissions(panel, monkeypatch):
    client, rt = panel
    headers = await authenticate(client)
    captured = []
    def build(credentials):
        captured.append(credentials)
        return FakeCloud(latency=0)
    monkeypatch.setattr('vkiphunter.web.app.build_backend', build)
    result = await client.post('/api/connect', json={'method':'appcred','openrc':
        'export OS_AUTH_URL="https://cloud.example/v3"\nexport OS_APPLICATION_CREDENTIAL_ID=abc123\n'
        'export OS_APPLICATION_CREDENTIAL_SECRET="a-secret"\nexport OS_REGION_NAME=region-two'}, headers=headers)
    assert result.status == 200
    assert captured[0].region == 'region-two'
    assert rt.settings.credentials_file.stat().st_mode & 0o777 == 0o600
    body = await (await client.get('/api/state')).text()
    assert 'a-secret' not in body
    assert 'password' not in (await (await client.get('/api/state')).json())


async def test_floating_cleanup_attach_and_duplicate_protection(panel):
    client, rt = panel
    headers = await authenticate(client)
    cloud = rt.cloud._backend
    prefix = rt.store.state.settings.prefix
    rt.store.state.settings.mode = 'floatingip'
    cloud._fips['found'] = FloatingIpInfo('found', '1.2.3.4', prefix+'-found')
    cloud._fips['trial'] = FloatingIpInfo('trial', '1.2.3.5', prefix+'-trial')
    cloud._fips['busy'] = FloatingIpInfo('busy', '1.2.3.6', prefix+'-busy', port_id='existing')
    rt.store.state.found.append(FoundPort('found', '1.2.3.4', prefix+'-found', 'internet', '0.0.0.0/0', kind='floatingip'))
    cloud._servers['vm'] = ServerBrief('vm', 'test-vm', 'ACTIVE')
    response = await client.post('/api/cleanup', json={'confirm':True}, headers=headers)
    assert (await response.json())['deleted'] == ['trial']
    body = {'confirm': True, 'port_id':'found', 'server_id':'vm'}
    assert (await client.post('/api/attach', json=body, headers=headers)).status == 200
    assert rt.store.state.found[0].server_id == 'vm'
    assert (await client.post('/api/attach', json=body, headers=headers)).status == 409
    assert 'busy' in cloud._fips


async def test_vm_creation_and_mutation_lock(panel):
    client, rt = panel
    headers = await authenticate(client)
    cloud = rt.cloud._backend
    cloud._ports['found'] = PortInfo('found', 'probe-found', '1.2.3.4')
    rt.store.state.found.append(FoundPort('found', '1.2.3.4', 'probe-found', 'internet', '0.0.0.0/0'))
    body = {'confirm':True,'port_id':'found','name':'web-vm','flavor_id':'flavor','image_id':'image'}
    async with rt.mutation:
        assert (await client.post('/api/vm', json=body, headers=headers)).status == 409
        assert (await client.get('/api/state')).status == 200
    result = await client.post('/api/vm', json=body, headers=headers)
    assert result.status == 200
    assert rt.store.state.found[0].server_name == 'web-vm'
    assert (await client.post('/api/vm', json=body, headers=headers)).status == 409


async def test_vm_failure_keeps_found_resource(panel):
    client, rt = panel
    headers = await authenticate(client)
    cloud = rt.cloud._backend
    cloud._ports['found'] = PortInfo('found', 'probe-found', '1.2.3.4')
    rt.store.state.found.append(FoundPort('found', '1.2.3.4', 'probe-found', 'internet', '0.0.0.0/0'))
    cloud.create_server = AsyncMock(side_effect=CloudError('timeout'))
    result = await client.post('/api/vm', json={'confirm':True,'port_id':'found','name':'web-vm',
                         'flavor_id':'flavor','image_id':'image'}, headers=headers)
    assert result.status == 502
    assert 'found' in cloud._ports and rt.store.state.found[0].server_id is None


async def test_read_apis_and_asset_allowlist(panel):
    client, _ = panel
    await authenticate(client)
    for resource in ('check','networks','subnets','availability','ports','floatingips','servers','flavors','images','keypairs'):
        assert (await client.get('/api/cloud/'+resource)).status == 200
    assert (await client.get('/api/cloud/unknown')).status == 404
    for path in ('/', '/static/app.js', '/static/style.css'):
        assert (await client.get(path)).status == 200
    assert (await client.get('/static/credentials.json')).status == 404


def test_password_required():
    with pytest.raises(ValueError, match='WEB_PASSWORD'):
        create_app(Settings(_env_file=None, WEB_PASSWORD='short'))


async def test_state_redacts_secrets_before_json_encoding(panel):
    client, rt = panel
    await authenticate(client)
    secret = 'quoted"secret\\value'
    rt.secrets.secrets.cloud = CloudCredentials(method='password', auth_url='https://example.test',
                                                username='alice', password=secret)
    rt.hunter.last_error = f'API rejected {secret}'
    result = await client.get('/api/state')
    data = await result.json()
    assert secret not in data['last_error']
    assert '[скрыто]' in data['last_error']


async def test_native_post_login_sets_session_without_password_in_url(panel):
    client, rt = panel
    origin = str(client.make_url('/')).rstrip('/')
    # Native form submission must work even while a cloud operation is busy.
    async with rt.mutation:
        result = await client.post('/login', data={'password': PASSWORD},
                                   headers={'Origin': origin}, allow_redirects=False)
    assert result.status == 303
    assert result.headers['Location'] == '/'
    assert PASSWORD not in result.headers['Location']
    assert result.cookies[COOKIE]['httponly']
    state = await client.get('/api/state')
    assert state.status == 200
    assert (await state.json())['csrf']


async def test_native_login_rejects_cross_site_and_missing_origin(panel):
    client, _ = panel
    for headers in ({}, {'Origin': 'https://evil.example'}):
        result = await client.post('/login', data={'password': PASSWORD}, headers=headers)
        assert result.status == 403
    assert (await client.get('/api/state')).status == 401


async def test_native_login_errors_are_visible_and_share_rate_limit(panel):
    client, _ = panel
    origin = str(client.make_url('/')).rstrip('/')
    for _ in range(9):
        response = await client.post('/api/login', json={'password': 'wrong'}, headers={'X-VKH-Request':'1'})
        assert response.status == 401
    response = await client.post('/login', data={'password': 'private-wrong-password'},
                                 headers={'Origin': origin})
    assert response.status == 401 and response.content_type == 'text/html'
    html = await response.text()
    assert 'Неверный пароль' in html and 'private-wrong-password' not in html
    result = await client.post('/login', data={'password': PASSWORD}, headers={'Origin': origin})
    assert result.status == 429
    assert 'Подождите минуту' in await result.text()


async def test_old_password_query_redirects_without_logging_in(panel):
    client, _ = panel
    response = await client.get('/', params={'password': PASSWORD}, allow_redirects=False)
    assert response.status == 303 and response.headers['Location'] == '/'
    assert PASSWORD not in await response.text()
    assert (await client.get('/api/state')).status == 401


@pytest.mark.parametrize('host,origin,expected', [
    ('panel.example:80', 'http://panel.example', 200),
    ('panel.example', 'http://panel.example:80', 200),
    ('panel.example:18080', 'http://panel.example:18080', 200),
    ('panel.example:18080', 'http://panel.example', 403),
    ('panel.example', 'https://panel.example', 200),
    ('panel.example', 'null', 403),
    ('panel.example', 'https://evil.example', 403),
])
async def test_login_origin_ports(panel, host, origin, expected):
    client, _ = panel
    result = await client.post('/api/login', json={'password': PASSWORD}, headers={
        'X-VKH-Request': '1', 'Origin': origin, 'Host': host,
    })
    assert result.status == expected


async def test_session_cookie_last_seven_days_and_renews(panel):
    import time
    client, rt = panel
    headers = await authenticate(client)
    session = next(iter(rt.sessions.records.values()))
    assert session.expires > time.time() + 6 * 86400
    session.expires -= 7200
    old_expiry = session.expires
    response = await client.get('/api/state')
    assert response.status == 200
    assert int(response.cookies[COOKIE]['max-age']) > 6 * 86400
    assert response.cookies[COOKIE]['httponly']
    assert session.expires > old_expiry
    assert (await client.post('/api/hunt/stop', json={}, headers=headers)).status == 200


async def test_session_and_csrf_survive_restart_but_logout_stays_revoked(panel, aiohttp_client):
    client, rt = panel
    headers = await authenticate(client)
    token = client.session.cookie_jar.filter_cookies(client.make_url('/'))[COOKIE].value
    await client.close()
    restarted = await aiohttp_client(create_app(rt.settings))
    restarted.session.cookie_jar.update_cookies({COOKIE: token}, response_url=restarted.make_url('/'))
    response = await restarted.get('/api/state')
    assert response.status == 200
    assert (await response.json())['csrf'] == headers['X-CSRF-Token']
    assert (await restarted.post('/api/logout', json={}, headers=headers)).status == 200
    await restarted.close()
    after_logout = await aiohttp_client(create_app(rt.settings))
    after_logout.session.cookie_jar.update_cookies({COOKIE: token}, response_url=after_logout.make_url('/'))
    assert (await after_logout.get('/api/state')).status == 401


async def test_password_change_invalidates_persisted_session(panel, aiohttp_client):
    client, rt = panel
    await authenticate(client)
    token = client.session.cookie_jar.filter_cookies(client.make_url('/'))[COOKIE].value
    await client.close()
    settings = rt.settings.model_copy(update={'web_password': 'new-long-test-password-123'})
    restarted = await aiohttp_client(create_app(settings))
    restarted.session.cookie_jar.update_cookies({COOKIE: token}, response_url=restarted.make_url('/'))
    assert (await restarted.get('/api/state')).status == 401


@pytest.mark.parametrize('kind', ['port', 'floatingip'])
async def test_delete_found_ip_removes_resource_and_record_but_preserves_history(panel, kind):
    client, rt = panel
    headers = await authenticate(client)
    cloud = rt.cloud._backend
    found = FoundPort('found-delete', '1.2.3.4', 'an-old-prefix-found', 'internet', '1.2.3.0/24', kind=kind)
    rt.store.state.found.append(found)
    rt.store.state.stats.matches = 1
    if kind == 'port':
        cloud._ports[found.port_id] = PortInfo(found.port_id, found.name, found.ip)
    else:
        cloud._fips[found.port_id] = FloatingIpInfo(found.port_id, found.ip, found.name)
    body = {'resource_id':found.port_id,'kind':kind,'ip':found.ip,'confirm':True}
    assert (await client.post('/api/ip/delete', json=body, headers=headers)).status == 200
    assert not rt.store.state.found and rt.store.state.stats.matches == 1
    assert found.port_id not in cloud._ports and found.port_id not in cloud._fips
    assert (await (await client.get('/api/state')).json())['found'] == []


@pytest.mark.parametrize('kind', ['port', 'floatingip'])
async def test_delete_refuses_attached_foreign_or_changed_ip_and_requires_confirmation(panel, kind):
    client, rt = panel
    headers = await authenticate(client)
    cloud = rt.cloud._backend
    prefix = rt.store.state.settings.prefix
    if kind == 'port':
        cloud._ports['busy'] = PortInfo('busy',prefix+'-busy','1.2.3.4',device_id='vm')
        cloud._ports['foreign'] = PortInfo('foreign','someone-else','1.2.3.5')
        cloud._ports['changed'] = PortInfo('changed',prefix+'-trial','1.2.3.6')
    else:
        cloud._fips['busy'] = FloatingIpInfo('busy','1.2.3.4',prefix+'-busy',port_id='vm')
        cloud._fips['foreign'] = FloatingIpInfo('foreign','1.2.3.5','someone-else')
        cloud._fips['changed'] = FloatingIpInfo('changed','1.2.3.6',prefix+'-trial')
    rt.store.state.found.append(FoundPort('busy','1.2.3.4',prefix+'-busy','internet','1.2.3.0/24',kind=kind))
    body = {'resource_id':'busy','kind':kind,'ip':'1.2.3.4','confirm':True}
    assert (await client.post('/api/ip/delete', json=body, headers=headers)).status == 409
    assert (await client.post('/api/ip/delete', json=dict(body,resource_id='foreign',ip='1.2.3.5'), headers=headers)).status == 403
    assert (await client.post('/api/ip/delete', json=dict(body,resource_id='changed',ip='1.2.3.7'), headers=headers)).status == 409
    assert (await client.post('/api/ip/delete', json={k:v for k,v in body.items() if k!='confirm'}, headers=headers)).status == 400
    assert len(cloud._ports if kind=='port' else cloud._fips) == 3
    assert rt.store.state.found


async def test_delete_own_probe_and_already_absent_found_ip(panel):
    client, rt = panel
    headers = await authenticate(client)
    cloud = rt.cloud._backend
    prefix = rt.store.state.settings.prefix
    cloud._fips['trial'] = FloatingIpInfo('trial','1.2.3.4',prefix+'-trial')
    body = {'resource_id':'trial','kind':'floatingip','ip':'1.2.3.4','confirm':True}
    assert (await client.post('/api/ip/delete', json=body, headers=headers)).status == 200
    rt.store.state.found.append(FoundPort('missing','1.2.3.5','old-prefix','internet','1.2.3.0/24',kind='floatingip'))
    response = await client.post('/api/ip/delete', json=dict(body,resource_id='missing',ip='1.2.3.5'), headers=headers)
    assert response.status == 200 and (await response.json())['already_absent']
    assert not rt.store.state.found


async def test_delete_cloud_error_keeps_found_ip_and_requires_csrf(panel):
    client, rt = panel
    headers = await authenticate(client)
    cloud = rt.cloud._backend
    cloud._fips['found'] = FloatingIpInfo('found','1.2.3.4','probe-found')
    rt.store.state.found.append(FoundPort('found','1.2.3.4','probe-found','internet','1.2.3.0/24',kind='floatingip'))
    body = {'resource_id':'found','kind':'floatingip','ip':'1.2.3.4','confirm':True}
    assert (await client.post('/api/ip/delete', json=body, headers={'X-VKH-Request':'1'})).status == 403
    cloud.delete_floating_ip = AsyncMock(side_effect=CloudError('cloud denied deletion'))
    assert (await client.post('/api/ip/delete', json=body, headers=headers)).status == 502
    assert rt.store.state.found and 'found' in cloud._fips


async def test_delete_is_blocked_while_hunting(panel):
    client, rt = panel
    headers = await authenticate(client)
    rt.store.state.settings.targets = ['192.0.2.0/24']
    await client.post('/api/hunt/start', json={}, headers=headers)
    response = await client.post('/api/ip/delete', json={
        'resource_id':'trial','kind':'floatingip','ip':'1.2.3.4','confirm':True,
    }, headers=headers)
    assert response.status == 409
    await rt.hunter.stop(wait=True)
