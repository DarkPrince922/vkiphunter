"""Контракт с openstacksdk.

Бэкенд нельзя проверить на живом VK Cloud из тестов, поэтому здесь стоит
двойник, повторяющий форму объектов SDK 4.x. Обращение к атрибуту, которого
у настоящего Connection нет (например, к исчезнувшему ``auth_ref``),
роняет тест — именно на этом ловится расхождение с версией библиотеки.
"""

import keystoneauth1.exceptions as ke
import pytest

from vkiphunter.cloud.base import CloudAuthError, NotFound, PoolExhausted
from vkiphunter.cloud.openstack_backend import OpenStackBackend

AUTH = {
    "auth_url": "https://infra.mail.ru:35357/v3/",
    "region_name": "RegionOne",
    "auth_type": "v3password",
    "username": "owner@example.com",
    "password": "s3cret",
    "project_id": "d48dc24102414d67877a4bcbf58bf711",
}


class FakeAccess:
    project_id = "d48dc24102414d67877a4bcbf58bf711"
    project_name = "mcs6434280924"
    username = "owner@example.com"


class FakeAuth:
    def __init__(self, error=None):
        self.error = error

    def get_access(self, session):
        if self.error:
            raise self.error
        return FakeAccess()


class FakeSession:
    def __init__(self, error=None):
        self.auth = FakeAuth(error)


class FakeNetwork:
    id = "net-1"
    name = "internet"
    subnet_ids = ["subnet-1"]


class FakePort:
    def __init__(self, id="port-1", name="vk-direct-probe-1", ip="217.16.22.90"):
        self.id = id
        self.name = name
        self.fixed_ips = [{"ip_address": ip, "subnet_id": "subnet-1"}] if ip else []
        self.network_id = "net-1"
        self.status = "DOWN"
        self.device_id = ""
        self.created_at = "2026-08-28T19:56:00Z"


class FakeNetworkProxy:
    def __init__(self, create_error=None):
        self.create_error = create_error
        self.deleted = []
        self.ports_list = [FakePort()]
        self.fips = [FakeFloatingIp()]
        self.updated = None

    def find_network(self, name, ignore_missing=True):
        return FakeNetwork() if name == "internet" else None

    def create_port(self, network_id, name):
        if self.create_error:
            raise self.create_error
        return FakePort(name=name)

    def delete_port(self, port_id, ignore_missing=True):
        self.deleted.append(port_id)

    def find_port(self, name, ignore_missing=True):
        return FakePort(name=name)

    def ports(self, **query):
        device_id = query.get("device_id")
        if device_id:
            return iter([FakePort(id="srv-port", name="internal", ip="10.0.0.5")])
        return iter(self.ports_list)

    def create_ip(self, floating_network_id, description):
        if self.create_error:
            raise self.create_error
        fip = FakeFloatingIp(description=description)
        self.fips.append(fip)
        return fip

    def delete_ip(self, fip_id, ignore_missing=True):
        self.deleted.append(fip_id)

    def ips(self, **query):
        description = query.get("description")
        if description:
            return iter([f for f in self.fips if f.description == description])
        return iter(self.fips)

    def update_ip(self, fip_id, port_id=None, fixed_ip_address=None):
        self.updated = {"fip": fip_id, "port_id": port_id, "fixed_ip": fixed_ip_address}
        return FakeFloatingIp(id=fip_id, port_id=port_id)


class FakeFloatingIp:
    def __init__(self, id="fip-1", ip="217.16.22.90", description="vk-direct-probe-1",
                 port_id=""):
        self.id = id
        self.floating_ip_address = ip
        self.description = description
        self.status = "DOWN"
        self.port_id = port_id
        self.created_at = "2026-08-28T19:56:00Z"


class FakeFlavor:
    id, name, vcpus, ram, disk = "flv-1", "STD2-1-2", 1, 2048, 0


class FakeImage:
    id, name, status = "img-1", "Ubuntu-24.04-202405", "active"


class FakeServer:
    id, name, status = "srv-1", "vk-direct", "ACTIVE"
    addresses = {"internet": [{"addr": "217.16.22.90"}]}


class FakeComputeProxy:
    def __init__(self):
        self.created_with = None

    def flavors(self):
        return iter([FakeFlavor()])

    def servers(self):
        return iter([FakeServer()])

    def keypairs(self):
        return iter([type("K", (), {"name": "vk-key"})()])

    def create_server(self, **kwargs):
        self.created_with = kwargs
        return FakeServer()

    def wait_for_server(self, server, wait=None):
        return server


class FakeImageProxy:
    def images(self, status=None):
        return iter([FakeImage()])


class FakeConnection:
    def __init__(self, auth_error=None, create_error=None):
        self.session = FakeSession(auth_error)
        self.network = FakeNetworkProxy(create_error)
        self.compute = FakeComputeProxy()
        self.image = FakeImageProxy()

    def close(self):
        return None


def make(auth_error=None, create_error=None):
    backend = OpenStackBackend(AUTH)
    connection = FakeConnection(auth_error, create_error)
    backend._connect_sync = lambda: connection  # type: ignore[method-assign]
    return backend, connection


@pytest.mark.asyncio
async def test_check_reads_identity_from_session():
    backend, _ = make()
    info = await backend.check("internet")
    assert info.project_id == "d48dc24102414d67877a4bcbf58bf711"
    assert info.project_name == "mcs6434280924"
    assert info.username == "owner@example.com"
    assert info.region == "RegionOne"
    assert info.network_id == "net-1"
    assert info.subnets == ["subnet-1"]


@pytest.mark.asyncio
async def test_check_reports_missing_network():
    backend, _ = make()
    with pytest.raises(NotFound):
        await backend.check("internet-old")


@pytest.mark.asyncio
async def test_wrong_password_becomes_auth_error():
    backend, _ = make(auth_error=ke.Unauthorized())
    with pytest.raises(CloudAuthError):
        await backend.check("internet")


@pytest.mark.asyncio
async def test_unreachable_keystone_names_auth_url():
    backend, _ = make(auth_error=ke.ConnectFailure("нет маршрута"))
    with pytest.raises(Exception) as excinfo:
        await backend.check("internet")
    assert "OS_AUTH_URL" in str(excinfo.value)


@pytest.mark.asyncio
async def test_create_port_extracts_ip():
    backend, _ = make()
    port = await backend.create_port("internet", "vk-direct-probe-1", 30)
    assert port.ip == "217.16.22.90"
    assert port.name == "vk-direct-probe-1"


@pytest.mark.asyncio
async def test_pool_exhausted_is_recognised():
    backend, _ = make(create_error=Exception("409 No more IP addresses available"))
    with pytest.raises(PoolExhausted):
        await backend.create_port("internet", "probe", 30)


@pytest.mark.asyncio
async def test_lists_map_sdk_fields():
    backend, _ = make()
    assert (await backend.list_flavors())[0].ram == 2048
    assert (await backend.list_images())[0].name == "Ubuntu-24.04-202405"
    assert await backend.list_keypairs() == ["vk-key"]
    assert (await backend.list_ports("internet"))[0].ip == "217.16.22.90"


@pytest.mark.asyncio
async def test_server_boots_from_volume_on_found_port():
    backend, connection = make()
    server = await backend.create_server(
        name="vk-direct-217-16-22-90",
        flavor_id="flv-1",
        image_id="img-1",
        port_id="port-1",
        key_name="vk-key",
        volume_size=10,
        volume_type="ceph-ssd",
        timeout=60,
    )
    assert server.status == "ACTIVE"

    kwargs = connection.compute.created_with
    assert kwargs["networks"] == [{"port": "port-1"}]
    assert kwargs["key_name"] == "vk-key"
    bdm = kwargs["block_device_mapping_v2"][0]
    assert bdm["uuid"] == "img-1"
    assert bdm["volume_size"] == 10
    assert bdm["volume_type"] == "ceph-ssd"
    assert bdm["destination_type"] == "volume"
    # imageRef не передаём — иначе Nova ругается на двойной источник загрузки
    assert "imageRef" not in kwargs


# --- floating IP --------------------------------------------------------


@pytest.mark.asyncio
async def test_floating_ip_is_created_with_our_label():
    backend, connection = make()
    fip = await backend.create_floating_ip("internet", "vk-direct-probe-7", 30)
    assert fip.ip == "217.16.22.90"
    assert fip.description == "vk-direct-probe-7"
    assert not fip.attached


@pytest.mark.asyncio
async def test_floating_ip_pool_exhausted():
    backend, _ = make(create_error=Exception("409 No more IP addresses available"))
    with pytest.raises(PoolExhausted):
        await backend.create_floating_ip("internet", "probe", 30)


@pytest.mark.asyncio
async def test_floating_ip_found_by_description():
    backend, _ = make()
    assert (await backend.find_floating_ip("vk-direct-probe-1")).id == "fip-1"
    assert await backend.find_floating_ip("чужая-метка") is None


@pytest.mark.asyncio
async def test_attach_uses_internal_port_of_server():
    backend, connection = make()
    result = await backend.attach_floating_ip("fip-1", "srv-1")
    assert result.attached
    assert connection.network.updated == {
        "fip": "fip-1",
        "port_id": "srv-port",
        "fixed_ip": "10.0.0.5",
    }


@pytest.mark.asyncio
async def test_servers_are_listed_for_attach():
    backend, _ = make()
    servers = await backend.list_servers()
    assert servers[0].name == "vk-direct"
    assert servers[0].status == "ACTIVE"
