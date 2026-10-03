"""Password hashing (bcrypt) and signed access tokens (JWT, HS256).

The token carries only the employee id and role. Every request reloads the
employee from the database, so a disabled login or changed role takes effect
immediately (not when the token expires).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from payroll_copilot.core.config import get_settings, jwt_signing_key
from payroll_copilot.core.exceptions import AuthError
from payroll_copilot.domain.enums import Role

ALGORITHM = "HS256"
ISSUER = "payroll-chatbot"
_MAX_PASSWORD_BYTES = 72  # bcrypt ignores anything longer; reject instead of truncating


def hash_password(password: str) -> str:
    raw = password.encode()
    if len(raw) > _MAX_PASSWORD_BYTES:
        raise ValueError(f"Password longer than {_MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    raw = password.encode()
    if len(raw) > _MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(raw, password_hash.encode())
    except ValueError:  # malformed hash in the DB
        return False


# A real hash to compare against when the email is unknown, so a wrong email and a
# wrong password take the same time (no user enumeration by timing).
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt()).decode()


def burn_password_check(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


@dataclass(frozen=True)
class TokenData:
    employee_id: int
    role: Role
    expires_at: datetime


def issue_token(employee_id: int, role: Role, now: datetime | None = None) -> tuple[str, datetime]:
    """Signed access token and its expiry time."""
    now = now or datetime.now(UTC)
    expires = now + timedelta(minutes=get_settings().jwt_expire_minutes)
    claims = {"sub": str(employee_id), "role": role.value, "iss": ISSUER,
              "iat": int(now.timestamp()), "exp": int(expires.timestamp())}  # fmt: skip
    return jwt.encode(claims, jwt_signing_key(), algorithm=ALGORITHM), expires


def decode_token(token: str) -> TokenData:
    try:
        claims = jwt.decode(
            token, jwt_signing_key(), algorithms=[ALGORITHM], issuer=ISSUER,
            options={"require": ["sub", "role", "exp", "iss"]},
        )  # fmt: skip
        return TokenData(employee_id=int(claims["sub"]), role=Role(claims["role"]),
                         expires_at=datetime.fromtimestamp(claims["exp"], UTC))  # fmt: skip
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Session expired, please log in again") from exc
    except (jwt.InvalidTokenError, ValueError, KeyError) as exc:
        raise AuthError("Invalid access token") from exc
