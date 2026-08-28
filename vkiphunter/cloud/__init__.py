"""Слой доступа к облаку.

Клиент живёт за прокси: креды можно поменять прямо в боте, прокси
пересоберёт соединение, а ссылки на объект у охотника и хендлеров
останутся валидными.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from ..secrets import METHOD_APPCRED, METHOD_PASSWORD, METHOD_SIMULATE, CloudCredentials
from .base import (
    CloudAuthError,
    CloudBackend,
    CloudError,
    CloudInfo,
    FlavorInfo,
    FloatingIpInfo,
    ImageInfo,
    NotFound,
    PoolExhausted,
    PortInfo,
    QuotaExceeded,
    ServerBrief,
    ServerInfo,
)


class NotConnected(CloudError):
    """Облако ещё не подключено — нужно пройти /connect."""


def build_backend(credentials: CloudCredentials) -> CloudBackend:
    """Собрать бэкенд под конкретные креды."""
    if credentials.simulate:
        from .fake import FakeCloud

        return FakeCloud()

    from .openstack_backend import OpenStackBackend

    return OpenStackBackend(credentials.auth_kwargs())


class CloudProxy(CloudBackend):
    """Держит текущий бэкенд и пересоздаёт его при смене кредов."""

    def __init__(self, provider: Callable[[], CloudCredentials]) -> None:
        self._provider = provider
        self._backend: CloudBackend | None = None
        self._lock = asyncio.Lock()

    @property
    def credentials(self) -> CloudCredentials:
        return self._provider()

    @property
    def connected(self) -> bool:
        return self.credentials.configured

    async def reset(self) -> None:
        """Забыть текущее соединение — следующий вызов поднимет новое."""
        async with self._lock:
            backend, self._backend = self._backend, None
        if backend is not None:
            await backend.close()

    async def _current(self) -> CloudBackend:
        credentials = self.credentials
        if not credentials.configured:
            raise NotConnected(
                "облако не подключено — пройдите /connect, и бот спросит доступы"
            )
        async with self._lock:
            if self._backend is None:
                self._backend = build_backend(credentials)
            return self._backend

    # --- делегирование --------------------------------------------------

    async def check(self, network: str) -> CloudInfo:
        return await (await self._current()).check(network)

    async def create_port(self, network: str, name: str, timeout: int) -> PortInfo:
        return await (await self._current()).create_port(network, name, timeout)

    async def delete_port(self, port_id: str) -> None:
        await (await self._current()).delete_port(port_id)

    async def find_port(self, name: str) -> PortInfo | None:
        return await (await self._current()).find_port(name)

    async def list_ports(self, network: str | None = None) -> list[PortInfo]:
        return await (await self._current()).list_ports(network)

    async def create_floating_ip(
        self, network: str, description: str, timeout: int
    ) -> FloatingIpInfo:
        return await (await self._current()).create_floating_ip(
            network, description, timeout
        )

    async def delete_floating_ip(self, fip_id: str) -> None:
        await (await self._current()).delete_floating_ip(fip_id)

    async def find_floating_ip(self, description: str) -> FloatingIpInfo | None:
        return await (await self._current()).find_floating_ip(description)

    async def list_floating_ips(self) -> list[FloatingIpInfo]:
        return await (await self._current()).list_floating_ips()

    async def attach_floating_ip(self, fip_id: str, server_id: str) -> FloatingIpInfo:
        return await (await self._current()).attach_floating_ip(fip_id, server_id)

    async def list_servers(self) -> list[ServerBrief]:
        return await (await self._current()).list_servers()

    async def list_flavors(self) -> list[FlavorInfo]:
        return await (await self._current()).list_flavors()

    async def list_images(self) -> list[ImageInfo]:
        return await (await self._current()).list_images()

    async def list_keypairs(self) -> list[str]:
        return await (await self._current()).list_keypairs()

    async def create_server(self, **kwargs) -> ServerInfo:
        return await (await self._current()).create_server(**kwargs)

    async def close(self) -> None:
        await self.reset()


__all__ = [
    "CloudAuthError",
    "CloudBackend",
    "CloudError",
    "CloudInfo",
    "CloudProxy",
    "FlavorInfo",
    "FloatingIpInfo",
    "ImageInfo",
    "METHOD_APPCRED",
    "METHOD_PASSWORD",
    "METHOD_SIMULATE",
    "NotConnected",
    "NotFound",
    "PoolExhausted",
    "PortInfo",
    "QuotaExceeded",
    "ServerBrief",
    "ServerInfo",
    "build_backend",
]
