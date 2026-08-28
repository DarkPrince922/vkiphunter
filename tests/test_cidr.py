import pytest

from vkiphunter.cidr import (
    VK_RANGES,
    CidrError,
    check_ip,
    merge_networks,
    split_networks,
    validate_networks,
)


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


# --- массовый ввод и свёртка вложенных --------------------------------


def test_split_accepts_human_lists():
    text = "5.188.140.0/22, 217.16.16.0/20\n • 91.231.132.0/22;94.100.176.0/20"
    assert split_networks(text) == [
        "5.188.140.0/22",
        "217.16.16.0/20",
        "91.231.132.0/22",
        "94.100.176.0/20",
    ]


def test_merge_skips_nested_networks():
    result = merge_networks(["91.231.132.0/22"], ["91.231.133.0/24"])
    assert result.added == []
    assert result.nested == [("91.231.133.0/24", "91.231.132.0/22")]
    assert result.final == ["91.231.132.0/22"]


def test_merge_absorbs_existing_when_wider_added():
    result = merge_networks(
        ["217.16.16.0/21", "217.16.24.0/22"], ["217.16.16.0/20"]
    )
    assert result.final == ["217.16.16.0/20"]
    assert sorted(a for a, _ in result.absorbed) == [
        "217.16.16.0/21",
        "217.16.24.0/22",
    ]


def test_merge_handles_wide_and_narrow_in_one_message():
    """Порядок во входе не важен: широкая сеть побеждает узкую."""
    result = merge_networks([], ["217.16.24.0/22", "217.16.16.0/20"])
    assert result.final == ["217.16.16.0/20"]


def test_merge_reports_duplicates_and_garbage():
    result = merge_networks(["5.188.140.0/22"], ["5.188.140.0/22", "не-сеть"])
    assert result.duplicates == ["5.188.140.0/22"]
    assert result.invalid == ["не-сеть"]
    assert result.added == []


def test_vk_preset_is_clean_and_covers_history():
    # в пресете нет вложенных друг в друга сетей
    assert merge_networks([], VK_RANGES).final == list(VK_RANGES)
    # и он покрывает исторические цели
    for ip in ("217.16.22.90", "5.188.142.1", "217.16.31.255"):
        assert check_ip(ip, VK_RANGES).accepted, ip
