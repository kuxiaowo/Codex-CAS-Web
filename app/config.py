"""集中读取运行配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数，当前值为 {raw_value!r}") from exc
    if value < minimum:
        raise RuntimeError(f"{name} 不能小于 {minimum}")
    return value


def _env_float(name: str, default: float, *, minimum: float = 0) -> float:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是数字，当前值为 {raw_value!r}") from exc
    if value < minimum:
        raise RuntimeError(f"{name} 不能小于 {minimum}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name, str(default)).strip().casefold()
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} 必须是 true/false，当前值为 {raw_value!r}")


@dataclass(frozen=True)
class Settings:
    app_host: str = os.getenv("APP_HOST", "0.0.0.0").strip()
    app_port: int = _env_int("APP_PORT", 3300, minimum=1)
    app_reload: bool = _env_bool("APP_RELOAD", False)
    database_path: str = os.getenv("DATABASE_PATH", "data/cas_notes.db").strip()
    database_connect_timeout_seconds: float = _env_float(
        "DATABASE_CONNECT_TIMEOUT_SECONDS", 5, minimum=0
    )
    database_busy_timeout_ms: int = _env_int("DATABASE_BUSY_TIMEOUT_MS", 5000)
    upload_max_bytes: int = _env_int("UPLOAD_MAX_MB", 50, minimum=1) * 1024 * 1024
    thumbnail_max_width: int = _env_int("THUMBNAIL_MAX_WIDTH", 1600, minimum=1)
    thumbnail_max_height: int = _env_int("THUMBNAIL_MAX_HEIGHT", 4000, minimum=1)
    thumbnail_webp_quality: int = _env_int("THUMBNAIL_WEBP_QUALITY", 82, minimum=1)
    thumbnail_webp_method: int = _env_int("THUMBNAIL_WEBP_METHOD", 6)
    auth_secret_key: str = os.getenv("AUTH_SECRET_KEY", "").strip()
    auth_token_expire_minutes: int = _env_int(
        "AUTH_TOKEN_EXPIRE_MINUTES", 240, minimum=1
    )


settings = Settings()


def database_path() -> Path:
    configured = Path(settings.database_path).expanduser()
    return configured if configured.is_absolute() else PROJECT_ROOT / configured


def validate_runtime_settings() -> None:
    if not settings.app_host:
        raise RuntimeError("APP_HOST 不能为空")
    if not 1 <= settings.app_port <= 65535:
        raise RuntimeError("APP_PORT 必须在 1-65535 之间")
    if not 1 <= settings.thumbnail_webp_quality <= 100:
        raise RuntimeError("THUMBNAIL_WEBP_QUALITY 必须在 1-100 之间")
    if not 0 <= settings.thumbnail_webp_method <= 6:
        raise RuntimeError("THUMBNAIL_WEBP_METHOD 必须在 0-6 之间")
    secret = settings.auth_secret_key.encode("utf-8")
    if len(secret) < 32:
        raise RuntimeError("AUTH_SECRET_KEY 未配置或长度不足 32 字节")
