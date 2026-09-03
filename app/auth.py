"""密码哈希、签名 Token 与权限依赖。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from app.config import settings
from app.database import connect, row_dict

PBKDF2_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("密码至少需要 8 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text + "==")
        expected = base64.urlsafe_b64decode(digest_text + "==")
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def create_token(user_id: int) -> str:
    payload = json.dumps(
        {
            "sub": user_id,
            "exp": int(time.time()) + settings.auth_token_expire_minutes * 60,
        },
        separators=(",", ":"),
    ).encode()
    encoded = _b64(payload)
    signature = _b64(hmac.new(settings.auth_secret_key.encode(), encoded.encode(), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def decode_token(token: str) -> int:
    try:
        encoded, signature = token.split(".", 1)
        expected = _b64(
            hmac.new(settings.auth_secret_key.encode(), encoded.encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=="))
        if int(payload["exp"]) < int(time.time()):
            raise ValueError
        return int(payload["sub"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="登录状态无效或已过期") from exc


def current_user(authorization: Annotated[str | None, Header()] = None) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="需要登录")
    user_id = decode_token(authorization.split(" ", 1)[1].strip())
    connection = connect()
    try:
        user = row_dict(
            connection.execute(
                "SELECT id, username, display_name, role, is_active, created_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        )
    finally:
        connection.close()
    if not user or not user["is_active"]:
        raise HTTPException(status_code=401, detail="账号不存在或已停用")
    return user


def admin_user(user: Annotated[dict, Depends(current_user)]) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user

