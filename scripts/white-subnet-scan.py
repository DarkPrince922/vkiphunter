#!/usr/bin/env python3
"""Найти, какие подсети VK Cloud достижимы из текущей сети.

Запускать НА ТЕЛЕФОНЕ (Termux) под целевой SIM с выключенным Wi-Fi — тогда
проверяется именно тот канал, где действует белый список оператора. Скрипт
берёт по несколько случайных адресов из каждой подсети и проверяет
достижимость: ICMP-пинг плюс TCP-коннект на 443/80. Подсети, откуда что-то
отвечает, ранжируются наверх — это кандидаты, которые стоит ловить ботом.

Это предварительный фильтр канала, а не доказательство. «Молчит» может
значить и «фаервол режет ICMP», и «адрес не занят». Финальную пригодность
адреса всё равно подтверждает только реальный запрос на свою ВМ с этим IP.

Источник списка сетей — публичные BGP-анонсы VK из RIPEstat, а не догадки.

Примеры:
    # все префиксы, которые анонсирует AS47764 (VK), затем проверка
    python3 white-subnet-scan.py --asn 47764

    # свой список подсетей из файла (по одной CIDR в строке)
    python3 white-subnet-scan.py --file subnets.txt

    # подсети прямо в аргументах
    python3 white-subnet-scan.py 217.16.16.0/20 5.188.140.0/22
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import random
import socket
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

RIPESTAT = "https://stat.ripe.net/data/announced-prefixes/data.json?resource=AS{asn}"
TCP_PORTS = (443, 80)


def fetch_prefixes(asn: int) -> list[str]:
    """Префиксы, которые анонсирует ASN, из публичного RIPEstat."""
    url = RIPESTAT.format(asn=asn)
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.load(resp)
    prefixes = [
        item["prefix"]
        for item in data["data"]["prefixes"]
        if ":" not in item["prefix"]  # только IPv4
    ]
    return prefixes


def sample_hosts(cidr: str, count: int) -> list[str]:
    """Несколько случайных адресов подсети, без адреса сети и broadcast."""
    net = ipaddress.ip_network(cidr, strict=False)
    if net.num_addresses <= 2:
        return [str(net.network_address)]
    lo, hi = int(net.network_address) + 1, int(net.broadcast_address) - 1
    picks: set[int] = set()
    limit = min(count, hi - lo + 1)
    while len(picks) < limit:
        picks.add(random.randint(lo, hi))
    return [str(ipaddress.ip_address(p)) for p in sorted(picks)]


def ping(host: str, timeout: int = 2) -> bool:
    """Один ICMP-пакет. Флаги совместимы с Linux/Termux."""
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 2,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def tcp_open(host: str, port: int, timeout: int = 2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_host(host: str, timeout: int) -> dict:
    icmp = ping(host, timeout)
    ports = [p for p in TCP_PORTS if tcp_open(host, p, timeout)]
    return {"host": host, "icmp": icmp, "tcp": ports, "alive": icmp or bool(ports)}


def probe_subnet(cidr: str, count: int, timeout: int) -> dict:
    hosts = sample_hosts(cidr, count)
    results = [probe_host(h, timeout) for h in hosts]
    alive = [r for r in results if r["alive"]]
    return {"cidr": cidr, "checked": len(results), "alive": alive, "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cidrs", nargs="*", help="подсети CIDR")
    parser.add_argument("--asn", type=int, action="append", default=[], help="взять префиксы этого ASN из RIPE (можно несколько)")
    parser.add_argument("--file", help="файл со списком CIDR, по одной в строке")
    parser.add_argument("-n", "--per-subnet", type=int, default=3, help="сколько адресов проверять в подсети (по умолчанию 3)")
    parser.add_argument("-t", "--timeout", type=int, default=2, help="тайм-аут одной проверки, секунды")
    parser.add_argument("-j", "--jobs", type=int, default=16, help="сколько подсетей проверять параллельно")
    parser.add_argument("--only-alive", action="store_true", help="печатать только достижимые подсети")
    args = parser.parse_args()

    cidrs: list[str] = list(args.cidrs)
    if args.file:
        with open(args.file) as handle:
            cidrs += [line.strip() for line in handle if line.strip() and not line.startswith("#")]
    for asn in args.asn:
        print(f"# тяну префиксы AS{asn} из RIPEstat…", file=sys.stderr)
        try:
            fetched = fetch_prefixes(asn)
            print(f"#   получено {len(fetched)} IPv4-префиксов", file=sys.stderr)
            cidrs += fetched
        except Exception as exc:  # noqa: BLE001
            print(f"#   не вышло: {exc}", file=sys.stderr)

    # нормализуем и убираем дубликаты, сохраняя порядок
    seen: dict[str, None] = {}
    for c in cidrs:
        try:
            seen.setdefault(str(ipaddress.ip_network(c, strict=False)), None)
        except ValueError:
            print(f"# пропускаю {c!r}: не CIDR", file=sys.stderr)
    subnets = list(seen)

    if not subnets:
        parser.error("нет ни одной подсети — задайте CIDR, --file или --asn")

    print(f"# проверяю {len(subnets)} подсетей, по {args.per_subnet} адреса, "
          f"ICMP+TCP{TCP_PORTS}", file=sys.stderr)
    print(f"# запускать это нужно ПОД ЦЕЛЕВОЙ SIM с выключенным Wi-Fi", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        reports = list(pool.map(lambda c: probe_subnet(c, args.per_subnet, args.timeout), subnets))

    reports.sort(key=lambda r: (-len(r["alive"]), r["cidr"]))

    alive_total = 0
    for report in reports:
        n_alive = len(report["alive"])
        if n_alive:
            alive_total += 1
        if args.only_alive and not n_alive:
            continue
        mark = "🎯" if n_alive else "  "
        line = f"{mark} {report['cidr']:20} {n_alive}/{report['checked']}"
        for r in report["alive"]:
            proto = []
            if r["icmp"]:
                proto.append("icmp")
            if r["tcp"]:
                proto.append("tcp:" + ",".join(map(str, r["tcp"])))
            line += f"  {r['host']}({'+'.join(proto)})"
        print(line)

    print(f"\n# достижимо подсетей: {alive_total} из {len(subnets)}", file=sys.stderr)
    print("# 🎯 = откуда что-то ответило. Это кандидаты для охоты ботом.", file=sys.stderr)
    print("# Финальная проверка адреса — всё равно реальный запрос на свою ВМ.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
