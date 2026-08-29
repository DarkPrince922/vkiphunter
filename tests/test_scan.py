"""Проверка логики сканера достижимости (без сети)."""

import ipaddress
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "white_subnet_scan", Path(__file__).resolve().parent.parent / "scripts" / "white-subnet-scan.py"
)
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)


def test_sample_stays_inside_subnet_and_skips_edges():
    net = ipaddress.ip_network("217.16.16.0/20")
    for host in scan.sample_hosts("217.16.16.0/20", 5):
        addr = ipaddress.ip_address(host)
        assert addr in net
        assert addr != net.network_address
        assert addr != net.broadcast_address


def test_sample_count_capped_by_subnet_size():
    # /31 — всего два адреса, столько и вернём максимум
    hosts = scan.sample_hosts("203.0.113.0/31", 5)
    assert len(hosts) <= 2


def test_sample_is_unique():
    hosts = scan.sample_hosts("10.0.0.0/24", 20)
    assert len(hosts) == len(set(hosts))


@pytest.mark.parametrize("cidr", ["5.188.140.0/22", "194.186.63.0/24", "217.16.16.0/20"])
def test_sample_returns_requested_count(cidr):
    assert len(scan.sample_hosts(cidr, 3)) == 3


def test_tcp_ports_are_reasonable():
    assert 443 in scan.TCP_PORTS
