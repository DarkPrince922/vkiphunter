"""Бэкенд поверх openstacksdk.

Все вызовы SDK блокирующие, поэтому каждый уходит в поток, а сверху
навешивается тайм-аут — как ``timeout 120 openstack port create`` в
исходном скрипте.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .base import (
    CloudAuthError,
    CloudBackend,
    CloudError,
    CloudInfo,
    FlavorInfo,
    FloatingIpInfo,
    ImageInfo,
    NetworkInfo,
    NotFound,
    PoolExhausted,
    PortInfo,
    QuotaExceeded,
    ServerBrief,
    ServerInfo,
    SubnetAvailability,
    SubnetInfo,
)

log = logging.getLogger(__name__)

_QUOTA_MARKERS = (
    "quota exceeded",
    "quotaexceeded",
    "превышена квота",
    "maximum number of ports exceeded",
    "maximum number of floating ips exceeded",
)

_POOL_MARKERS = (
    "no more ip addresses available",
    "no more ip addresses",
    "ip address generation failure",
)


def _is_quota_exceeded(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _QUOTA_MARKERS)


def _is_pool_exhausted(exc: BaseException) -> bool:
    text = str(exc).lower()
    if any(marker in text for marker in _POOL_MARKERS):
        return True
    return getattr(exc, "status_code", None) == 409 and "ip" in text


def _translate(exc: BaseException) -> CloudError:
    import keystoneauth1.exceptions as ke
    import openstack.exceptions as oe

    # Квоту проверяем раньше пула: оба отвечают 409, но ждать при
    # исчерпанной квоте бессмысленно.
    if _is_quota_exceeded(exc):
        return QuotaExceeded(str(exc))
    if _is_pool_exhausted(exc):
        return PoolExhausted("409 No more IP addresses available")

    # Ошибки авторизации приходят из keystoneauth, а не из openstacksdk.
    if isinstance(exc, ke.Unauthorized):
        return CloudAuthError("неверный логин, пароль или application credential")
    if isinstance(exc, ke.SSLError):
        return CloudError(f"проблема с TLS при обращении к Keystone: {exc}")
    if isinstance(exc, (ke.ConnectFailure, ke.DiscoveryFailure)):
        return CloudError(f"не достучаться до Keystone — проверьте OS_AUTH_URL: {exc}")
    if isinstance(exc, ke.MissingRequiredOptions):
        return CloudError(f"не хватает параметров подключения: {exc}")

    if isinstance(exc, oe.HttpException) and getattr(exc, "status_code", None) in (
        401,
        403,
    ):
        return CloudAuthError(str(exc))
    if isinstance(exc, oe.ResourceNotFound):
        return NotFound(str(exc))
    return CloudError(str(exc))


class OpenStackBackend(CloudBackend):
    def __init__(self, auth: dict[str, str]) -> None:
        self._auth = dict(auth)
        self._conn: Any = None
        self._conn_lock = asyncio.Lock()

    # --- подключение ----------------------------------------------------

    def _connect_sync(self) -> Any:
        import openstack

        return openstack.connection.Connection(**self._auth)

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
            # get_access выполняет саму авторизацию и бросает при неверных
            # кредах. В openstacksdk 4.x у Connection нет auth_ref — данные
            # о токене живут на keystoneauth-сессии.
            session = conn.session
            access = session.auth.get_access(session)

            net = conn.network.find_network(network, ignore_missing=True)
            if net is None:
                raise NotFound(f"внешняя сеть {network!r} не найдена")
            return CloudInfo(
                project_id=getattr(access, "project_id", "") or "",
                project_name=getattr(access, "project_name", "") or "",
                username=getattr(access, "username", "") or "",
                region=self._auth.get("region_name", ""),
                network_id=net.id,
                network_name=net.name,
                subnets=list(net.subnet_ids or []),
            )

        return await self._call(_check, timeout=60)

    # --- порты ----------------------------------------------------------

    async def list_networks(self) -> list[NetworkInfo]:
        """Внешние сети проекта вместе с их подсетями.

        Подсети забираем одним запросом и раскладываем по сетям — так
        дешевле, чем спрашивать их для каждой сети отдельно.
        """

        def _list(conn: Any) -> list[NetworkInfo]:
            by_network: dict[str, list[SubnetInfo]] = {}
            for subnet in conn.network.subnets():
                by_network.setdefault(subnet.network_id, []).append(
                    SubnetInfo(
                        id=subnet.id,
                        name=subnet.name or "",
                        cidr=subnet.cidr or "",
                        ip_version=subnet.ip_version or 4,
                    )
                )
            networks = []
            for net in conn.network.networks():
                networks.append(
                    NetworkInfo(
                        id=net.id,
                        name=net.name or "",
                        external=bool(net.is_router_external),
                        subnets=by_network.get(net.id, []),
                    )
                )
            return networks

        return await self._call(_list, timeout=180)

    async def ip_availability(self, network: str) -> list[SubnetAvailability]:
        """Сколько адресов занято в каждом пуле сети.

        Показывает и те подсети, которых нет в ``subnet list``: их может
        скрывать политика проекта, хотя IPAM из них выделяет.
        """

        def _availability(conn: Any) -> list[SubnetAvailability]:
            net = conn.network.find_network(network, ignore_missing=True)
            if net is None:
                raise NotFound(f"внешняя сеть {network!r} не найдена")
            report = conn.network.get_network_ip_availability(net.id)
            rows = []
            for item in report.subnet_ip_availability or []:
                rows.append(
                    SubnetAvailability(
                        subnet_id=item.get("subnet_id", ""),
                        name=item.get("subnet_name", "") or "",
                        cidr=item.get("cidr", "") or "",
                        total=int(item.get("total_ips") or 0),
                        used=int(item.get("used_ips") or 0),
                    )
                )
            return rows

        return await self._call(_availability, timeout=120)

    async def list_subnets(self, network: str) -> list[SubnetInfo]:
        def _list(conn: Any) -> list[SubnetInfo]:
            net = conn.network.find_network(network, ignore_missing=True)
            if net is None:
                raise NotFound(f"внешняя сеть {network!r} не найдена")
            return [
                SubnetInfo(
                    id=subnet.id,
                    name=subnet.name or "",
                    cidr=subnet.cidr or "",
                    ip_version=subnet.ip_version or 4,
                )
                for subnet in conn.network.subnets(network_id=net.id)
            ]

        return await self._call(_list, timeout=120)

    async def create_port(
        self, network: str, name: str, timeout: int, subnet_id: str = ""
    ) -> PortInfo:
        def _create(conn: Any) -> PortInfo:
            net = conn.network.find_network(network, ignore_missing=True)
            if net is None:
                raise NotFound(f"внешняя сеть {network!r} не найдена")
            attrs: dict[str, Any] = {"network_id": net.id, "name": name}
            if subnet_id:
                # Просим адрес из конкретного пула, а не любой свободный.
                attrs["fixed_ips"] = [{"subnet_id": subnet_id}]
            port = conn.network.create_port(**attrs)
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

    # --- floating IP ----------------------------------------------------

    async def create_floating_ip(
        self, network: str, description: str, timeout: int, subnet_id: str = ""
    ) -> FloatingIpInfo:
        def _create(conn: Any) -> FloatingIpInfo:
            net = conn.network.find_network(network, ignore_missing=True)
            if net is None:
                raise NotFound(f"внешняя сеть {network!r} не найдена")
            attrs: dict[str, Any] = {
                "floating_network_id": net.id,
                "description": description,
            }
            if subnet_id:
                # Neutron умеет выделять адрес из указанной подсети пула.
                attrs["subnet_id"] = subnet_id
            fip = conn.network.create_ip(**attrs)
            return _fip_info(fip)

        return await self._call(_create, timeout=timeout)

    async def delete_floating_ip(self, fip_id: str) -> None:
        def _delete(conn: Any) -> None:
            conn.network.delete_ip(fip_id, ignore_missing=True)

        await self._call(_delete, timeout=120)

    async def find_floating_ip(self, description: str) -> FloatingIpInfo | None:
        def _find(conn: Any) -> FloatingIpInfo | None:
            for fip in conn.network.ips(description=description):
                return _fip_info(fip)
            return None

        return await self._call(_find, timeout=60)

    async def list_floating_ips(self) -> list[FloatingIpInfo]:
        def _list(conn: Any) -> list[FloatingIpInfo]:
            return [_fip_info(fip) for fip in conn.network.ips()]

        return await self._call(_list, timeout=120)

    async def attach_floating_ip(self, fip_id: str, server_id: str) -> FloatingIpInfo:
        def _attach(conn: Any) -> FloatingIpInfo:
            target = None
            for port in conn.network.ports(device_id=server_id):
                if port.fixed_ips:
                    target = port
                    break
            if target is None:
                raise NotFound(
                    "у выбранной ВМ нет внутреннего порта с адресом — "
                    "привязать floating IP не к чему"
                )
            fip = conn.network.update_ip(
                fip_id,
                port_id=target.id,
                fixed_ip_address=target.fixed_ips[0].get("ip_address"),
            )
            return _fip_info(fip)

        return await self._call(_attach, timeout=180)

    async def list_servers(self) -> list[ServerBrief]:
        def _list(conn: Any) -> list[ServerBrief]:
            return [
                ServerBrief(id=s.id, name=s.name or "", status=s.status or "")
                for s in conn.compute.servers()
            ]

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


def _fip_info(fip: Any) -> FloatingIpInfo:
    return FloatingIpInfo(
        id=fip.id,
        ip=fip.floating_ip_address,
        description=fip.description or "",
        subnet_id=getattr(fip, "subnet_id", "") or "",
        status=fip.status or "",
        port_id=fip.port_id or "",
        created_at=str(fip.created_at or ""),
    )


def _port_info(port: Any) -> PortInfo:
    fixed = list(port.fixed_ips or [])
    ip = fixed[0].get("ip_address") if fixed else None
    # subnet_id из ответа — единственный способ узнать подсеть, которую
    # IPAM выбрал сам: в subnet list она может не показываться.
    subnet_id = fixed[0].get("subnet_id", "") if fixed else ""
    return PortInfo(
        id=port.id,
        name=port.name or "",
        ip=ip,
        subnet_id=subnet_id,
        network_id=port.network_id or "",
        status=port.status or "",
        device_id=port.device_id or "",
        created_at=str(port.created_at or ""),
    )
