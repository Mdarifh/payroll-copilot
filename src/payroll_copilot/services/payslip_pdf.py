"""Payslip PDF from the stored payslip JSON (same numbers as the payslip card).

Built-in PDF fonts have no ₹ glyph, so amounts are written as "Rs. 58,000.00".
"""

from decimal import Decimal
from typing import Any

from fpdf import FPDF

from payroll_copilot.core.config import get_settings
from payroll_copilot.core.money import format_inr


def _rs(value: str | Decimal) -> str:
    return format_inr(Decimal(str(value))).replace("₹", "Rs. ")


def _table(pdf: FPDF, title: str, lines: list[dict[str, Any]], total_label: str,
           total: str | None) -> None:  # fmt: skip
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(240, 242, 245)
    pdf.cell(0, 7, title, fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    for line in lines:
        pdf.cell(120, 6, line["name"])
        pdf.cell(0, 6, _rs(line["amount"]), align="R", new_x="LMARGIN", new_y="NEXT")
    if total is not None:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(120, 7, total_label, border="T")
        pdf.cell(0, 7, _rs(total), border="T", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)


def render_payslip_pdf(slip: dict[str, Any]) -> bytes:
    pdf = FPDF(format="A4")
    pdf.set_title(f"Payslip {slip['employee_code']} {slip['period_label']}")
    pdf.set_author(get_settings().company_name)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 9, get_settings().company_name, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 6, f"Payslip for {slip['period_label']}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font("Helvetica", "", 10)
    details = [
        ("Employee", f"{slip['employee_name']} ({slip['employee_code']})"),
        ("Designation", slip.get("designation") or "-"),
        ("Department", slip.get("department") or "-"),
        ("Paid days", f"{slip['paid_days']} of {slip['basis_days']}"),
        ("LOP days", slip["lop_days"]),
    ]
    for label, value in details:
        pdf.cell(35, 6, label)
        pdf.cell(0, 6, str(value), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    _table(pdf, "Earnings", slip["earnings"], "Gross earnings", slip["gross_earnings"])
    _table(pdf, "Deductions", slip["deductions"], "Total deductions", slip["total_deductions"])

    pdf.set_font("Helvetica", "B", 12)
    pdf.set_fill_color(225, 245, 234)
    pdf.cell(120, 9, "NET PAY", fill=True)
    pdf.cell(0, 9, _rs(slip["net_pay"]), fill=True, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    if slip.get("employer_contributions"):
        _table(pdf, "Employer contributions (not deducted from net pay)",
               slip["employer_contributions"], "Monthly CTC", slip.get("monthly_ctc"))  # fmt: skip

    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(110, 110, 110)
    pdf.multi_cell(0, 4, "Computer-generated payslip from demo data; no signature required. "
                         "Statutory rule versions: " + ", ".join(slip.get("rule_versions", [])))
    return bytes(pdf.output())
