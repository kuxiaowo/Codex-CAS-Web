"""集中读取运行配置。"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

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
    app_environment: str = os.getenv("APP_ENV", "production").strip().casefold()
    app_host: str = os.getenv("APP_HOST", "127.0.0.1").strip()
    app_port: int = _env_int("APP_PORT", 3300, minimum=1)
    app_reload: bool = _env_bool("APP_RELOAD", False)
    database_path: str = os.getenv("DATABASE_PATH", "data/cas_notes.db").strip()
    database_connect_timeout_seconds: float = _env_float(
        "DATABASE_CONNECT_TIMEOUT_SECONDS", 5, minimum=0
    )
    database_busy_timeout_ms: int = _env_int("DATABASE_BUSY_TIMEOUT_MS", 5000)
    upload_max_bytes: int = _env_int("UPLOAD_MAX_MB", 50, minimum=1) * 1024 * 1024
    thumbnail_max_width: int = _env_int("THUMBNAIL_MAX_WIDTH", 640, minimum=1)
    thumbnail_max_height: int = _env_int("THUMBNAIL_MAX_HEIGHT", 640, minimum=1)
    thumbnail_webp_quality: int = _env_int("THUMBNAIL_WEBP_QUALITY", 82, minimum=1)
    thumbnail_webp_method: int = _env_int("THUMBNAIL_WEBP_METHOD", 6)
    media_storage_backend: str = os.getenv("MEDIA_STORAGE_BACKEND", "r2").strip().casefold()
    media_gateway_url: str = os.getenv(
        "MEDIA_GATEWAY_URL", "https://codex-media.nethub.wiki"
    ).strip().rstrip("/")
    media_hmac_secret: str = os.getenv("MEDIA_HMAC_SECRET", "").strip()
    media_request_timeout_seconds: float = _env_float(
        "MEDIA_REQUEST_TIMEOUT_SECONDS", 30, minimum=1
    )
    media_download_ttl_seconds: int = _env_int(
        "MEDIA_DOWNLOAD_TTL_SECONDS", 90, minimum=1
    )
    media_multipart_threshold_bytes: int = _env_int(
        "MEDIA_MULTIPART_THRESHOLD_MB", 20, minimum=5
    ) * 1024 * 1024
    media_multipart_part_bytes: int = _env_int(
        "MEDIA_MULTIPART_PART_MB", 8, minimum=5
    ) * 1024 * 1024
    oidc_issuer: str = os.getenv("OIDC_ISSUER", "https://auth.nethub.wiki").strip().rstrip("/")
    oidc_client_id: str = os.getenv("OIDC_CLIENT_ID", "cas").strip()
    oidc_client_secret: str = os.getenv("OIDC_CLIENT_SECRET", "").strip()
    oidc_redirect_uri: str = os.getenv(
        "OIDC_REDIRECT_URI", "https://codex.nethub.wiki/api/auth/callback"
    ).strip()
    oidc_cookie_secure: bool = _env_bool("OIDC_COOKIE_SECURE", True)
    oidc_state_expire_seconds: int = _env_int("OIDC_STATE_EXPIRE_SECONDS", 600, minimum=60)
    local_session_expire_seconds: int = _env_int(
        "LOCAL_SESSION_EXPIRE_SECONDS", 7 * 86400, minimum=300
    )
    cas_admin_subs: frozenset[str] = frozenset(
        value.strip() for value in os.getenv("CAS_ADMIN_SUBS", "").split(",") if value.strip()
    )


settings = Settings()


def database_path() -> Path:
    configured = Path(settings.database_path).expanduser()
    return configured if configured.is_absolute() else PROJECT_ROOT / configured


def _https_or_loopback(value: str) -> bool:
    parsed = urlsplit(value)
    if parsed.scheme == "https" and parsed.netloc:
        return True
    if parsed.scheme != "http" or not parsed.hostname or not parsed.netloc:
        return False
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return parsed.hostname.casefold() == "localhost"


def validate_media_storage_settings(
    configured: Settings = settings, *, require_r2: bool = False
) -> None:
    """Validate only settings required by media storage clients."""

    if configured.media_storage_backend not in {"r2", "local"}:
        raise RuntimeError("MEDIA_STORAGE_BACKEND 必须是 r2 或 local")
    if require_r2 and configured.media_storage_backend != "r2":
        raise RuntimeError("执行迁移时 MEDIA_STORAGE_BACKEND 必须为 r2")
    if configured.media_storage_backend == "local":
        if configured.app_environment == "production":
            raise RuntimeError("生产环境禁止使用本地媒体存储")
        return

    parsed_gateway = urlsplit(configured.media_gateway_url)
    if parsed_gateway.scheme != "https" or not parsed_gateway.netloc:
        raise RuntimeError("MEDIA_GATEWAY_URL 必须是 HTTPS 地址")
    if len(configured.media_hmac_secret.encode("utf-8")) < 32:
        raise RuntimeError("MEDIA_HMAC_SECRET 至少需要 32 个 UTF-8 字节")
    if configured.media_download_ttl_seconds > 120:
        raise RuntimeError("MEDIA_DOWNLOAD_TTL_SECONDS 不能超过 Worker 的 120 秒上限")
    if configured.media_multipart_threshold_bytes > 25 * 1024 * 1024:
        raise RuntimeError("MEDIA_MULTIPART_THRESHOLD_MB 不能超过 Worker 的 25 MiB 上限")
    if configured.media_multipart_part_bytes > 25 * 1024 * 1024:
        raise RuntimeError("MEDIA_MULTIPART_PART_MB 不能超过 Worker 的 25 MiB 上限")


def validate_runtime_settings() -> None:
    if not settings.app_host:
        raise RuntimeError("APP_HOST 不能为空")
    if not 1 <= settings.app_port <= 65535:
        raise RuntimeError("APP_PORT 必须在 1-65535 之间")
    if not 1 <= settings.thumbnail_webp_quality <= 100:
        raise RuntimeError("THUMBNAIL_WEBP_QUALITY 必须在 1-100 之间")
    if not 0 <= settings.thumbnail_webp_method <= 6:
        raise RuntimeError("THUMBNAIL_WEBP_METHOD 必须在 0-6 之间")
    if settings.app_environment not in {"production", "development", "test"}:
        raise RuntimeError("APP_ENV 必须是 production、development 或 test")
    validate_media_storage_settings(settings)
    if not settings.oidc_issuer.startswith("https://"):
        raise RuntimeError("OIDC_ISSUER 必须使用 https://")
    if not settings.oidc_client_id or len(settings.oidc_client_secret) < 16:
        raise RuntimeError("OIDC_CLIENT_ID 或 OIDC_CLIENT_SECRET 未正确配置")
    if not _https_or_loopback(settings.oidc_redirect_uri):
        raise RuntimeError("OIDC_REDIRECT_URI 必须使用 HTTPS（本机回环开发地址可使用 HTTP）")
