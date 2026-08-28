import asyncio
from pathlib import Path

import pytest

from vkiphunter.cloud.base import CloudBackend, CloudError, CloudInfo, PoolExhausted, PortInfo
from vkiphunter.hunter import EventKind, Hunter
from vkiphunter.state import OUTCOME_POOL_EMPTY, OUTCOME_REJECT, Store


class ScriptedCloud(CloudBackend):
    """Выдаёт заранее заданную последовательность ответов на create_port."""

    def __init__(self, script):
        self.script = list(script)
        self.created: list[PortInfo] = []
        self.deleted: list[str] = []
        self.calls = 0

    async def create_port(self, network, name, timeout):
        self.calls += 1
        item = self.script.pop(0) if self.script else "83.0.0.1"
        if isinstance(item, Exception):
            raise item
        port = PortInfo(id=f"port-{self.calls}", name=name, ip=item)
        self.created.append(port)
        return port

    async def delete_port(self, port_id):
        self.deleted.append(port_id)

    async def find_port(self, name):
        return None

    async def check(self, network):
        return CloudInfo()

    async def list_ports(self, network=None):
        return list(self.created)

    async def list_flavors(self):
        return []

    async def list_images(self):
        return []

    async def list_keypairs(self):
        return []

    async def create_server(self, **kwargs):
        raise NotImplementedError


def make_hunter(tmp_path: Path, script, interval=0):
    store = Store(tmp_path / "state.json")
    store.load()
    store.state.settings.interval = interval
    cloud = ScriptedCloud(script)
    return Hunter(cloud, store), store, cloud


@pytest.mark.asyncio
async def test_stops_on_match_and_keeps_port(tmp_path):
    hunter, store, cloud = make_hunter(
        tmp_path, ["83.166.255.3", "212.233.93.99", "217.16.22.90"]
    )
    events = []
    hunter.add_listener(lambda e: events.append(e) or asyncio.sleep(0))

    await hunter.start()
    await asyncio.wait_for(hunter._task, timeout=5)

    found = store.state.last_found()
    assert found is not None
    assert found.ip == "217.16.22.90"
    assert found.matched_cidr == "217.16.16.0/21"
    # удалены только два неподходящих порта, найденный сохранён
    assert cloud.deleted == ["port-1", "port-2"]
    assert EventKind.MATCH in [e.kind for e in events]
    assert store.state.stats.matches == 1
    assert store.state.stats.rejected == 2


@pytest.mark.asyncio
async def test_409_is_not_fatal_and_loop_continues(tmp_path):
    hunter, store, cloud = make_hunter(
        tmp_path,
        [PoolExhausted("409 No more IP addresses available"), "5.188.140.10"],
    )
    await hunter.start()
    await asyncio.wait_for(hunter._task, timeout=5)

    outcomes = [a.outcome for a in store.state.attempts]
    assert outcomes == [OUTCOME_POOL_EMPTY, "match"]
    assert store.state.stats.pool_empty == 1
    assert cloud.deleted == []


@pytest.mark.asyncio
async def test_api_error_is_recorded_and_retried(tmp_path):
    hunter, store, _ = make_hunter(
        tmp_path, [CloudError("тайм-аут 120 с при обращении к API"), "217.16.28.5"]
    )
    await hunter.start()
    await asyncio.wait_for(hunter._task, timeout=5)

    assert store.state.stats.errors == 1
    assert store.state.last_found().ip == "217.16.28.5"


@pytest.mark.asyncio
async def test_stop_interrupts_waiting(tmp_path):
    hunter, store, cloud = make_hunter(
        tmp_path, ["83.1.1.1"] * 50, interval=30
    )
    await hunter.start()
    while store.state.stats.attempts < 1:
        await asyncio.sleep(0.01)
    await asyncio.wait_for(hunter.stop(), timeout=5)

    assert not hunter.running
    assert store.state.stats.attempts == 1
    assert cloud.deleted == ["port-1"]


@pytest.mark.asyncio
async def test_foreign_port_is_never_deleted(tmp_path):
    hunter, store, cloud = make_hunter(tmp_path, ["83.1.1.1"])
    store.state.settings.max_attempts = 1
    store.state.settings.prefix = "vk-direct-probe"

    async def rename(*args, **kwargs):
        port = PortInfo(id="port-x", name="production-lb-port", ip="83.1.1.1")
        cloud.created.append(port)
        return port

    cloud.create_port = rename  # type: ignore[method-assign]
    await hunter.start()
    await asyncio.wait_for(hunter._task, timeout=5)

    assert cloud.deleted == []
    assert store.state.attempts[-1].outcome == OUTCOME_REJECT
    assert "НЕ удалён" in store.state.attempts[-1].detail


@pytest.mark.asyncio
async def test_max_attempts_limit(tmp_path):
    hunter, store, _ = make_hunter(tmp_path, ["83.1.1.1"] * 10)
    store.state.settings.max_attempts = 3
    await hunter.start()
    await asyncio.wait_for(hunter._task, timeout=5)
    assert store.state.stats.attempts == 3
