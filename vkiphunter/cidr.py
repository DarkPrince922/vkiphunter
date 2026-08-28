"""CIDR-математика для проверки выданного IP.

Проверка выполняется строго через :mod:`ipaddress`; сравнение строковых
префиксов (``ip.startswith("217.16.")``) недопустимо, потому что даёт
ложные срабатывания на соседних сетях.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

#: Целевые диапазоны из исторического watcher-скрипта.
#: 217.16.16.0/21 + 217.16.24.0/22 + 217.16.28.0/22 полностью покрывают
#: 217.16.16.0/20, куда входил рабочий адрес 217.16.22.90.
DEFAULT_TARGETS: tuple[str, ...] = (
    "5.188.140.0/22",
    "217.16.16.0/21",
    "217.16.24.0/22",
    "217.16.28.0/22",
)

#: Пулы, которые уже отбраковывались вручную.
DEFAULT_EXCLUDES: tuple[str, ...] = ()


class CidrError(ValueError):
    """Некорректная запись сети или адреса."""


def parse_network(value: str) -> IPNetwork:
    """Разобрать одну сеть. Принимает и ``217.16.22.90/21``, и ``217.16.16.0/21``."""
    text = value.strip()
    if not text:
        raise CidrError("пустая строка вместо CIDR")
    try:
        return ipaddress.ip_network(text, strict=False)
    except ValueError as exc:
        raise CidrError(f"{value!r}: {exc}") from exc


def parse_networks(values: Iterable[str]) -> list[IPNetwork]:
    """Разобрать список сетей, сохраняя порядок."""
    return [parse_network(value) for value in values]


def parse_address(value: str) -> IPAddress:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError as exc:
        raise CidrError(f"{value!r}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class CidrDecision:
    """Результат проверки адреса по спискам целей и исключений."""

    ip: str
    accepted: bool
    matched_target: str | None = None
    matched_exclude: str | None = None

    @property
    def reason(self) -> str:
        if self.matched_exclude:
            return f"исключён по {self.matched_exclude}"
        if self.accepted:
            return f"входит в {self.matched_target}"
        return "вне целевых диапазонов"


def check_ip(
    ip: str,
    targets: Sequence[str] = DEFAULT_TARGETS,
    excludes: Sequence[str] = DEFAULT_EXCLUDES,
) -> CidrDecision:
    """Проверить адрес: сначала исключения, затем целевые диапазоны.

    Исключения имеют приоритет — адрес из целевого диапазона, попавший
    в чёрный список, считается неподходящим.
    """
    address = parse_address(ip)

    for network in parse_networks(excludes):
        if address.version == network.version and address in network:
            return CidrDecision(str(address), False, matched_exclude=str(network))

    for network in parse_networks(targets):
        if address.version == network.version and address in network:
            return CidrDecision(str(address), True, matched_target=str(network))

    return CidrDecision(str(address), False)


def validate_networks(values: Iterable[str]) -> list[str]:
    """Нормализовать список сетей, отбросив дубликаты."""
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(str(parse_network(value)), None)
    return list(seen)
