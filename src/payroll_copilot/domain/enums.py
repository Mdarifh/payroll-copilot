"""Enumerations used across the project."""

from enum import StrEnum


class Gender(StrEnum):
    MALE = "M"
    FEMALE = "F"
    OTHER = "O"


class EmployeeStatus(StrEnum):
    ACTIVE = "ACTIVE"
    NOTICE = "NOTICE"
    SEPARATED = "SEPARATED"


class TaxRegime(StrEnum):
    NEW = "NEW"
    OLD = "OLD"


class AgeCategory(StrEnum):
    GENERAL = "GENERAL"  # below 60
    SENIOR = "SENIOR"  # 60 to 79
    SUPER_SENIOR = "SUPER_SENIOR"  # 80 and above


class HeadCategory(StrEnum):
    EARNING = "EARNING"
    DEDUCTION = "DEDUCTION"  # borne by employee, reduces net pay
    EMPLOYER_CONTRIBUTION = "EMPLOYER_CONTRIBUTION"  # borne by employer, part of CTC


class HeadType(StrEnum):
    FIXED = "FIXED"  # comes from salary structure
    VARIABLE = "VARIABLE"  # bonus/incentive, may change monthly
    STATUTORY = "STATUTORY"  # PF, ESIC, PT, LWF, TDS
    ADHOC = "ADHOC"  # loan, other one-off


class CalcType(StrEnum):
    FIXED = "FIXED"  # value = fixed monthly amount
    PERCENT_OF_GROSS = "PERCENT_OF_GROSS"  # value% of monthly fixed gross
    PERCENT_OF_HEAD = "PERCENT_OF_HEAD"  # value% of base_head_code
    BALANCING = "BALANCING"  # gross minus all other earnings
    STATUTORY = "STATUTORY"  # computed by statutory engine


class AssignmentReason(StrEnum):
    JOINING = "JOINING"
    INCREMENT = "INCREMENT"
    REVISION = "REVISION"


class PaidDaysBasis(StrEnum):
    CALENDAR = "CALENDAR"
    FIXED_30 = "FIXED_30"
    FIXED_26 = "FIXED_26"
    WORKING_DAYS = "WORKING_DAYS"


class LopAllocation(StrEnum):
    """Which salary segment absorbs LOP when salary changes within the month."""

    LAST_SEGMENT = "LAST_SEGMENT"  # latest segment first, overflow to earlier ones
    PROPORTIONAL = "PROPORTIONAL"  # in proportion to each segment's payable days




class RuleType(StrEnum):
    PF = "PF"
    ESIC = "ESIC"
    PT = "PT"
    LWF = "LWF"
    TDS = "TDS"


class RuleStatus(StrEnum):
    UNVERIFIED = "UNVERIFIED"  # entered but not checked against the official source
    VERIFIED = "VERIFIED"  # checked; immutable, change needs a new version
    RETIRED = "RETIRED"  # never used by the resolver


class Frequency(StrEnum):
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    HALF_YEARLY = "HALF_YEARLY"
    YEARLY = "YEARLY"


class WageBasis(StrEnum):
    """Period the slab wage limits are expressed in."""

    MONTHLY = "MONTHLY"
    HALF_YEARLY = "HALF_YEARLY"
    ANNUAL = "ANNUAL"


class CeilingProration(StrEnum):
    NONE = "NONE"  # ceiling applied as-is
    PAID_DAYS = "PAID_DAYS"  # ceiling × paid_days / basis_days






class ComparisonStatus(StrEnum):
    MATCH = "MATCH"
    WITHIN_TOLERANCE = "WITHIN_TOLERANCE"
    MISMATCH = "MISMATCH"
    MISSING_ACTUAL = "MISSING_ACTUAL"
    MISSING_EXPECTED = "MISSING_EXPECTED"


# ---------------------------------------------------------------------- chatbot


class Role(StrEnum):
    EMPLOYEE = "employee"
    MANAGER = "manager"  # sees own team's profiles, never amounts
    HR = "hr"  # full payroll access (same as ADMIN; kept for existing users)
    # payroll staff roles (permissions in auth/permissions.py)
    ADMIN = "admin"
    PAYROLL_MANAGER = "payroll_manager"
    PAYROLL_EXECUTIVE = "payroll_executive"
    VIEWER = "viewer"


class AttendanceStatus(StrEnum):
    """One employee-day. ABSENT without an approved leave is an attendance exception."""

    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"  # covered by an approved leave
    HALF_DAY = "HALF_DAY"
    WEEK_OFF = "WEEK_OFF"
    HOLIDAY = "HOLIDAY"


class LeaveType(StrEnum):
    CL = "CL"  # casual
    SL = "SL"  # sick
    EL = "EL"  # earned / privilege
    LOP = "LOP"  # leave without pay


class LeaveStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class PayrollRunStatus(StrEnum):
    PROCESSED = "PROCESSED"
    LOCKED = "LOCKED"  # paid out; changes need a new run


class BatchStatus(StrEnum):
    REVIEW = "REVIEW"  # HR is reviewing LOP; nothing processed yet
    QUEUED = "QUEUED"  # HR pressed run; waiting for the worker
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    CANCELLED = "CANCELLED"


FINISHED_BATCH = frozenset({BatchStatus.COMPLETED, BatchStatus.COMPLETED_WITH_ERRORS,
                            BatchStatus.CANCELLED})  # fmt: skip


class BatchItemStatus(StrEnum):
    PENDING = "PENDING"
    DONE = "DONE"  # payroll processed by this batch
    SKIPPED = "SKIPPED"  # already processed for the month before (idempotent)
    FAILED = "FAILED"
