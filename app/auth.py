import time
import bcrypt
import jwt
from fastapi import Header, HTTPException
from .config import JWT_SECRET
from .models import Session, User


def _password_bytes(password: str) -> bytes:
    encoded = password.encode("utf-8")
    if len(encoded) > 72:
        raise ValueError("Password must be at most 72 UTF-8 bytes")
    return encoded


def hash_pw(p: str) -> str:
    return bcrypt.hashpw(_password_bytes(p), bcrypt.gensalt()).decode()


def check_pw(p: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(_password_bytes(p), h.encode())
    except (ValueError, TypeError):
        return False


def make_token(uid: int) -> str:
    return jwt.encode({"sub": str(uid), "exp": int(time.time()) + 86400}, JWT_SECRET, "HS256")


async def user_from_token(token: str):
    try:
        uid = int(jwt.decode(token, JWT_SECRET, algorithms=["HS256"])["sub"])
    except Exception:
        return None
    async with Session() as s:
        return await s.get(User, uid)


async def current_user(authorization: str = Header(default="")):
    u = await user_from_token(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "Invalid or expired token")
    return u
