"""Симулятор VK Cloud для отладки бота без реального проекта.

Включается переменной ``SIMULATE=1``. Повторяет наблюдавшееся поведение:
адреса выдаются в основном из чужих пулов (83.*, 212.233.*, 90.156.*,
213.219.*), изредка приходит 409, и совсем редко — адрес из целевого
диапазона.
"""

from __future__ import annotations

import asyncio
import ipaddress
import random
import time
import uuid

from .base import (
    CloudBackend,
    CloudInfo,
    FlavorInfo,
    ImageInfo,
    PoolExhausted,
    PortInfo,
    ServerInfo,
)

#: Пулы, из которых «выдаёт» адреса симулятор: (сеть, вес).
POOLS: tuple[tuple[str, int], ...] = (
    ("83.166.240.0/20", 35),
    ("212.233.64.0/18", 20),
    ("90.156.128.0/18", 15),
    ("213.219.192.0/18", 15),
    ("5.188.140.0/22", 8),
    ("217.16.16.0/20", 7),
)

POOL_EMPTY_CHANCE = 0.12


class FakeCloud(CloudBackend):
    def __init__(
        self,
        latency: float = 0.4,
        seed: int | None = None,
        pool_empty_chance: float = POOL_EMPTY_CHANCE,
    ) -> None:
        self._ports: dict[str, PortInfo] = {}
        self._latency = latency
        self._random = random.Random(seed)
        self.pool_empty_chance = pool_empty_chance

    async def _tick(self) -> None:
        await asyncio.sleep(self._latency)

    def _random_ip(self) -> str:
        networks = [n for n, _ in POOLS]
        weights = [w for _, w in POOLS]
        network = ipaddress.ip_network(self._random.choices(networks, weights)[0])
        offset = self._random.randint(1, network.num_addresses - 2)
        return str(network.network_address + offset)

    async def check(self, network: str) -> CloudInfo:
        await self._tick()
        return CloudInfo(
            project_id="sim-project",
            project_name="simulated",
            username="sim-user",
            region="RegionOne",
            network_id="sim-network",
            network_name=network,
            subnets=["sim-subnet"],
        )

    async def create_port(self, network: str, name: str, timeout: int) -> PortInfo:
        await self._tick()
        if self._random.random() < self.pool_empty_chance:
            raise PoolExhausted("409 No more IP addresses available")
        port = PortInfo(
            id=str(uuid.uuid4()),
            name=name,
            ip=self._random_ip(),
            network_id="sim-network",
            status="DOWN",
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self._ports[port.id] = port
        return port

    async def delete_port(self, port_id: str) -> None:
        await self._tick()
        self._ports.pop(port_id, None)

    async def find_port(self, name: str) -> PortInfo | None:
        await self._tick()
        for port in self._ports.values():
            if port.name == name or port.id == name:
                return port
        return None

    async def list_ports(self, network: str | None = None) -> list[PortInfo]:
        await self._tick()
        return list(self._ports.values())

    async def list_flavors(self) -> list[FlavorInfo]:
        await self._tick()
        return [
            FlavorInfo(id="flv-1", name="STD2-1-2", vcpus=1, ram=2048, disk=0),
            FlavorInfo(id="flv-2", name="STD2-2-4", vcpus=2, ram=4096, disk=0),
        ]

    async def list_images(self) -> list[ImageInfo]:
        await self._tick()
        return [
            ImageInfo(id="img-1", name="Ubuntu-24.04-202405", status="active"),
            ImageInfo(id="img-2", name="Debian-12-202405", status="active"),
        ]

    async def list_keypairs(self) -> list[str]:
        await self._tick()
        return ["sim-key"]

    async def create_server(
        self,
        *,
        name: str,
        flavor_id: str,
        image_id: str,
        port_id: str,
        key_name: str | None,
        volume_size: int,
        volume_type: str,
        timeout: int,
    ) -> ServerInfo:
        await self._tick()
        port = self._ports.get(port_id)
        if port:
            port.device_id = "sim-server"
        return ServerInfo(
            id=str(uuid.uuid4()),
            name=name,
            status="ACTIVE",
            addresses={"internet": [{"addr": port.ip if port else "0.0.0.0"}]},
        )
