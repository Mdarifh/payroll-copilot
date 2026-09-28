from decimal import Decimal

import pytest

from payroll_copilot.core.exceptions import MoneyError
from payroll_copilot.core.money import (
    PAISA,
    RoundingMode,
    apply_rate,
    format_inr,
    round_money,
    to_decimal,
)


class TestToDecimal:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1500, Decimal("1500")),
            ("1500.50", Decimal("1500.50")),
            ("1,25,000", Decimal("125000")),
            (" 42 ", Decimal("42")),
            (Decimal("0.1"), Decimal("0.1")),
        ],
    )
    def test_accepts_exact_types(self, value, expected):
        assert to_decimal(value) == expected

    @pytest.mark.parametrize("value", [0.1, 1500.0, True, None, [1]])
    def test_rejects_float_bool_and_others(self, value):
        with pytest.raises(MoneyError):
            to_decimal(value)

    @pytest.mark.parametrize("value", ["abc", "", "NaN", "Infinity"])
    def test_rejects_invalid_strings(self, value):
        with pytest.raises(MoneyError):
            to_decimal(value)


class TestRoundMoney:
    @pytest.mark.parametrize(
        ("value", "mode", "expected"),
        [
            ("1249.5", RoundingMode.HALF_UP, "1250"),
            ("1249.49", RoundingMode.HALF_UP, "1249"),
            ("1248.5", RoundingMode.HALF_EVEN, "1248"),
            ("1249.5", RoundingMode.HALF_EVEN, "1250"),
            ("112.01", RoundingMode.CEIL, "113"),
            ("112.00", RoundingMode.CEIL, "112"),
            ("112.99", RoundingMode.FLOOR, "112"),
            ("-10.5", RoundingMode.HALF_UP, "-11"),
            ("1249.5", RoundingMode.NONE, "1249.5"),
        ],
    )
    def test_modes_to_rupee(self, value, mode, expected):
        assert round_money(value, mode) == Decimal(expected)

    def test_mode_accepts_string(self):
        assert round_money("10.5", "CEIL") == Decimal("11")

    def test_paisa_precision(self):
        assert round_money("1249.555", RoundingMode.HALF_UP, PAISA) == Decimal("1249.56")

    def test_unknown_mode(self):
        with pytest.raises(ValueError):
            round_money("10", "ROUND_ANYHOW")


class TestApplyRate:
    def test_percent_is_not_rounded(self):
        assert apply_rate(15000, "8.33") == Decimal("1249.5")

    def test_twelve_percent(self):
        assert apply_rate("25000", 12) == Decimal("3000")


class TestFormatInr:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, "₹0"),
            (999, "₹999"),
            (1000, "₹1,000"),
            (100000, "₹1,00,000"),
            ("1234567.5", "₹12,34,567.50"),
            ("123456789", "₹12,34,56,789"),
            (-2700, "-₹2,700"),
            ("0.005", "₹0.01"),
        ],
    )
    def test_indian_grouping(self, value, expected):
        assert format_inr(value) == expected

    def test_without_symbol(self):
        assert format_inr(125000, with_symbol=False) == "1,25,000"
