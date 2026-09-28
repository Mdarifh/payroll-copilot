"""Pydantic schemas for rule YAML files (``config/rules/**.yaml``).

One file = one rule family (``rule_code``) with one or more dated versions.
Validation here catches config mistakes (overlapping versions, gaps in slabs,
rates above 100%) before anything reaches the database.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from payroll_copilot.core.dates import FinancialYear, date_ranges_overlap
from payroll_copilot.core.money import RoundingMode
from payroll_copilot.domain.enums import (
    AgeCategory,
    CeilingProration,
    Frequency,
    Gender,
    RuleStatus,
    TaxRegime,
    WageBasis,
)
from payroll_copilot.rules.applicability import SUPPORTED_OPERATORS

Percent = Annotated[Decimal, Field(ge=0, le=100)]
Amount = Annotated[Decimal, Field(ge=0)]
Month = Annotated[int, Field(ge=1, le=12)]
ALL_MONTHS = list(range(1, 13))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConditionConfig(StrictModel):
    field: str
    op: str
    value: Any

    @model_validator(mode="after")
    def _known_operator(self) -> "ConditionConfig":
        if self.op not in SUPPORTED_OPERATORS:
            raise ValueError(f"unsupported operator {self.op!r}")
        return self


class VersionBase(StrictModel):
    version: PositiveInt
    name: str
    effective_from: date
    effective_to: date | None = None
    status: RuleStatus = RuleStatus.UNVERIFIED
    source_ref: str | None = None
    notes: str | None = None
    applicability: list[ConditionConfig] = []
    rounding_mode: RoundingMode = RoundingMode.HALF_UP

    @model_validator(mode="after")
    def _valid_range(self) -> "VersionBase":
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError(f"version {self.version}: effective_to before effective_from")
        return self


def _check_upto_sequence(uptos: list[Decimal | None], label: str) -> None:
    if not uptos:
        raise ValueError(f"{label}: at least one slab required")
    if uptos[-1] is not None:
        raise ValueError(f"{label}: last slab must have upto: null (no upper limit)")
    bounded = uptos[:-1]
    if any(u is None for u in bounded):
        raise ValueError(f"{label}: only the last slab may have upto: null")
    if any(b <= a for a, b in zip(bounded, bounded[1:], strict=False)):
        raise ValueError(f"{label}: slab 'upto' values must be strictly increasing")


# ------------------------------------------------------------------ PF / ESIC


class PFVersion(VersionBase):
    employee_rate: Percent
    employer_rate: Percent
    eps_rate: Percent
    edli_rate: Percent
    admin_rate: Percent
    admin_min_amount: Amount | None = None
    pf_wage_ceiling: Amount
    eps_wage_ceiling: Amount
    edli_wage_ceiling: Amount
    eps_max_age: PositiveInt | None = None
    ceiling_proration: CeilingProration = CeilingProration.NONE

    @model_validator(mode="after")
    def _eps_within_employer_share(self) -> "PFVersion":
        if self.eps_rate > self.employer_rate:
            raise ValueError("eps_rate cannot exceed employer_rate")
        return self


class ESICVersion(VersionBase):
    employee_rate: Percent
    employer_rate: Percent
    wage_threshold: Amount
    disabled_wage_threshold: Amount | None = None
    contribution_period_start_months: list[Month]


# ------------------------------------------------------------------ PT / LWF


class PTSlabConfig(StrictModel):
    upto: Amount | None
    amount: Amount
    month_overrides: dict[Month, Amount] = {}


class PTSlabSet(StrictModel):
    gender: Gender | None = None  # None => every gender not listed separately
    slabs: list[PTSlabConfig]

    @model_validator(mode="after")
    def _slabs_contiguous(self) -> "PTSlabSet":
        _check_upto_sequence([s.upto for s in self.slabs], f"gender={self.gender}")
        return self


class PTVersion(VersionBase):
    is_levied: bool = True
    frequency: Frequency = Frequency.MONTHLY
    wage_basis: WageBasis = WageBasis.MONTHLY
    deduction_months: list[Month] | None = None
    slab_sets: list[PTSlabSet] = []

    @model_validator(mode="after")
    def _consistent(self) -> "PTVersion":
        if not self.is_levied:
            if self.slab_sets:
                raise ValueError("is_levied: false must not define slab_sets")
            return self
        if not self.slab_sets:
            raise ValueError("is_levied: true requires slab_sets")
        genders = [s.gender for s in self.slab_sets]
        if len(genders) != len(set(genders)):
            raise ValueError("duplicate gender in slab_sets")
        if None not in genders:
            raise ValueError("one slab_set must have gender: null (default for all)")
        if self.frequency is not Frequency.MONTHLY and not self.deduction_months:
            raise ValueError(f"frequency {self.frequency} requires deduction_months")
        return self

    @property
    def effective_deduction_months(self) -> list[int]:
        if not self.is_levied:
            return []
        return sorted(self.deduction_months or ALL_MONTHS)


class LWFVersion(VersionBase):
    frequency: Frequency
    deduction_months: list[Month]
    employee_amount: Amount | None = None
    employer_amount: Amount | None = None
    employee_rate: Percent | None = None
    employee_max_amount: Amount | None = None
    employer_multiplier: Amount | None = None
    wage_upper_limit: Amount | None = None

    @model_validator(mode="after")
    def _fixed_or_rate(self) -> "LWFVersion":
        fixed = self.employee_amount is not None and self.employer_amount is not None
        rate = self.employee_rate is not None and self.employer_multiplier is not None
        if fixed == rate:
            raise ValueError(
                "define either employee_amount+employer_amount or employee_rate+employer_multiplier"
            )
        return self


# ------------------------------------------------------------------ TDS


class TaxSlabConfig(StrictModel):
    upto: Amount | None
    rate: Percent


class SurchargeConfig(StrictModel):
    above: Amount
    rate: Percent


class TDSVersion(VersionBase):
    financial_year: str
    regime: TaxRegime
    age_category: AgeCategory | None = None  # None => all ages
    standard_deduction: Amount
    rebate_income_limit: Amount | None = None
    rebate_max: Amount | None = None
    rebate_marginal_relief: bool = False
    cess_rate: Percent
    surcharge: list[SurchargeConfig] = []
    slabs: list[TaxSlabConfig]
    deduction_limits: dict[str, Amount] = {}
    allow_professional_tax_deduction: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> "TDSVersion":
        fy = FinancialYear.parse(self.financial_year)
        if self.effective_from != fy.start or self.effective_to != fy.end:
            raise ValueError(f"TDS version dates must equal FY {fy.label} ({fy.start}..{fy.end})")
        _check_upto_sequence([s.upto for s in self.slabs], "tax slabs")
        aboves = [s.above for s in self.surcharge]
        if aboves != sorted(set(aboves)):
            raise ValueError("surcharge 'above' values must be strictly increasing")
        return self


# ------------------------------------------------------------------ files


class RuleFileBase(StrictModel):
    rule_code: str = Field(pattern=r"^[A-Z0-9_-]{2,32}$")
    versions: list[Any]

    @model_validator(mode="after")
    def _versions_consistent(self) -> "RuleFileBase":
        versions = self.versions
        if not versions:
            raise ValueError("at least one version required")
        numbers = [v.version for v in versions]
        if numbers != sorted(set(numbers)):
            raise ValueError("version numbers must be unique and increasing")
        for a, b in zip(versions, versions[1:], strict=False):
            if date_ranges_overlap(a.effective_from, a.effective_to, b.effective_from, None):
                raise ValueError(
                    f"{self.rule_code}: version {a.version} overlaps version {b.version}; "
                    "each version must end before the next starts"
                )
        return self

    def family_fields(self) -> dict[str, Any]:
        """Columns shared by all versions (e.g. state for PT/LWF)."""
        return {}


class PFRuleFile(RuleFileBase):
    rule_type: Literal["PF"]
    versions: list[PFVersion]


class ESICRuleFile(RuleFileBase):
    rule_type: Literal["ESIC"]
    versions: list[ESICVersion]


class _StateRuleFile(RuleFileBase):
    state_code: str = Field(pattern=r"^[A-Z]{2,4}$")
    state_name: str

    def family_fields(self) -> dict[str, Any]:
        return {"state_code": self.state_code, "state_name": self.state_name}


class PTRuleFile(_StateRuleFile):
    rule_type: Literal["PT"]
    versions: list[PTVersion]


class LWFRuleFile(_StateRuleFile):
    rule_type: Literal["LWF"]
    versions: list[LWFVersion]


class TDSRuleFile(RuleFileBase):
    rule_type: Literal["TDS"]
    versions: list[TDSVersion]


RuleFile = Annotated[
    PFRuleFile | ESICRuleFile | PTRuleFile | LWFRuleFile | TDSRuleFile,
    Field(discriminator="rule_type"),
]
