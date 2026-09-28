"""Portable column types.

``DecimalType`` keeps money exact on every database: NUMERIC on PostgreSQL/MySQL,
and a decimal string on SQLite (whose NUMERIC affinity would turn values into
floats). Consequence on SQLite: do not ORDER BY / compare these columns in SQL;
filter money in Python instead.
"""

from decimal import Decimal

from sqlalchemy import Numeric, String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine

from payroll_copilot.core.money import to_decimal


class DecimalType(TypeDecorator[Decimal]):
    impl = Numeric
    cache_ok = True

    def __init__(self, precision: int = 14, scale: int = 2) -> None:
        super().__init__()
        self.precision = precision
        self.scale = scale

    def __repr__(self) -> str:
        # Alembic renders custom types with repr(); keep precision/scale in migrations.
        return f"DecimalType(precision={self.precision}, scale={self.scale})"

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(self.precision + 2))
        return dialect.type_descriptor(Numeric(self.precision, self.scale, asdecimal=True))

    def process_bind_param(self, value, dialect: Dialect):
        if value is None:
            return None
        amount = to_decimal(value)
        return str(amount) if dialect.name == "sqlite" else amount

    def process_result_value(self, value, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        return value if isinstance(value, Decimal) else Decimal(str(value))


Money = DecimalType(14, 2)  # rupees and paise
Rate = DecimalType(9, 4)  # percentages such as 8.3300
Days = DecimalType(6, 2)  # attendance days, half days allowed
