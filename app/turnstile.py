"""Verify a single-use Turnstile token before accepting a comment."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

from .config import settings


def verify_turnstile(token: str, action: str) -> None:
    if not token:
        raise HTTPException(status_code=400, detail="请完成人机验证")
    if not settings.turnstile_secret_key:
        raise HTTPException(status_code=503, detail="人机验证暂不可用")
    try:
        response = httpx.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": settings.turnstile_secret_key, "response": token},
            timeout=4.0,
        )
        response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="人机验证暂不可用") from exc
    if not (
        result.get("success") is True
        and result.get("hostname") == urlsplit(settings.oidc_redirect_uri).hostname
        and result.get("action") == action
    ):
        raise HTTPException(status_code=400, detail="人机验证未通过，请重试")
