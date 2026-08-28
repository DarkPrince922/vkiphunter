"""Бэкенд поверх openstacksdk.

Все вызовы SDK блокирующие, поэтому каждый уходит в поток, а сверху
навешивается тайм-аут — как ``timeout 120 openstack port create`` в
исходном скрипте.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..config import Settings
from .base import (
    CloudAuthError,
    CloudBackend,
    CloudError,
    CloudInfo,
    FlavorInfo,
    ImageInfo,
    NotFound,
    PoolExhausted,
    PortInfo,
    ServerInfo,
)

log = logging.getLogger(__name__)

_POOL_MARKERS = (
    "no more ip addresses available",
    "no more ip addresses",
    "ip address generation failure",
)


def _is_pool_exhausted(exc: BaseException) -> bool:
    text = str(exc).lower()
    if any(marker in text for marker in _POOL_MARKERS):
        return True
    return getattr(exc, "status_code", None) == 409 and "ip" in text


def _translate(exc: BaseException) -> CloudError:
    import openstack.exceptions as oe

    if _is_pool_exhausted(exc):
        return PoolExhausted("409 No more IP addresses available")
    if isinstance(exc, oe.HttpException) and getattr(exc, "status_code", None) in (
        401,
        403,
    ):
        return CloudAuthError(str(exc))
    if isinstance(exc, oe.ResourceNotFound):
        return NotFound(str(exc))
    return CloudError(str(exc))


class OpenStackBackend(CloudBackend):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._conn: Any = None
        self._conn_lock = asyncio.Lock()

    # --- подключение ----------------------------------------------------

    def _connect_sync(self) -> Any:
        import openstack

        openstack.enable_logging(debug=False)
        return openstack.connection.Connection(**self._settings.auth_kwargs())

    async def _connection(self) -> Any:
        if self._conn is None:
            async with self._conn_lock:
                if self._conn is None:
                    self._conn = await asyncio.to_thread(self._connect_sync)
        return self._conn

    async def _call(self, func, *args, timeout: float | None = None, **kwargs):
        conn = await self._connection()
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(func, conn, *args, **kwargs), timeout=timeout
            )
        except asyncio.TimeoutError:
            raise CloudError(f"тайм-аут {timeout:.0f} с при обращении к API") from None
        except CloudError:
            raise
        except Exception as exc:  # noqa: BLE001 - переводим любую ошибку SDK
            raise _translate(exc) from exc

    # --- проверки -------------------------------------------------------

    async def check(self, network: str) -> CloudInfo:
        def _check(conn: Any) -> CloudInfo:
            auth_ref = conn.auth_ref  # бросит исключение при неверных кредах
            net = conn.network.find_network(network, ignore_missing=True)
            if net is None:
                raise NotFound(f"внешняя сеть {network!r} не найдена")
            return CloudInfo(
                project_id=auth_ref.project_id or "",
                project_name=getattr(auth_ref, "project_name", "") or "",
                username=getattr(auth_ref, "username", "") or "",
                region=self._settings.os_region_name,
                network_id=net.id,
                network_name=net.name,
                subnets=list(net.subnet_ids or []),
            )

        return await self._call(_check, timeout=60)

    # --- порты ----------------------------------------------------------

    async def create_port(self, network: str, name: str, timeout: int) -> PortInfo:
        def _create(conn: Any) -> PortInfo:
            net = conn.network.find_network(network, ignore_missing=True)
            if net is None:
                raise NotFound(f"внешняя сеть {network!r} не найдена")
            port = conn.network.create_port(network_id=net.id, name=name)
            return _port_info(port)

        return await self._call(_create, timeout=timeout)

    async def delete_port(self, port_id: str) -> None:
        def _delete(conn: Any) -> None:
            conn.network.delete_port(port_id, ignore_missing=True)

        await self._call(_delete, timeout=120)

    async def find_port(self, name: str) -> PortInfo | None:
        def _find(conn: Any) -> PortInfo | None:
            port = conn.network.find_port(name, ignore_missing=True)
            return _port_info(port) if port else None

        return await self._call(_find, timeout=60)

    async def list_ports(self, network: str | None = None) -> list[PortInfo]:
        def _list(conn: Any) -> list[PortInfo]:
            query: dict[str, Any] = {}
            if network:
                net = conn.network.find_network(network, ignore_missing=True)
                if net is None:
                    raise NotFound(f"сеть {network!r} не найдена")
                query["network_id"] = net.id
            return [_port_info(p) for p in conn.network.ports(**query)]

        return await self._call(_list, timeout=120)

    # --- ресурсы для ВМ -------------------------------------------------

    async def list_flavors(self) -> list[FlavorInfo]:
        def _list(conn: Any) -> list[FlavorInfo]:
            return [
                FlavorInfo(
                    id=f.id,
                    name=f.name,
                    vcpus=f.vcpus or 0,
                    ram=f.ram or 0,
                    disk=f.disk or 0,
                )
                for f in conn.compute.flavors()
            ]

        return await self._call(_list, timeout=120)

    async def list_images(self) -> list[ImageInfo]:
        def _list(conn: Any) -> list[ImageInfo]:
            return [
                ImageInfo(id=i.id, name=i.name or "", status=i.status or "")
                for i in conn.image.images(status="active")
            ]

        return await self._call(_list, timeout=180)

    async def list_keypairs(self) -> list[str]:
        def _list(conn: Any) -> list[str]:
            return [k.name for k in conn.compute.keypairs()]

        return await self._call(_list, timeout=120)

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
        def _create(conn: Any) -> ServerInfo:
            block_device: dict[str, Any] = {
                "boot_index": 0,
                "uuid": image_id,
                "source_type": "image",
                "destination_type": "volume",
                "volume_size": volume_size,
                "delete_on_termination": True,
            }
            if volume_type:
                block_device["volume_type"] = volume_type

            kwargs: dict[str, Any] = {
                "name": name,
                "flavorRef": flavor_id,
                "networks": [{"port": port_id}],
                "block_device_mapping_v2": [block_device],
            }
            if key_name:
                kwargs["key_name"] = key_name

            server = conn.compute.create_server(**kwargs)
            server = conn.compute.wait_for_server(server, wait=timeout)
            return ServerInfo(
                id=server.id,
                name=server.name,
                status=server.status,
                addresses=dict(server.addresses or {}),
            )

        # +60 с сверху тайм-аута ожидания: сам wait_for_server уже ограничен.
        return await self._call(_create, timeout=timeout + 60)

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)


def _port_info(port: Any) -> PortInfo:
    fixed = list(port.fixed_ips or [])
    ip = fixed[0].get("ip_address") if fixed else None
    return PortInfo(
        id=port.id,
        name=port.name or "",
        ip=ip,
        network_id=port.network_id or "",
        status=port.status or "",
        device_id=port.device_id or "",
        created_at=str(port.created_at or ""),
    )
