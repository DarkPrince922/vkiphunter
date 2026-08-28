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

#: Публичные диапазоны VK. Вложенные подсети из исходного списка убраны:
#: 91.231.133.0/24 и 91.231.134.0/24 лежат в 91.231.132.0/22,
#: 185.16.244.0/23 и 185.16.247.0/24 — в 185.16.244.0/22.
#: 217.16.16.0/20 покрывает все три исторические цели 217.16.*.
VK_RANGES: tuple[str, ...] = (
    "5.61.16.0/21",
    "5.101.40.0/22",
    "5.188.140.0/22",
    "45.84.128.0/22",
    "45.136.20.0/22",
    "79.137.240.0/21",
    "83.222.28.0/22",
    "89.208.228.0/22",
    "89.221.228.0/22",
    "89.221.232.0/22",
    "89.221.236.0/22",
    "90.156.232.0/21",
    "91.231.132.0/22",
    "94.100.176.0/20",
    "95.163.32.0/19",
    "95.163.248.0/21",
    "130.49.224.0/19",
    "155.212.192.0/20",
    "176.112.168.0/21",
    "178.237.16.0/20",
    "185.16.148.0/22",
    "185.16.244.0/22",
    "185.100.104.0/22",
    "185.180.200.0/22",
    "185.226.52.0/22",
    "188.93.56.0/21",
    "193.203.40.0/22",
    "194.186.63.0/24",
    "217.16.16.0/20",
    "217.20.144.0/20",
    "217.174.188.0/22",
)


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


def overlapping_target(cidr: str, targets: Sequence[str] = DEFAULT_TARGETS) -> str | None:
    """Первый целевой диапазон, пересекающийся с этой сетью.

    Нужно, чтобы показать, из какой подсети внешней сети вообще есть смысл
    просить адрес: если ни одна не пересекается с целями, ждать бесполезно.
    """
    try:
        network = parse_network(cidr)
    except CidrError:
        return None
    for target in parse_networks(targets):
        if network.version == target.version and network.overlaps(target):
            return str(target)
    return None


def split_networks(text: str) -> list[str]:
    """Разобрать список сетей из одного сообщения.

    Принимает что угодно человеческое: запятые, пробелы, переносы строк,
    маркеры списка — чтобы можно было вставить готовый перечень целиком.
    """
    cleaned = text.replace(",", " ").replace(";", " ").replace("•", " ")
    return [chunk for chunk in cleaned.split() if chunk not in {"-", "*"}]


@dataclass(frozen=True, slots=True)
class MergeResult:
    """Что произошло при добавлении списка сетей."""

    final: list[str]
    added: list[str]
    duplicates: list[str]
    nested: list[tuple[str, str]]
    absorbed: list[tuple[str, str]]
    invalid: list[str]


def merge_networks(current: Sequence[str], values: Iterable[str]) -> MergeResult:
    """Добавить сети к списку, свернув вложенные.

    Вложенная сеть ничего не добавляет: адрес, попадающий в неё, и так
    попадает в объемлющую. Поэтому широкие обрабатываются первыми, а уже
    имевшиеся узкие поглощаются добавленной широкой.
    """
    added: list[str] = []
    duplicates: list[str] = []
    nested: list[tuple[str, str]] = []
    absorbed: list[tuple[str, str]] = []
    invalid: list[str] = []

    known = [parse_network(item) for item in current]

    candidates = []
    for value in values:
        try:
            candidates.append(parse_network(value))
        except CidrError:
            invalid.append(value)

    # Широкие вперёд — иначе узкая успеет попасть в список до объемлющей.
    for network in sorted(candidates, key=lambda n: (n.version, n.prefixlen)):
        if any(network == other for other in known):
            duplicates.append(str(network))
            continue

        container = next(
            (
                other
                for other in known
                if network.version == other.version and network.subnet_of(other)
            ),
            None,
        )
        if container is not None:
            nested.append((str(network), str(container)))
            continue

        swallowed = [
            other
            for other in known
            if other.version == network.version and other.subnet_of(network)
        ]
        for other in swallowed:
            known.remove(other)
            absorbed.append((str(other), str(network)))
            if str(other) in added:
                added.remove(str(other))

        known.append(network)
        added.append(str(network))

    # Решения о вложенности принимались по ширине сетей, но список
    # показывается человеку — возвращаем порядок, в котором их прислали.
    added_set = set(added)
    added_ordered: list[str] = []
    for network in candidates:
        name = str(network)
        if name in added_set and name not in added_ordered:
            added_ordered.append(name)

    kept_existing = [str(n) for n in known if str(n) not in added_set]
    return MergeResult(
        kept_existing + added_ordered,
        added_ordered,
        duplicates,
        nested,
        absorbed,
        invalid,
    )


def validate_networks(values: Iterable[str]) -> list[str]:
    """Нормализовать список сетей, отбросив дубликаты."""
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(str(parse_network(value)), None)
    return list(seen)
