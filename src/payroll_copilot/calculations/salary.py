"""Salary (earnings) engine: prorate structure components by paid days.

For each salary segment (one structure assignment active in the month):

    segment paid days = segment payable days − LOP allocated to the segment
    prorated head     = full-month amount × segment paid days / basis days
    non-prorated head = full-month amount (latest segment only)

Each line is rounded individually; gross = Σ rounded lines flagged part_of_gross.
LOP reversal days are paid as separate arrear lines at the earlier month's rate.
Statutory deductions and employer contributions are added by their own engines.
"""

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from payroll_copilot.calculations.trace import CalculationResult, TraceStep
from payroll_copilot.core.exceptions import StructureError
from payroll_copilot.core.money import PAISA, RUPEE, RoundingMode, round_money
from payroll_copilot.domain.enums import HeadCategory, LopAllocation, PaidDaysBasis
from payroll_copilot.domain.salary import PayLine, SalaryHeadInfo

ZERO = Decimal(0)


class SalarySegmentInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    active_from: date
    active_to: date
    monthly_gross: Decimal
    components: list[PayLine]

    @property
    def active_days(self) -> int:
        return (self.active_to - self.active_from).days + 1


class LopReversalInput(BaseModel):
    """LOP wrongly deducted in ``period``, to be paid back at that month's rate."""

    model_config = ConfigDict(frozen=True)

    period: str
    days: Decimal
    basis_days: Decimal
    components: list[PayLine]


class SegmentProration(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    active_from: date
    active_to: date
    monthly_gross: Decimal
    payable_days: Decimal
    lop_days: Decimal
    paid_days: Decimal


EarningKind = Literal["REGULAR", "LOP_REVERSAL", "ARREAR", "VARIABLE"]


class EarningLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    head_code: str
    name: str
    source: str  # segment label, "LOP reversal <period>", "Arrear <period>" or "variable"
    full_month_amount: Decimal
    paid_days: Decimal | None
    basis_days: Decimal | None
    is_prorated: bool
    raw_amount: Decimal
    amount: Decimal
    is_arrear: bool = False
    formula: str
    kind: EarningKind = "REGULAR"


def merge_lines(lines: Sequence[EarningLine]) -> list[PayLine]:
    """One PayLine per (head, arrear flag), amounts summed."""
    merged: dict[tuple[str, bool], Decimal] = {}
    for line in lines:
        key = (line.head_code, line.is_arrear)
        merged[key] = merged.get(key, ZERO) + line.amount
    return [PayLine(head_code=h, amount=a, is_arrear=arr) for (h, arr), a in merged.items()]


class SalaryResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_code: str | None
    period: str
    basis: PaidDaysBasis
    basis_days: Decimal
    paid_days: Decimal
    segments: list[SegmentProration]
    lines: list[EarningLine]
    earnings: list[PayLine]  # merged per head (+ arrear flag), feeds pay summary
    gross_earnings: Decimal
    full_month_gross: Decimal
    reduction: dict[str, Decimal]
    trace: CalculationResult
    warnings: list[str] = Field(default_factory=list)

    def earnings_excluding(self, *kinds: EarningKind) -> list[PayLine]:
        """Merged earnings without the given line kinds (e.g. increment ARREAR for PF)."""
        return merge_lines([line for line in self.lines if line.kind not in kinds])


# ------------------------------------------------------------------ helpers


def _segment_payable_days(
    segments: Sequence[SalarySegmentInput], b: Mapping[str, Decimal], basis: PaidDaysBasis
) -> list[Decimal]:
    payable_total = b["payable_days"]
    if len(segments) == 1:
        return [payable_total]
    if basis is PaidDaysBasis.WORKING_DAYS:
        raise StructureError(
            "Mid-month salary change on WORKING_DAYS basis needs a work calendar per segment"
        )
    covered = sum(s.active_days for s in segments)
    if Decimal(covered) != b["employed_days"]:
        raise StructureError(
            f"Salary structures cover {covered} of {b['employed_days']} employed days "
            "(gap or overlap in salary assignments)"
        )
    if basis is PaidDaysBasis.CALENDAR:
        return [Decimal(s.active_days) for s in segments]
    shares = [
        round_money(
            b["basis_days"] * s.active_days / b["calendar_days"], RoundingMode.HALF_UP, PAISA
        )
        for s in segments[:-1]
    ]
    return [*shares, payable_total - sum(shares, ZERO)]


def _allocate_lop(
    lop: Decimal, payable: list[Decimal], allocation: LopAllocation
) -> list[Decimal]:
    if len(payable) == 1:
        return [lop]
    if lop == 0:
        return [ZERO] * len(payable)
    if allocation is LopAllocation.LAST_SEGMENT:
        result = [ZERO] * len(payable)
        remaining = lop
        for i in reversed(range(len(payable))):
            take = min(remaining, payable[i])
            result[i], remaining = take, remaining - take
        return result
    total = sum(payable, ZERO)
    shares = [round_money(lop * p / total, RoundingMode.HALF_UP, PAISA) for p in payable[:-1]]
    return [*shares, lop - sum(shares, ZERO)]


def _prorated_gross(components: Sequence[PayLine], heads: Mapping[str, SalaryHeadInfo]) -> Decimal:
    return sum(
        (c.amount for c in components
         if heads[c.head_code].is_prorated and heads[c.head_code].part_of_gross),
        ZERO,
    )  # fmt: skip


def _order(head_code: str, heads: Mapping[str, SalaryHeadInfo]) -> tuple[int, str]:
    head = heads.get(head_code)
    return (head.display_order if head else 10_000, head_code)


def _earning_head(head_code: str, heads: Mapping[str, SalaryHeadInfo]) -> SalaryHeadInfo:
    head = heads.get(head_code)
    if head is None:
        raise StructureError(f"Unknown salary head {head_code}")
    if head.category is not HeadCategory.EARNING:
        raise StructureError(f"{head_code} is a {head.category}, not an earning")
    return head


# ------------------------------------------------------------------ engine


def calculate_earnings(
    segments: Sequence[SalarySegmentInput],
    paid_days: CalculationResult,
    heads: Mapping[str, SalaryHeadInfo],
    *,
    basis: PaidDaysBasis,
    rounding_mode: RoundingMode = RoundingMode.HALF_UP,
    lop_allocation: LopAllocation = LopAllocation.LAST_SEGMENT,
    lop_reversal: LopReversalInput | None = None,
    variable_earnings: Sequence[PayLine] = (),
    arrear_lines: Sequence[EarningLine] = (),
    employee_code: str | None = None,
) -> SalaryResult:
    """``arrear_lines`` are increment arrears computed by the arrear engine (kind ARREAR)."""
    if paid_days.component != "PAID_DAYS":
        raise StructureError("paid_days must be the result of compute_paid_days")
    b = paid_days.breakdown
    basis_days, lop = b["basis_days"], b["lop_days"]
    segments = sorted(segments, key=lambda s: s.active_from)
    for prev, nxt in zip(segments, segments[1:], strict=False):
        if nxt.active_from <= prev.active_to:
            raise StructureError(f"Salary segments overlap: {prev.label} and {nxt.label}")
    warnings: list[str] = []
    steps: list[TraceStep] = []
    lines: list[EarningLine] = []

    if not segments and b["payable_days"] > 0:
        raise StructureError(f"No salary structure for {b['payable_days']} payable days")

    payable = _segment_payable_days(segments, b, basis) if segments else []
    seg_lop = _allocate_lop(lop, payable, lop_allocation) if segments else []
    if any(p < 0 for p in payable):
        raise StructureError(f"Negative segment payable days {payable}")
    if len(segments) > 1 and lop > 0:
        warnings.append(
            f"LOP {lop} days allocated across salary segments using {lop_allocation.value}; "
            "confirm this matches company policy"
        )

    prorations: list[SegmentProration] = []
    for index, (seg, seg_payable, seg_lop_days) in enumerate(
        zip(segments, payable, seg_lop, strict=True)
    ):
        seg_paid = seg_payable - seg_lop_days
        prorations.append(
            SegmentProration(
                label=seg.label, active_from=seg.active_from, active_to=seg.active_to,
                monthly_gross=seg.monthly_gross, payable_days=seg_payable,
                lop_days=seg_lop_days, paid_days=seg_paid,
            )  # fmt: skip
        )
        steps.append(
            TraceStep(
                label=f"Segment {seg.label} ({seg.active_from} to {seg.active_to})",
                formula=f"payable {seg_payable} − LOP {seg_lop_days}",
                values={"monthly_gross": seg.monthly_gross, "basis_days": basis_days},
                result=seg_paid,
                note="paid days",
                unit="DAYS",
            )
        )
        is_last = index == len(segments) - 1
        for comp in sorted(seg.components, key=lambda c: _order(c.head_code, heads)):
            head = _earning_head(comp.head_code, heads)
            if head.is_prorated:
                raw = comp.amount * seg_paid / basis_days
                formula = f"{comp.amount} × {seg_paid} / {basis_days}"
            elif is_last:
                raw = comp.amount
                formula = f"{comp.amount} (not prorated)"
            else:
                warnings.append(
                    f"{comp.head_code} is not prorated; paid once from the latest segment"
                )
                continue
            amount = round_money(raw, rounding_mode, RUPEE)
            lines.append(
                EarningLine(
                    head_code=head.code, name=head.name, source=seg.label,
                    full_month_amount=comp.amount,
                    paid_days=seg_paid if head.is_prorated else None,
                    basis_days=basis_days if head.is_prorated else None,
                    is_prorated=head.is_prorated, raw_amount=raw, amount=amount,
                    formula=formula,
                )  # fmt: skip
            )

    reversal_total = ZERO
    if lop_reversal and lop_reversal.days > 0:
        source = f"LOP reversal {lop_reversal.period}"
        for comp in sorted(lop_reversal.components, key=lambda c: _order(c.head_code, heads)):
            head = _earning_head(comp.head_code, heads)
            if not head.is_prorated:
                continue
            raw = comp.amount * lop_reversal.days / lop_reversal.basis_days
            amount = round_money(raw, rounding_mode, RUPEE)
            reversal_total += amount
            lines.append(
                EarningLine(
                    head_code=head.code, name=head.name, source=source,
                    full_month_amount=comp.amount, paid_days=lop_reversal.days,
                    basis_days=lop_reversal.basis_days, is_prorated=True, raw_amount=raw,
                    amount=amount, is_arrear=True, kind="LOP_REVERSAL",
                    formula=f"{comp.amount} × {lop_reversal.days} / {lop_reversal.basis_days}",
                )  # fmt: skip
            )

    arrear_total = ZERO
    for arrear in arrear_lines:
        if arrear.kind != "ARREAR" or not arrear.is_arrear:
            raise StructureError(f"{arrear.head_code}: arrear lines must be kind ARREAR")
        _earning_head(arrear.head_code, heads)
        arrear_total += arrear.amount if heads[arrear.head_code].part_of_gross else ZERO
        lines.append(arrear)

    variable_total = ZERO
    for extra in variable_earnings:
        head = _earning_head(extra.head_code, heads)
        variable_total += extra.amount if head.part_of_gross else ZERO
        lines.append(
            EarningLine(
                head_code=head.code, name=head.name, source="variable",
                full_month_amount=extra.amount, paid_days=None, basis_days=None,
                is_prorated=False, raw_amount=extra.amount, amount=extra.amount,
                is_arrear=extra.is_arrear, formula=f"{extra.amount} (variable, as given)",
                kind="VARIABLE",
            )  # fmt: skip
        )

    for line in lines:
        note = None if line.raw_amount == line.amount else f"raw {line.raw_amount:.4f}"
        steps.append(
            TraceStep(
                label=f"{line.name} [{line.source}]", formula=line.formula,
                result=line.amount, note=note,
            )  # fmt: skip
        )

    earnings = merge_lines(lines)
    gross =sum((line.amount for line in lines if heads[line.head_code].part_of_gross), ZERO)
    steps.append(TraceStep(label="Gross earnings", formula="Σ rounded earning lines", result=gross))

    # --- why is gross below the full-month salary? -------------------------------
    full_month_gross = segments[-1].monthly_gross if segments else ZERO
    ref = _prorated_gross(segments[-1].components, heads) if segments else ZERO
    lop_loss = sum(
        (p.lop_days * _prorated_gross(s.components, heads) / basis_days
         for s, p in zip(segments, prorations, strict=True)),
        ZERO,
    )  # fmt: skip
    not_employed_loss = (basis_days - b["payable_days"]) * ref / basis_days if segments else ZERO
    revision_loss = sum(
        (p.payable_days * (ref - _prorated_gross(s.components, heads)) / basis_days
         for s, p in zip(segments, prorations, strict=True)),
        ZERO,
    )  # fmt: skip
    earned = gross - reversal_total - variable_total - arrear_total
    rounding_diff = earned - (full_month_gross - lop_loss - not_employed_loss - revision_loss)

    def q(value: Decimal) -> Decimal:
        return round_money(value, RoundingMode.HALF_UP, PAISA)

    reduction = {
        "full_month_gross": full_month_gross,
        "reduction_lop": q(lop_loss),
        "reduction_not_employed": q(not_employed_loss),
        "reduction_mid_month_revision": q(revision_loss),
        "rounding_difference": q(rounding_diff),
        "earned_gross": earned,
        "lop_reversal": reversal_total,
        "arrears": arrear_total,
        "variable_earnings": variable_total,
    }

    trace = CalculationResult(
        component="GROSS_EARNINGS",
        employee_code=employee_code,
        period=paid_days.period,
        inputs={
            "basis": basis.value, "basis_days": basis_days, "paid_days": paid_days.value,
            "lop_allocation": lop_allocation.value,
            "segments": [s.label for s in segments],
        },  # fmt: skip
        formula="Σ (full-month amount × paid days / basis days) per earning head, rounded per line",
        steps=steps,
        raw_value=sum((line.raw_amount for line in lines), ZERO),
        value=gross,
        rounding_mode=rounding_mode,
        breakdown={
            f"{p.head_code}{' (arrear)' if p.is_arrear else ''}": p.amount for p in earnings
        },
        warnings=warnings,
    )
    return SalaryResult(
        employee_code=employee_code,
        period=paid_days.period,
        basis=basis,
        basis_days=basis_days,
        paid_days=paid_days.value,
        segments=prorations,
        lines=lines,
        earnings=earnings,
        gross_earnings=gross,
        full_month_gross=full_month_gross,
        reduction=reduction,
        trace=trace,
        warnings=warnings,
    )
