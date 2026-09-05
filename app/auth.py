"""NetHub Accounts OIDC client and local opaque-session dependencies."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlparse

import httpx
from authlib.jose import JsonWebKey, JsonWebToken
from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app.config import settings
from app.database import row_dict, transaction, utc_now

SESSION_COOKIE = "cas_session"
LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"
RS256_JWT = JsonWebToken(["RS256"])


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _future(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def safe_return_path(value: str | None) -> str:
    if (
        not value
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        return "/"
    parsed = urlparse(value)
    return value if not parsed.scheme and not parsed.netloc else "/"


def browser_request_is_same_origin(request: Request) -> bool:
    if request.headers.get("sec-fetch-site", "").casefold() == "cross-site":
        return False
    origin = request.headers.get("origin")
    if not origin:
        # Non-browser clients generally omit Origin. Browsers include it for
        # unsafe cross-origin requests and also send Sec-Fetch-Site.
        return True
    expected = urlparse(settings.oidc_redirect_uri)
    actual = urlparse(origin)
    return (
        actual.scheme.casefold() == expected.scheme.casefold()
        and actual.netloc.casefold() == expected.netloc.casefold()
        and not actual.path.rstrip("/")
        and not actual.params
        and not actual.query
        and not actual.fragment
    )


def start_oidc_login(return_path: str) -> RedirectResponse:
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    with transaction(immediate=True) as connection:
        connection.execute("DELETE FROM oidc_login_states WHERE expires_at <= ?", (utc_now(),))
        connection.execute(
            """
            INSERT INTO oidc_login_states (state_hash, nonce, code_verifier, return_path, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (_token_hash(state), nonce, verifier, safe_return_path(return_path), _future(settings.oidc_state_expire_seconds)),
        )
    query = urlencode(
        {
            "response_type": "code", "client_id": settings.oidc_client_id,
            "redirect_uri": settings.oidc_redirect_uri, "scope": "openid profile",
            "state": state, "nonce": nonce, "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return RedirectResponse(f"{settings.oidc_issuer}/oauth/authorize?{query}", status_code=302)


def consume_login_state(state: str) -> dict:
    with transaction(immediate=True) as connection:
        item = row_dict(connection.execute(
            "SELECT * FROM oidc_login_states WHERE state_hash = ?", (_token_hash(state),)
        ).fetchone())
        if item:
            connection.execute("DELETE FROM oidc_login_states WHERE id = ?", (item["id"],))
    if not item or item["expires_at"] <= utc_now():
        raise HTTPException(status_code=400, detail="登录请求已过期或无效，请重新登录")
    return item


def _discovery() -> dict:
    try:
        response = httpx.get(f"{settings.oidc_issuer}/.well-known/openid-configuration", timeout=8.0)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="账号中心暂时不可用") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="账号中心返回了无效的发现文档")
    if data.get("issuer") != settings.oidc_issuer:
        raise HTTPException(status_code=502, detail="账号中心返回了无效的发现文档")
    for endpoint in ("token_endpoint", "userinfo_endpoint", "jwks_uri"):
        value = data.get(endpoint)
        if not isinstance(value, str) or not value.startswith("https://"):
            raise HTTPException(status_code=502, detail="账号中心发现文档缺少安全端点")
    return data


def _decode_id_token(id_token: str, nonce: str, discovery: dict) -> dict:
    try:
        response = httpx.get(discovery["jwks_uri"], timeout=8.0)
        response.raise_for_status()
        claims = RS256_JWT.decode(
            id_token,
            JsonWebKey.import_key_set(response.json()),
            claims_options={
                "iss": {"essential": True, "value": settings.oidc_issuer},
                "aud": {"essential": True, "value": settings.oidc_client_id},
                "exp": {"essential": True},
                "iat": {"essential": True},
                "sub": {"essential": True},
                "sid": {"essential": True},
                "auth_time": {"essential": True},
                "nonce": {"essential": True, "value": nonce},
            },
        )
        claims.validate(leeway=15)
        data = dict(claims)
    except (KeyError, ValueError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail="无法验证账号中心身份令牌") from exc
    except Exception as exc:
        raise HTTPException(status_code=401, detail="账号中心身份令牌无效") from exc
    audience = data.get("aud", [])
    if isinstance(audience, str):
        audience = [audience]
    if (
        data.get("iss") != settings.oidc_issuer
        or settings.oidc_client_id not in audience
        or data.get("nonce") != nonce
        or not isinstance(data.get("sub"), str)
        or not data["sub"]
        or not isinstance(data.get("sid"), str)
        or not data["sid"]
        or not isinstance(data.get("auth_time"), int)
    ):
        raise HTTPException(status_code=401, detail="账号中心身份令牌声明无效")
    return data


def complete_oidc_login(code: str, state: str) -> tuple[dict, str, str]:
    login_state = consume_login_state(state)
    discovery = _discovery()
    try:
        token_response = httpx.post(
            discovery["token_endpoint"], auth=(settings.oidc_client_id, settings.oidc_client_secret),
            data={"grant_type": "authorization_code", "code": code,
                  "redirect_uri": settings.oidc_redirect_uri,
                  "code_verifier": login_state["code_verifier"]}, timeout=8.0,
        )
        token_response.raise_for_status()
        token = token_response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="账号中心拒绝了本次登录") from exc
    if not isinstance(token, dict):
        raise HTTPException(status_code=401, detail="账号中心返回了无效的令牌响应")
    if not isinstance(token.get("id_token"), str) or not isinstance(token.get("access_token"), str):
        raise HTTPException(status_code=401, detail="账号中心返回了不完整的令牌")
    identity = _decode_id_token(token["id_token"], login_state["nonce"], discovery)
    try:
        info_response = httpx.get(
            discovery["userinfo_endpoint"], headers={"Authorization": f"Bearer {token['access_token']}"}, timeout=8.0,
        )
        info_response.raise_for_status()
        userinfo = info_response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="无法读取账号中心用户资料") from exc
    if not isinstance(userinfo, dict):
        raise HTTPException(status_code=401, detail="账号中心返回了无效的用户资料")
    if userinfo.get("sub") != identity["sub"]:
        raise HTTPException(status_code=401, detail="账号中心用户资料不匹配")
    identity["preferred_username"] = userinfo.get("preferred_username", identity.get("preferred_username"))
    identity["name"] = userinfo.get("name", identity.get("name"))
    user = provision_user(identity)
    return user, login_state["return_path"], create_local_session(user, identity.get("sid", ""))


def provision_user(identity: dict) -> dict:
    sub = identity["sub"]
    username = str(identity.get("preferred_username") or sub).strip()[:50] or sub
    display_name = str(identity.get("name") or username).strip()[:50] or username
    with transaction(immediate=True) as connection:
        user = row_dict(connection.execute("SELECT * FROM users WHERE auth_sub = ?", (sub,)).fetchone())
        if user:
            if not user["is_active"]:
                raise HTTPException(status_code=403, detail="本站成员资格已停用")
            return user
        candidate, suffix = username, 2
        while connection.execute("SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (candidate,)).fetchone():
            candidate = f"{username[:44]}-{suffix}"
            suffix += 1
        role = "admin" if sub in settings.cas_admin_subs else "user"
        cursor = connection.execute(
            """INSERT INTO users (username, display_name, password_hash, auth_sub, role, is_active, created_at)
               VALUES (?, ?, '', ?, ?, 1, ?)""",
            (candidate, display_name, sub, role, utc_now()),
        )
        return row_dict(connection.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone())


def create_local_session(user: dict, oidc_sid: str) -> str:
    raw_token, now = secrets.token_urlsafe(48), utc_now()
    with transaction() as connection:
        connection.execute("DELETE FROM local_sessions WHERE expires_at <= ?", (now,))
        connection.execute(
            """INSERT INTO local_sessions (token_hash, user_id, auth_sub, oidc_sid, created_at, last_seen_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (_token_hash(raw_token), user["id"], user["auth_sub"], oidc_sid, now, now, _future(settings.local_session_expire_seconds)),
        )
    return raw_token


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(SESSION_COOKIE, token, max_age=settings.local_session_expire_seconds,
                        httponly=True, secure=settings.oidc_cookie_secure, samesite="lax", path="/")


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=settings.oidc_cookie_secure, samesite="lax")


def current_user(request: Request) -> dict:
    raw_token = request.cookies.get(SESSION_COOKIE)
    if not raw_token:
        raise HTTPException(status_code=401, detail="需要登录")
    with transaction() as connection:
        user = row_dict(connection.execute(
            """SELECT u.id, u.username, u.display_name, u.auth_sub, u.role, u.is_active, u.created_at,
                      s.id AS session_id, s.expires_at
               FROM local_sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?""",
            (_token_hash(raw_token),),
        ).fetchone())
        now = utc_now()
        if user and user["expires_at"] > now and user["is_active"]:
            connection.execute(
                "UPDATE local_sessions SET last_seen_at = ? WHERE id = ?",
                (now, user["session_id"]),
            )
        elif user:
            connection.execute("DELETE FROM local_sessions WHERE id = ?", (user["session_id"],))
    if not user or user["expires_at"] <= utc_now() or not user["is_active"]:
        raise HTTPException(status_code=401, detail="登录状态无效或已过期")
    user.pop("session_id", None)
    user.pop("expires_at", None)
    return user


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def revoke_current_session(request: Request) -> None:
    raw_token = request.cookies.get(SESSION_COOKIE)
    if raw_token:
        with transaction() as connection:
            connection.execute("DELETE FROM local_sessions WHERE token_hash = ?", (_token_hash(raw_token),))


def revoke_backchannel_sessions(logout_token: str) -> int:
    discovery = _discovery()
    try:
        response = httpx.get(discovery["jwks_uri"], timeout=8.0)
        response.raise_for_status()
        claims = RS256_JWT.decode(
            logout_token,
            JsonWebKey.import_key_set(response.json()),
            claims_options={
                "iss": {"essential": True, "value": settings.oidc_issuer},
                "aud": {"essential": True, "value": settings.oidc_client_id},
                "iat": {"essential": True},
                "jti": {"essential": True},
                "sub": {"essential": True},
                "events": {"essential": True},
            },
        )
        claims.validate(leeway=15)
        data = dict(claims)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="无效的退出通知") from exc
    audience = data.get("aud", [])
    if isinstance(audience, str):
        audience = [audience]
    events = data.get("events")
    now_epoch = int(time.time())
    if (
        data.get("iss") != settings.oidc_issuer
        or settings.oidc_client_id not in audience
        or not isinstance(data.get("sub"), str)
        or not data["sub"]
        or not isinstance(data.get("jti"), str)
        or not data["jti"]
        or not isinstance(data.get("iat"), int)
        or data["iat"] < now_epoch - 300
        or data["iat"] > now_epoch + 15
        or not isinstance(events, dict)
        or events.get(LOGOUT_EVENT) != {}
        or "nonce" in data
    ):
        raise HTTPException(status_code=400, detail="无效的退出通知声明")
    query, params = "DELETE FROM local_sessions WHERE auth_sub = ?", (data["sub"],)
    if data.get("sid"):
        query += " AND oidc_sid = ?"
        params = (data["sub"], str(data["sid"]))
    with transaction(immediate=True) as connection:
        now = utc_now()
        connection.execute("DELETE FROM oidc_logout_events WHERE expires_at <= ?", (now,))
        inserted = connection.execute(
            """INSERT OR IGNORE INTO oidc_logout_events
               (jti_hash, received_at, expires_at) VALUES (?, ?, ?)""",
            (_token_hash(data["jti"]), now, _future(86400)),
        )
        if not inserted.rowcount:
            return 0
        return connection.execute(query, params).rowcount
