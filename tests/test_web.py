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
    assert not rt.sessions


async def test_rate_limit_and_expired_session(panel):
    client, rt = panel
    for _ in range(10):
        response = await client.post('/api/login', json={'password': 'wrong'}, headers={'X-VKH-Request': '1'})
        assert response.status == 401
    assert (await client.post('/api/login', json={'password': PASSWORD}, headers={'X-VKH-Request': '1'})).status == 429
    rt.failures.clear()
    await authenticate(client)
    token = next(iter(rt.sessions))
    rt.sessions[token] = (0, 'expired')
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
