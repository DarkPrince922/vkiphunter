"""Абстракция облака: то, что нужно охотнику, и ничего лишнего."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


class CloudError(Exception):
    """Ошибка облачного API."""


class CloudAuthError(CloudError):
    """Не удалось авторизоваться (протух OpenRC, неверный пароль, нет проекта)."""


class PoolExhausted(CloudError):
    """409 No more IP addresses available — в пуле сейчас нет свободного адреса."""


class NotFound(CloudError):
    """Ресурс не найден."""


@dataclass(slots=True)
class CloudInfo:
    project_id: str = ""
    project_name: str = ""
    username: str = ""
    region: str = ""
    network_id: str = ""
    network_name: str = ""
    subnets: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PortInfo:
    id: str
    name: str
    ip: str | None
    network_id: str = ""
    status: str = ""
    device_id: str = ""
    created_at: str = ""


@dataclass(slots=True)
class FlavorInfo:
    id: str
    name: str
    vcpus: int
    ram: int
    disk: int


@dataclass(slots=True)
class ImageInfo:
    id: str
    name: str
    status: str = "active"


@dataclass(slots=True)
class ServerInfo:
    id: str
    name: str
    status: str
    addresses: dict | None = None


class CloudBackend(abc.ABC):
    """Интерфейс, который реализуют OpenStack и симулятор."""

    @abc.abstractmethod
    async def check(self, network: str) -> CloudInfo:
        """Проверить авторизацию и наличие внешней сети."""

    @abc.abstractmethod
    async def create_port(self, network: str, name: str, timeout: int) -> PortInfo: ...

    @abc.abstractmethod
    async def delete_port(self, port_id: str) -> None: ...

    @abc.abstractmethod
    async def find_port(self, name: str) -> PortInfo | None: ...

    @abc.abstractmethod
    async def list_ports(self, network: str | None = None) -> list[PortInfo]: ...

    @abc.abstractmethod
    async def list_flavors(self) -> list[FlavorInfo]: ...

    @abc.abstractmethod
    async def list_images(self) -> list[ImageInfo]: ...

    @abc.abstractmethod
    async def list_keypairs(self) -> list[str]: ...

    @abc.abstractmethod
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
    ) -> ServerInfo: ...

    async def close(self) -> None:  # pragma: no cover - по умолчанию нечего закрывать
        return None
