import asyncio
from pathlib import Path

import pytest

from vkiphunter.cloud.base import (
    CloudBackend,
    CloudError,
    CloudInfo,
    FloatingIpInfo,
    PoolExhausted,
    PortInfo,
)
from vkiphunter.hunter import EventKind, Hunter
from vkiphunter.state import (
    MODE_FLOATING_IP,
    OUTCOME_POOL_EMPTY,
    OUTCOME_QUOTA,
    OUTCOME_REJECT,
    Store,
)


class ScriptedCloud(CloudBackend):
    """Выдаёт заранее заданную последовательность ответов на create_port."""

    def __init__(self, script):
        self.script = list(script)
        self.created: list[PortInfo] = []
        self.deleted: list[str] = []
        self.calls = 0
        self.subnets_asked: list[str] = []

    def _next(self):
        self.calls += 1
        item = self.script.pop(0) if self.script else "83.0.0.1"
        if isinstance(item, Exception):
            raise item
        return item

    async def list_subnets(self, network):
        from vkiphunter.cloud.base import SubnetInfo

        return [
            SubnetInfo(id="s-1", name="ext-01", cidr="161.104.107.0/24"),
            SubnetInfo(id="s-2", name="ext-02", cidr="217.16.16.0/20"),
        ]

    async def ip_availability(self, network):
        from vkiphunter.cloud.base import SubnetAvailability

        return [
            SubnetAvailability(
                subnet_id="s-1", name="ext-01", cidr="161.104.107.0/24",
                total=256, used=256,
            ),
            SubnetAvailability(
                subnet_id="s-2", name="ext-02", cidr="217.16.16.0/20",
                total=4096, used=4090,
            ),
        ]

    async def list_networks(self):
        from vkiphunter.cloud.base import NetworkInfo

        return [
            NetworkInfo(
                id="net-1",
                name="internet",
                external=True,
                subnets=await self.list_subnets("internet"),
            )
        ]

    async def create_port(self, network, name, timeout, subnet_id=""):
        self.subnets_asked.append(subnet_id)
        port = PortInfo(id=f"port-{self.calls + 1}", name=name, ip=self._next())
        self.created.append(port)
        return port

    async def delete_port(self, port_id):
        self.deleted.append(port_id)

    async def find_port(self, name):
        return None

    async def create_floating_ip(self, network, description, timeout, subnet_id=""):
        self.subnets_asked.append(subnet_id)
        fip = FloatingIpInfo(
            id=f"fip-{self.calls + 1}", ip=self._next(), description=description
        )
        self.created.append(fip)
        return fip

    async def delete_floating_ip(self, fip_id):
        self.deleted.append(fip_id)

    async def find_floating_ip(self, description):
        return None

    async def list_floating_ips(self):
        return [c for c in self.created if isinstance(c, FloatingIpInfo)]

    async def attach_floating_ip(self, fip_id, server_id):
        raise NotImplementedError

    async def list_servers(self):
        return []

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


def make_hunter(tmp_path: Path, script, interval=0, mode=None, workers=1):
    store = Store(tmp_path / "state.json")
    store.load()
    store.state.settings.interval = interval
    store.state.settings.workers = workers
    if mode:
        store.state.settings.mode = mode
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
async def test_waits_through_long_pool_outage(tmp_path):
    """409 подряд сколько угодно раз — цикл ждёт и продолжает, не сдаваясь."""
    outage = [PoolExhausted("409 No more IP addresses available")] * 40
    hunter, store, cloud = make_hunter(tmp_path, [*outage, "217.16.22.90"])

    await hunter.start()
    await asyncio.wait_for(hunter._task, timeout=10)

    assert store.state.stats.pool_empty == 40
    assert store.state.stats.attempts == 41
    assert store.state.last_found().ip == "217.16.22.90"
    # ни одного порта не удалено: при 409 порт не создаётся
    assert cloud.deleted == []


@pytest.mark.asyncio
async def test_errors_do_not_stop_the_loop_either(tmp_path):
    """Сбои API тоже только откладывают попытку, а не останавливают охоту."""
    script = [CloudError("тайм-аут 120 с при обращении к API")] * 15
    hunter, store, _ = make_hunter(tmp_path, [*script, "5.188.140.9"])

    await hunter.start()
    await asyncio.wait_for(hunter._task, timeout=10)

    assert store.state.stats.errors == 15
    assert store.state.last_found().ip == "5.188.140.9"


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


# --- режим floating IP --------------------------------------------------


@pytest.mark.asyncio
async def test_floating_ip_mode_keeps_matching_address(tmp_path):
    hunter, store, cloud = make_hunter(
        tmp_path,
        ["83.166.255.3", "217.16.22.90"],
        mode=MODE_FLOATING_IP,
    )
    await hunter.start()
    await asyncio.wait_for(hunter._task, timeout=5)

    found = store.state.last_found()
    assert found.ip == "217.16.22.90"
    assert found.kind == MODE_FLOATING_IP
    assert found.title == "floating IP"
    # освобождён только неподходящий адрес, найденный сохранён
    assert cloud.deleted == ["fip-1"]


@pytest.mark.asyncio
async def test_floating_ip_mode_waits_out_409(tmp_path):
    outage = [PoolExhausted("409 No more IP addresses available")] * 12
    hunter, store, cloud = make_hunter(
        tmp_path, [*outage, "5.188.140.4"], mode=MODE_FLOATING_IP
    )
    await hunter.start()
    await asyncio.wait_for(hunter._task, timeout=5)

    assert store.state.stats.pool_empty == 12
    assert store.state.last_found().ip == "5.188.140.4"
    assert cloud.deleted == []


@pytest.mark.asyncio
async def test_foreign_floating_ip_is_never_deleted(tmp_path):
    hunter, store, cloud = make_hunter(tmp_path, ["83.1.1.1"], mode=MODE_FLOATING_IP)
    store.state.settings.max_attempts = 1

    async def foreign(*args, **kwargs):
        fip = FloatingIpInfo(id="fip-x", ip="83.1.1.1", description="production-fip")
        cloud.created.append(fip)
        return fip

    cloud.create_floating_ip = foreign  # type: ignore[method-assign]
    await hunter.start()
    await asyncio.wait_for(hunter._task, timeout=5)

    assert cloud.deleted == []
    assert "НЕ удалён" in store.state.attempts[-1].detail


# --- параллельные воркеры -----------------------------------------------


@pytest.mark.asyncio
async def test_four_workers_share_the_queue_and_stop_on_match(tmp_path):
    script = ["83.1.1.1"] * 20 + ["217.16.22.90"] + ["83.2.2.2"] * 20
    hunter, store, cloud = make_hunter(tmp_path, script, workers=4)

    await hunter.start()
    await asyncio.wait_for(hunter._task, timeout=10)

    found = store.state.last_found()
    assert found.ip == "217.16.22.90"
    # найденный ресурс не освобождён, остальные — освобождены
    assert found.port_id not in cloud.deleted
    assert store.state.stats.rejected == len(cloud.deleted)
    # после находки воркеры не разгоняются дальше: хвост скрипта не выбран
    assert store.state.stats.attempts <= 25


@pytest.mark.asyncio
async def test_workers_are_numbered_in_history(tmp_path):
    hunter, store, _ = make_hunter(tmp_path, ["83.1.1.1"] * 8 + ["5.188.140.1"], workers=4)
    await hunter.start()
    await asyncio.wait_for(hunter._task, timeout=10)

    workers = {a.worker for a in store.state.attempts}
    assert workers == {0, 1, 2, 3}, "в истории должно быть видно, чей это запрос"


@pytest.mark.asyncio
async def test_parallel_labels_never_collide(tmp_path):
    """Метки ресурсов уникальны — иначе не отличить, чей ресурс подбирать."""
    hunter, store, cloud = make_hunter(tmp_path, ["83.1.1.1"] * 12 + ["217.16.16.1"], workers=4)
    await hunter.start()
    await asyncio.wait_for(hunter._task, timeout=10)

    labels = [getattr(c, "name", None) or c.description for c in cloud.created]
    assert len(labels) == len(set(labels))
    assert all(label.startswith("vk-direct-probe-") for label in labels)


@pytest.mark.asyncio
async def test_quota_is_not_confused_with_empty_pool(tmp_path):
    from vkiphunter.cloud import QuotaExceeded

    hunter, store, _ = make_hunter(
        tmp_path,
        [QuotaExceeded("409 Quota exceeded for resources: ['floatingip']"), "5.188.140.2"],
    )
    await hunter.start()
    await asyncio.wait_for(hunter._task, timeout=5)

    assert store.state.attempts[0].outcome == OUTCOME_QUOTA
    assert store.state.stats.quota == 1
    assert store.state.stats.pool_empty == 0


# --- выбор подсети ------------------------------------------------------


@pytest.mark.asyncio
async def test_chosen_subnet_is_passed_to_the_cloud(tmp_path):
    hunter, store, cloud = make_hunter(tmp_path, ["217.16.22.90"])
    store.state.settings.subnets = [{"id": "s-2", "cidr": "217.16.16.0/20"}]

    await hunter.start()
    await asyncio.wait_for(hunter._task, timeout=5)

    assert cloud.subnets_asked == ["s-2"]


@pytest.mark.asyncio
async def test_without_choice_subnet_is_not_forced(tmp_path):
    hunter, store, cloud = make_hunter(tmp_path, ["217.16.22.90"])
    await hunter.start()
    await asyncio.wait_for(hunter._task, timeout=5)
    assert cloud.subnets_asked == [""]


@pytest.mark.asyncio
async def test_subnet_also_applies_to_floating_ip_mode(tmp_path):
    hunter, store, cloud = make_hunter(
        tmp_path, ["5.188.140.5"], mode=MODE_FLOATING_IP
    )
    store.state.settings.subnets = [{"id": "s-2", "cidr": "217.16.16.0/20"}]
    await hunter.start()
    await asyncio.wait_for(hunter._task, timeout=5)
    assert cloud.subnets_asked == ["s-2"]


# --- раскладка подсетей по воркерам -------------------------------------


@pytest.mark.asyncio
async def test_each_worker_gets_its_own_subnet(tmp_path):
    """Четыре воркера на четыре подсети — каждый закреплён за своей."""
    hunter, store, cloud = make_hunter(tmp_path, ["83.1.1.1"] * 40, workers=4)
    store.state.settings.max_attempts = 12
    store.state.settings.subnets = [
        {"id": f"s-{i}", "cidr": f"10.{i}.0.0/16"} for i in range(4)
    ]

    await hunter.start()
    await asyncio.wait_for(hunter._task, timeout=10)

    by_worker: dict[int, set[str]] = {}
    for attempt in store.state.attempts:
        by_worker.setdefault(attempt.worker, set()).add(attempt.subnet)

    assert len(by_worker) == 4
    for subnets in by_worker.values():
        assert len(subnets) == 1, "воркер не должен прыгать между подсетями"
    # и все четыре подсети разобраны
    assert len({next(iter(s)) for s in by_worker.values()}) == 4


@pytest.mark.asyncio
async def test_single_worker_cycles_through_subnets(tmp_path):
    hunter, store, cloud = make_hunter(tmp_path, ["83.1.1.1"] * 40)
    store.state.settings.max_attempts = 6
    store.state.settings.subnets = [
        {"id": f"s-{i}", "cidr": f"10.{i}.0.0/16"} for i in range(3)
    ]

    await hunter.start()
    await asyncio.wait_for(hunter._task, timeout=10)

    asked = cloud.subnets_asked
    assert asked[:6] == ["s-0", "s-1", "s-2", "s-0", "s-1", "s-2"]


@pytest.mark.asyncio
async def test_two_workers_cover_all_four_subnets(tmp_path):
    hunter, store, cloud = make_hunter(tmp_path, ["83.1.1.1"] * 40, workers=2)
    store.state.settings.max_attempts = 8
    store.state.settings.subnets = [
        {"id": f"s-{i}", "cidr": f"10.{i}.0.0/16"} for i in range(4)
    ]

    await hunter.start()
    await asyncio.wait_for(hunter._task, timeout=10)

    assert set(cloud.subnets_asked) == {"s-0", "s-1", "s-2", "s-3"}
