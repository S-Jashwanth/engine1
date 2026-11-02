import time

import jwt
import pytest

from app.auth import check_pw, hash_pw, make_token, _password_bytes
from app.config import JWT_SECRET


def test_password_hash_and_verify():
    encoded = hash_pw("A-strong-password-123")
    assert encoded != "A-strong-password-123"
    assert check_pw("A-strong-password-123", encoded)
    assert not check_pw("wrong-password", encoded)


def test_password_byte_limit():
    with pytest.raises(ValueError):
        _password_bytes("é" * 37)  # 74 UTF-8 bytes


def test_jwt_contains_user_and_expiry():
    token = make_token(123)
    payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    assert payload["sub"] == "123"
    assert payload["exp"] > int(time.time())
