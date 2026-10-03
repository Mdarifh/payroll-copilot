"""Project-wide exception hierarchy.

Every error raised by our own code derives from PayrollCopilotError so that
API/agent layers can catch domain errors without swallowing programming bugs.
"""


class PayrollCopilotError(Exception):
    """Base class for all application errors."""


class ConfigurationError(PayrollCopilotError):
    """Invalid or incomplete application configuration."""


class MoneyError(PayrollCopilotError):
    """Invalid monetary value or rounding request."""


class PeriodError(PayrollCopilotError):
    """Invalid payroll period or financial year."""


class CalculationError(PayrollCopilotError):
    """Inputs to a calculation are invalid or inconsistent."""


class StructureError(CalculationError):
    """Salary structure template or pay lines are inconsistent."""


class AttendanceError(CalculationError):
    """Attendance data is invalid (negative counts, LOP beyond payable days...)."""


class NotFoundError(PayrollCopilotError):
    """Requested employee / structure / record does not exist."""


class RuleError(PayrollCopilotError):
    """Base class for rule configuration and resolution problems."""


class RuleConfigError(RuleError):
    """A rule file is invalid (schema, overlapping versions, bad slabs...)."""


class RuleImmutableError(RuleError):
    """Attempt to change the values of a VERIFIED rule version in place."""


class ApplicabilityError(RuleError):
    """An applicability condition cannot be evaluated (unknown field/operator)."""


class RuleNotFoundError(RuleError):
    """No rule version applies to the employee and period."""


class AmbiguousRuleError(RuleError):
    """More than one rule version applies to the same dates; the resolver never guesses."""


class MidPeriodRuleChangeError(RuleError):
    """Different rule versions apply to different parts of the period; caller must split."""

    def __init__(self, message: str, rules: list) -> None:
        super().__init__(message)
        self.rules = rules


class UnverifiedRuleError(RuleError):
    """Only an UNVERIFIED rule applies and ALLOW_UNVERIFIED_RULES is false."""


class AuthError(PayrollCopilotError):
    """Login failed or the access token is missing / invalid / expired (HTTP 401)."""


class PermissionDeniedError(PayrollCopilotError):
    """The logged-in user's role may not do this (HTTP 403)."""
