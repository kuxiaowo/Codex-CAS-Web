"""SQLite 连接、结构迁移与首批内容。"""

from __future__ import annotations

import sqlite3
import hashlib
import hmac
import json
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator, Sequence

from app.config import database_path, settings


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL,
  password_hash TEXT NOT NULL DEFAULT '',
  auth_sub TEXT UNIQUE,
  role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  slug TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  accent TEXT NOT NULL DEFAULT '#8b7cff',
  sort_order INTEGER NOT NULL DEFAULT 10,
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS galleries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
  title TEXT NOT NULL,
  resource_dir TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'archived')),
  is_featured INTEGER NOT NULL DEFAULT 0 CHECK (is_featured IN (0, 1)),
  views INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS announcements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'published' CHECK (status IN ('published', 'archived')),
  is_pinned INTEGER NOT NULL DEFAULT 0 CHECK (is_pinned IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gallery_id INTEGER NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  parent_id INTEGER REFERENCES comments(id) ON DELETE CASCADE,
  content TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'visible' CHECK (status IN ('visible', 'hidden')),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action TEXT NOT NULL,
  subject TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS local_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  auth_sub TEXT NOT NULL,
  oidc_sid TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oidc_login_states (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  state_hash TEXT NOT NULL UNIQUE,
  nonce TEXT NOT NULL,
  code_verifier TEXT NOT NULL,
  return_path TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oidc_logout_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  jti_hash TEXT NOT NULL UNIQUE,
  received_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_galleries_category_status ON galleries(category_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_galleries_resource_dir_nocase
  ON galleries(resource_dir COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_comments_gallery_status ON comments(gallery_id, status);
CREATE INDEX IF NOT EXISTS idx_auth_attempts_lookup ON auth_attempts(action, subject, created_at);
CREATE INDEX IF NOT EXISTS idx_local_sessions_user ON local_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_local_sessions_subject ON local_sessions(auth_sub, oidc_sid);
CREATE INDEX IF NOT EXISTS idx_oidc_login_states_expires ON oidc_login_states(expires_at);
CREATE INDEX IF NOT EXISTS idx_oidc_logout_events_expires ON oidc_logout_events(expires_at);
PRAGMA user_version = 4;
"""

MIGRATE_V1_TO_V2 = """
DROP TABLE IF EXISTS comments;
DROP TABLE IF EXISTS notes;

CREATE TABLE galleries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
  title TEXT NOT NULL,
  resource_dir TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'archived')),
  is_featured INTEGER NOT NULL DEFAULT 0 CHECK (is_featured IN (0, 1)),
  views INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  gallery_id INTEGER NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  parent_id INTEGER REFERENCES comments(id) ON DELETE CASCADE,
  content TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'visible' CHECK (status IN ('visible', 'hidden')),
  created_at TEXT NOT NULL
);

CREATE INDEX idx_galleries_category_status ON galleries(category_id, status);
CREATE UNIQUE INDEX idx_galleries_resource_dir_nocase
  ON galleries(resource_dir COLLATE NOCASE);
CREATE INDEX idx_comments_gallery_status ON comments(gallery_id, status);
PRAGMA user_version = 2;
"""

MIGRATE_V2_TO_V3 = """
ALTER TABLE users ADD COLUMN auth_sub TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_auth_sub ON users(auth_sub) WHERE auth_sub IS NOT NULL;
UPDATE users SET password_hash = '', is_active = 0 WHERE auth_sub IS NULL;
CREATE TABLE local_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  auth_sub TEXT NOT NULL,
  oidc_sid TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE oidc_login_states (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  state_hash TEXT NOT NULL UNIQUE,
  nonce TEXT NOT NULL,
  code_verifier TEXT NOT NULL,
  return_path TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX idx_local_sessions_user ON local_sessions(user_id);
CREATE INDEX idx_local_sessions_subject ON local_sessions(auth_sub, oidc_sid);
CREATE INDEX idx_oidc_login_states_expires ON oidc_login_states(expires_at);
PRAGMA user_version = 3;
"""

MIGRATE_V3_TO_V4 = """
UPDATE users SET is_active = 0 WHERE auth_sub IS NULL;
CREATE TABLE oidc_logout_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  jti_hash TEXT NOT NULL UNIQUE,
  received_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX idx_oidc_logout_events_expires ON oidc_logout_events(expires_at);
PRAGMA user_version = 4;
"""

DEFAULT_SETTINGS = {
    "site_name": "Note Gallery",
    "site_tagline": "一个简单的笔记集合站。",
    "comment_per_minute": "8",
}

def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _backend_name() -> str:
    return settings.database_backend or ("d1" if settings.app_environment == "production" else "sqlite")


class D1DatabaseError(RuntimeError):
    """D1 gateway request or SQL error."""


class D1IntegrityError(D1DatabaseError):
    """D1 constraint conflict, analogous to sqlite3.IntegrityError."""


class D1Row(dict[str, Any]):
    """Mapping row with sqlite3.Row-compatible positional access."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class D1Cursor:
    def __init__(self, result: dict[str, Any]):
        self._rows = [D1Row(row) for row in (result.get("rows") or [])]
        meta = result.get("meta") if isinstance(result.get("meta"), dict) else result
        self.rowcount = int(meta.get("changes") or 0)
        self.lastrowid = meta.get("last_row_id")

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows


class D1GatewayAdapter:
    """Small connection-like client for the private D1 SQL gateway."""

    def __init__(self, url: str | None = None, secret: str | None = None, timeout: float | None = None):
        normalized_url = (url if url is not None else settings.d1_gateway_url).rstrip("/")
        self.url = (
            normalized_url
            if normalized_url.endswith("/internal/db")
            else normalized_url + "/internal/db"
        )
        self.secret = secret if secret is not None else settings.d1_hmac_secret
        self.timeout = timeout if timeout is not None else settings.d1_request_timeout_seconds
        if not self.url.startswith("https://"):
            raise RuntimeError("D1_GATEWAY_URL 必须使用 HTTPS")
        if len(self.secret.encode("utf-8")) < 32:
            raise RuntimeError("D1_HMAC_SECRET 至少需要 32 个 UTF-8 字节")

    def _request(self, statements: Sequence[dict[str, Any]], mode: str = "single") -> list[dict[str, Any]]:
        request_id, timestamp = str(uuid.uuid4()), str(int(time.time()))
        payload = json.dumps({"requestId": request_id, "timestamp": int(timestamp), "mode": mode, "statements": list(statements)}, separators=(",", ":"), ensure_ascii=False).encode()
        digest = hashlib.sha256(payload).hexdigest()
        message = f"v1\nPOST\n/internal/db\n{request_id}\n{timestamp}\n{digest}".encode()
        signature = hmac.new(self.secret.encode(), message, hashlib.sha256).hexdigest()
        request = Request(self.url, data=payload, method="POST", headers={
            "Content-Type": "application/json", "X-DB-Request-ID": request_id,
            "X-DB-Timestamp": timestamp, "X-DB-Signature": signature,
        })
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                error_body = json.loads(exc.read().decode("utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                error_body = {}
            message = error_body.get("message") or error_body.get("error") or str(exc)
            error_type = D1IntegrityError if exc.code == 409 or error_body.get("error") == "database_conflict" else D1DatabaseError
            raise error_type(f"D1 网关请求失败: {message}") from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise D1DatabaseError(f"D1 网关请求失败: {exc}") from exc
        if not isinstance(body, dict) or body.get("error"):
            raise D1DatabaseError(str(body.get("error") if isinstance(body, dict) else body))
        results = body.get("results")
        if not isinstance(results, list):
            raise D1DatabaseError("D1 网关响应缺少 results")
        if len(results) != len(statements) or not all(isinstance(item, dict) for item in results):
            raise D1DatabaseError("D1 网关响应的结果数量或格式无效")
        return results

    def execute(self, sql: str, params: Sequence[Any] = ()) -> D1Cursor:
        results = self._request([{"sql": sql, "params": list(params)}])
        if not results:
            raise D1DatabaseError("D1 网关未返回 execute 结果")
        return D1Cursor(results[0])

    def executemany(self, sql: str, seq_of_params: Sequence[Sequence[Any]]):
        return self.batch([{"sql": sql, "params": list(params)} for params in seq_of_params])[-1]

    def batch(self, statements: Sequence[dict[str, Any]]) -> list[D1Cursor]:
        return [D1Cursor(result) for result in self._request(statements, mode="batch")]

    def close(self) -> None:
        return None

def connect() -> sqlite3.Connection | D1GatewayAdapter:
    if _backend_name() == "d1":
        return D1GatewayAdapter()
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        path,
        timeout=settings.database_connect_timeout_seconds,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(
        f"PRAGMA busy_timeout = {int(settings.database_connect_timeout_seconds * 1000)}"
    )
    return connection


@contextmanager
def transaction(*, immediate: bool = False) -> Iterator[sqlite3.Connection | D1GatewayAdapter]:
    connection = connect()
    try:
        if immediate and isinstance(connection, sqlite3.Connection):
            connection.execute("BEGIN IMMEDIATE")
        yield connection
        if isinstance(connection, sqlite3.Connection):
            connection.commit()
    except Exception:
        if isinstance(connection, sqlite3.Connection):
            connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> None:
    # D1 schema/default data is deployed separately. Never execute production DDL.
    if _backend_name() == "d1":
        return
    with transaction() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 1:
            connection.executescript(MIGRATE_V1_TO_V2)
            connection.executescript(MIGRATE_V2_TO_V3)
            connection.executescript(MIGRATE_V3_TO_V4)
        elif version == 0:
            connection.executescript(SCHEMA)
        elif version == 2:
            connection.executescript(MIGRATE_V2_TO_V3)
            connection.executescript(MIGRATE_V3_TO_V4)
        elif version == 3:
            connection.executescript(MIGRATE_V3_TO_V4)
        elif version != 4:
            raise RuntimeError(f"不支持的数据库版本：{version}")
        else:
            connection.executescript(SCHEMA)
        now = utc_now()
        for key, value in DEFAULT_SETTINGS.items():
            connection.execute(
                "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, now),
            )
        connection.execute(
            """
            UPDATE settings SET value = ?, updated_at = ?
            WHERE key = 'site_name' AND value = 'CAS Gallery'
            """,
            (DEFAULT_SETTINGS["site_name"], now),
        )
        if connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            connection.execute(
                """
                INSERT INTO categories
                  (name, slug, description, accent, sort_order, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                ("默认图库", "galleries", "按主题整理的图片图集", "#8b7cff", 10, now),
            )


def get_setting(connection: sqlite3.Connection | D1GatewayAdapter, key: str, fallback: str = "") -> str:
    row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else fallback


def row_dict(row: sqlite3.Row | D1Row | None) -> dict | None:
    return dict(row) if row is not None else None
