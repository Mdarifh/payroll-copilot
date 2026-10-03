"""Salary domain objects used by calculations (decoupled from the ORM)."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from payroll_copilot.domain.enums import AssignmentReason, CalcType, HeadCategory, HeadType


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)


class SalaryHeadInfo(_Frozen):
    code: str
    name: str
    category: HeadCategory
    head_type: HeadType
    is_taxable: bool = False
    is_pf_wage: bool = False
    is_esic_wage: bool = False
    is_pt_wage: bool = False
    is_lwf_wage: bool = False
    is_prorated: bool = False
    part_of_gross: bool = False
    part_of_ctc: bool = False
    display_order: int = 0
    # Employee Directory: which tab shows the head, and its income-tax treatment
    display_group: str | None = None  # EARNINGS | ALLOWANCES
    tax_treatment: str | None = None  # TAXABLE | EXEMPT_WITH_LIMIT | EXEMPT_ON_BILLS
    tax_note: str | None = None


class StructureComponentSpec(_Frozen):
    head_code: str
    calc_type: CalcType
    value: Decimal | None = None
    base_head_code: str | None = None
    sequence: int = 0


class StructureTemplate(_Frozen):
    code: str
    version: int
    name: str
    components: list[StructureComponentSpec]


class PayLine(_Frozen):
    """One amount against one head (earning, deduction or employer contribution)."""

    head_code: str
    amount: Decimal
    is_arrear: bool = False


class SalaryAssignmentInfo(_Frozen):
    """An employee's structure and full-month amounts for a date range."""

    structure: StructureTemplate
    monthly_gross: Decimal
    annual_ctc: Decimal | None = None
    effective_from: date
    effective_to: date | None = None
    reason: AssignmentReason
    components: list[PayLine]

    def amount(self, head_code: str) -> Decimal:
        return next((c.amount for c in self.components if c.head_code == head_code), Decimal(0))


class BucketLine(_Frozen):
    head_code: str
    name: str
    amount: Decimal
    is_arrear: bool = False


class PaySummary(_Frozen):
    """Pay lines grouped into the three buckets, with totals.

    Net pay = Gross earnings - Employee deductions. Employer contributions are
    never subtracted from net pay; they only add to CTC.
    """

    earnings: list[BucketLine]
    deductions: list[BucketLine]
    employer_contributions: list[BucketLine]
    gross_earnings: Decimal
    total_deductions: Decimal
    net_pay: Decimal
    total_employer_contributions: Decimal
    monthly_ctc: Decimal
    wage_bases: dict[str, Decimal] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
