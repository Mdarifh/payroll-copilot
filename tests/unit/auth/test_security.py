from datetime import UTC, datetime, timedelta

import jwt
import pytest

from payroll_copilot.auth.security import (
    ALGORITHM,
    ISSUER,
    decode_token,
    hash_password,
    issue_token,
    verify_password,
)
from payroll_copilot.core.exceptions import AuthError
from payroll_copilot.domain.enums import Role
from tests.conftest import TEST_JWT_SECRET

FAR_FUTURE = 4102444800  # 2100-01-01


def test_hash_and_verify():
    hashed = hash_password("S3cret!pass")
    assert hashed != "S3cret!pass"
    assert verify_password("S3cret!pass", hashed)
    assert not verify_password("s3cret!pass", hashed)
    assert not verify_password("anything", "not-a-bcrypt-hash")


def test_overlong_password_rejected_not_truncated():
    with pytest.raises(ValueError, match="72 bytes"):
        hash_password("x" * 73)
    assert not verify_password("x" * 73, hash_password("x" * 72))


def test_token_round_trip():
    token, expires = issue_token(7, Role.HR)
    data = decode_token(token)
    assert (data.employee_id, data.role) == (7, Role.HR)
    assert abs((data.expires_at - expires).total_seconds()) < 1


def test_expired_token():
    token, _ = issue_token(7, Role.EMPLOYEE, now=datetime.now(UTC) - timedelta(days=2))
    with pytest.raises(AuthError, match="expired"):
        decode_token(token)


def _claims(**changes) -> dict:
    return {"sub": "7", "role": "hr", "iss": ISSUER, "exp": FAR_FUTURE, **changes}


@pytest.mark.parametrize("token", [
    jwt.encode(_claims(), "wrong-key-" + "k" * 30, algorithm=ALGORITHM),
    jwt.encode({k: v for k, v in _claims().items() if k != "iss"}, TEST_JWT_SECRET,
               algorithm=ALGORITHM),
    jwt.encode(_claims(role="superuser"), TEST_JWT_SECRET, algorithm=ALGORITHM),
    jwt.encode(_claims(), None, algorithm="none"),
    "not.a.token",
], ids=["wrong-key", "no-issuer", "unknown-role", "alg-none", "garbage"])  # fmt: skip
def test_forged_or_broken_tokens_rejected(token):
    with pytest.raises(AuthError, match="Invalid access token"):
        decode_token(token)
