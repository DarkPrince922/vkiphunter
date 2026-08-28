import pytest

from vkiphunter.cidr import CidrError, check_ip, validate_networks


def test_historical_working_ip_matches():
    decision = check_ip("217.16.22.90")
    assert decision.accepted
    assert decision.matched_target == "217.16.16.0/21"


@pytest.mark.parametrize(
    "ip",
    ["83.166.255.3", "212.233.93.99", "90.156.150.232", "90.156.214.204", "213.219.215.253"],
)
def test_observed_rejected_ips(ip):
    assert not check_ip(ip).accepted


def test_full_217_16_16_0_20_is_covered():
    for ip in ("217.16.16.0", "217.16.23.255", "217.16.24.0", "217.16.31.255"):
        assert check_ip(ip).accepted, ip
    # соседние сети вне /20 не должны проходить
    assert not check_ip("217.16.15.255").accepted
    assert not check_ip("217.16.32.0").accepted


def test_no_string_prefix_false_positive():
    # начинается с "217.16." строкой, но лежит вне целевого /20
    assert not check_ip("217.16.200.1").accepted


def test_exclude_wins_over_target():
    decision = check_ip("217.16.22.90", excludes=["217.16.22.0/24"])
    assert not decision.accepted
    assert decision.matched_exclude == "217.16.22.0/24"


def test_ipv6_does_not_crash():
    assert not check_ip("2a00:1148::1").accepted


def test_validate_networks_normalises_and_dedupes():
    assert validate_networks(["217.16.22.90/21", "217.16.16.0/21"]) == ["217.16.16.0/21"]


def test_invalid_network_raises():
    with pytest.raises(CidrError):
        validate_networks(["не-сеть"])
