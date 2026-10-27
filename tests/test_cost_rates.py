import pytest

from cabotage.utils.cost.rates import effective_rates


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ('{"vcpu_hour": "0.03"}', TypeError),
        ('{"vcpu_hour": true}', TypeError),
        ('{"fixed_hourly": 1}', TypeError),
        ('{"node_family_xlarge_hourly": {"m8g": "cheap"}}', TypeError),
        ('{"vcpu_hours": 0.03}', ValueError),
    ],
)
def test_bad_overrides_are_rejected(overrides, error):
    with pytest.raises(error, match="COST_RATES"):
        effective_rates({"COST_RATES": overrides})


def test_valid_overrides_apply():
    rates = effective_rates(
        {"COST_RATES": '{"vcpu_hour": 0.05, "fixed_hourly": {"nat_gateway": 0}}'}
    )

    assert rates["vcpu_hour"] == 0.05
    assert rates["fixed_hourly"]["nat_gateway"] == 0
    assert rates["fixed_hourly"]["eks_control_plane"] == pytest.approx(0.10)
