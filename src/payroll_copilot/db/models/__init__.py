"""All ORM models. Importing this package registers every table on Base.metadata."""

from payroll_copilot.db.models.actions import ActionRequest
from payroll_copilot.db.models.adjustments import OvertimeEntry, PayAdjustment
from payroll_copilot.db.models.audit import AuditLog
from payroll_copilot.db.models.audit_run import AuditRun
from payroll_copilot.db.models.batch import (
    BatchItem,
    BatchLopItem,
    PayrollBatch,
    ValidationResult,
)
from payroll_copilot.db.models.chat import ChatMessage, ChatSession
from payroll_copilot.db.models.employee import Employee
from payroll_copilot.db.models.knowledge import KnowledgeChunk, KnowledgeDocument
from payroll_copilot.db.models.payroll import (
    Attendance,
    Leave,
    LopRecord,
    PayrollRun,
    SalaryStructure,
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
from payroll_copilot.db.models.statutory import CompanySetting, PfCalculationAudit, StatutoryRate

__all__ = [
    "ActionRequest", "Attendance", "AuditLog", "AuditRun", "BatchItem", "BatchLopItem",
    "ChatMessage", "ChatSession", "CompanySetting", "ESICRule", "Employee", "KnowledgeChunk",
    "KnowledgeDocument", "LWFRule", "Leave", "LopRecord", "OvertimeEntry", "PFRule", "PTRule",
    "PTSlab", "PayAdjustment", "PayrollBatch", "PayrollRun", "PfCalculationAudit",
    "SalaryStructure", "StatutoryRate", "TDSRule", "TDSSlab", "ValidationResult",
]  # fmt: skip
