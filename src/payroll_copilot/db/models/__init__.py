"""All ORM models. Importing this package registers every table on Base.metadata."""

from payroll_copilot.db.models.adjustments import (
    Arrear,
    FnFSettlement,
    TDSDeclaration,
    TDSOverride,
)
from payroll_copilot.db.models.attendance import Attendance
from payroll_copilot.db.models.employee import Company, Employee
from payroll_copilot.db.models.governance import Approval, AuditLog, QAIssue, QARun
from payroll_copilot.db.models.payroll import (
    EmployerContribution,
    Payroll,
    PayrollComponent,
    PayrollDeduction,
    PayrollEarning,
    PayrollRun,
)
from payroll_copilot.db.models.rules import (
    ESICRule,
    LWFRule,
    PFRule,
    PTRule,
    PTSlab,
    TDSRule,
    TDSSlab,
)
from payroll_copilot.db.models.salary import (
    EmployeeSalaryComponent,
    EmployeeSalaryStructure,
    SalaryHead,
    SalaryStructure,
    SalaryStructureComponent,
)

__all__ = [
    "Approval", "Arrear", "Attendance", "AuditLog", "Company", "ESICRule", "Employee",
    "EmployeeSalaryComponent", "EmployeeSalaryStructure", "EmployerContribution",
    "FnFSettlement", "LWFRule", "PFRule", "PTRule", "PTSlab", "Payroll", "PayrollComponent",
    "PayrollDeduction", "PayrollEarning", "PayrollRun", "QAIssue", "QARun", "SalaryHead",
    "SalaryStructure", "SalaryStructureComponent", "TDSDeclaration", "TDSOverride", "TDSRule",
    "TDSSlab",
]  # fmt: skip
