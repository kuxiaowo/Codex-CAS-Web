"""SQLite 连接、结构迁移与首批内容。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Iterator

from app.config import database_path, settings


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
  title TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  summary TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',
  cover_style TEXT NOT NULL DEFAULT 'violet',
  reading_minutes INTEGER NOT NULL DEFAULT 5,
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
  note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
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

CREATE INDEX IF NOT EXISTS idx_notes_category_status ON notes(category_id, status);
CREATE INDEX IF NOT EXISTS idx_comments_note_status ON comments(note_id, status);
CREATE INDEX IF NOT EXISTS idx_auth_attempts_lookup ON auth_attempts(action, subject, created_at);
PRAGMA user_version = 1;
"""

DEFAULT_SETTINGS = {
    "site_name": "CAS Notes",
    "site_tagline": "把零散知识，整理成随时能找到的答案。",
    "registration_enabled": "true",
    "login_per_minute": "10",
    "register_per_hour": "5",
    "comment_per_minute": "8",
}

SEED_NOTES = (
    (
        "Python 上下文管理器：把收尾工作写进协议",
        "python-context-manager",
        "从 with 语句开始，理解资源进入、异常传递与可靠释放。",
        """# 上下文管理器不是语法糖那么简单

`with` 语句把一段工作放进明确的生命周期：进入、执行、退出。

## 最小协议

对象实现 `__enter__` 与 `__exit__` 后，就可以参与上下文管理。退出方法无论代码块是否抛出异常都会被调用，因此适合关闭文件、释放锁和回滚事务。

```python
class Timer:
    def __enter__(self):
        self.started_at = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.elapsed = time.perf_counter() - self.started_at
```

## 什么时候使用

- 资源必须成对获取和释放；
- 清理动作不能依赖调用者记得执行；
- 需要把异常处理边界表达清楚。
""",
        "violet",
        6,
        1,
    ),
    (
        "SQLite WAL：让读取与写入更从容地并行",
        "sqlite-wal-mode",
        "理解 WAL、busy timeout，以及小型网站如何避免数据库锁冲突。",
        """# 为什么选择 WAL

传统回滚日志模式下，写事务更容易阻塞读取。WAL 将变更先追加到独立日志，使读者可以继续看到稳定快照。

## 实用配置

```sql
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
```

WAL 不是无限并发写入方案。SQLite 仍然只有一个写者，因此事务应当短小，并避免在事务中执行网络请求。
""",
        "cyan",
        5,
        1,
    ),
    (
        "CSS Grid 的 auto-fit 与 minmax",
        "css-grid-auto-fit",
        "不用堆媒体查询，也能做出自然收缩的卡片网格。",
        """# 让内容决定列数

下面这一行适合卡片列表：

```css
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 18rem), 1fr));
}
```

`minmax` 给卡片一个舒适的下限，`auto-fit` 会把空轨道折叠，剩余卡片自动填满可用宽度。
""",
        "amber",
        4,
        0,
    ),
    (
        "HTTP 缓存：Cache-Control 的阅读顺序",
        "http-cache-control",
        "从浏览器视角读懂 max-age、no-cache、private 与验证请求。",
        """# 先回答两个问题

浏览器处理缓存时，先判断响应能否被保存，再判断保存的副本是否仍然新鲜。

- `no-store`：不要保存；
- `no-cache`：可以保存，但再次使用前必须验证；
- `private`：只能由私有缓存保存；
- `max-age`：从响应生成开始计算新鲜时间。

带登录态的个性化响应通常使用 `private, no-store`，而带内容指纹的静态资源可以使用很长的 `max-age`。
""",
        "rose",
        5,
        0,
    ),
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
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
    connection.execute(f"PRAGMA busy_timeout = {settings.database_busy_timeout_ms}")
    return connection


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    connection = connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> None:
    with transaction() as connection:
        connection.executescript(SCHEMA)
        now = utc_now()
        for key, value in DEFAULT_SETTINGS.items():
            connection.execute(
                "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, now),
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO categories
              (name, slug, description, accent, sort_order, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            ("学习笔记", "study-notes", "代码、系统与设计的可复用笔记", "#8b7cff", 10, now),
        )
        category = connection.execute(
            "SELECT id FROM categories WHERE slug = 'study-notes'"
        ).fetchone()
        if category and connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0:
            for title, slug, summary, content, style, minutes, featured in SEED_NOTES:
                connection.execute(
                    """
                    INSERT INTO notes
                      (category_id, title, slug, summary, content, cover_style,
                       reading_minutes, status, is_featured, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'published', ?, ?, ?)
                    """,
                    (category["id"], title, slug, summary, content, style, minutes, featured, now, now),
                )


def get_setting(connection: sqlite3.Connection, key: str, fallback: str = "") -> str:
    row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else fallback


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None

