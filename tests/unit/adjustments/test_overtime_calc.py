"""Overtime rate and amount (pure engine)."""

from datetime import date
from decimal import Decimal

import pytest

from payroll_copilot.calculations.overtime import (
    OvertimePolicy,
    check_hours,
    overtime_amount,
    overtime_rate,
)
from payroll_copilot.core.exceptions import CalculationError

POLICY = OvertimePolicy(version=1, effective_from=date(2026, 4, 1), method="MULTIPLE_OF_HOURLY",
                        rate_heads=["BASIC"], days_divisor=Decimal(26), hours_per_day=Decimal(8),
                        multiplier=Decimal(2), max_hours_per_day=Decimal(4),
                        max_hours_per_month=Decimal(50))  # fmt: skip


def test_multiple_of_hourly_from_basic():
    r = overtime_rate(POLICY, {"BASIC": Decimal(26000), "HRA": Decimal(13000)})
    # 26,000 / 26 / 8 = 125 per hour, x 2 = 250
    assert r.value == Decimal("250.00")
    assert "Overtime policy v1" in r.inputs["source"]
    assert overtime_amount(Decimal(6), r.value) == (Decimal(1500), "6 h × ₹250/h = ₹1,500")


def test_rate_is_rounded_to_paisa_before_multiplying():
    r = overtime_rate(POLICY, {"BASIC": Decimal(30000)})
    assert r.value == Decimal("288.46")  # 30000/26/8*2 = 288.4615...
    amount, formula = overtime_amount(Decimal("2.5"), r.value)
    assert amount == Decimal(721) and "2.5 h × ₹288.46/h" in formula  # 721.15 -> 721


def test_employee_override_wins_and_fixed_rate():
    r = overtime_rate(POLICY, {"BASIC": Decimal(26000)}, employee_rate=Decimal(300))
    assert r.value == 300 and r.inputs["source"].startswith("Employee configuration")
    fixed = POLICY.model_copy(update={"method": "FIXED_RATE", "fixed_rate_per_hour": Decimal(180)})
    assert overtime_rate(fixed, {}).value == 180


def test_no_rate_is_never_guessed():
    with pytest.raises(CalculationError, match="No BASIC"):
        overtime_rate(POLICY, {"HRA": Decimal(10000)})
    with pytest.raises(CalculationError, match="fixed_rate_per_hour"):
        overtime_rate(POLICY.model_copy(update={"method": "FIXED_RATE"}), {})
    with pytest.raises(CalculationError, match="disabled"):
        overtime_rate(POLICY.model_copy(update={"enabled": False}), {"BASIC": Decimal(1)})


def test_hour_limits():
    check_hours(POLICY, Decimal(4), Decimal(46))
    with pytest.raises(CalculationError, match="per day"):
        check_hours(POLICY, Decimal(5), Decimal(0))
    with pytest.raises(CalculationError, match="50 h"):
        check_hours(POLICY, Decimal(4), Decimal(47))
    with pytest.raises(CalculationError, match="positive"):
        check_hours(POLICY, Decimal(0), Decimal(0))
