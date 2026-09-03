"""FastAPI 一体化入口：同一端口提供页面、静态资源和 JSON API。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json
import sqlite3
from pathlib import Path
from typing import Annotated

import bleach
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import markdown
import uvicorn

from app.auth import admin_user, create_token, current_user, hash_password, verify_password
from app.config import PROJECT_ROOT, settings, validate_runtime_settings
from app.database import connect, get_setting, initialize_database, row_dict, transaction, utc_now
from app.schemas import (
    AnnouncementInput,
    CategoryInput,
    CommentInput,
    LoginInput,
    NoteInput,
    RegisterInput,
    SettingsInput,
    UserCreateInput,
    UserUpdateInput,
)

STATIC_DIR = PROJECT_ROOT / "static"
TEMPLATE_DIR = PROJECT_ROOT / "templates"


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_runtime_settings()
    initialize_database()
    yield


app = FastAPI(
    title="CAS Notes",
    description="一体化学习笔记信息站",
    version="0.1.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATE_DIR)


@app.middleware("http")
async def no_store_dynamic_pages(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


def _site_context(connection: sqlite3.Connection) -> dict:
    return {
        "siteName": get_setting(connection, "site_name", "CAS Notes"),
        "siteTagline": get_setting(
            connection,
            "site_tagline",
            "把零散知识，整理成随时能找到的答案。",
        ),
    }


def _categories(connection: sqlite3.Connection, *, include_inactive: bool = False) -> list[dict]:
    where = "" if include_inactive else "WHERE c.is_active = 1"
    rows = connection.execute(
        f"""
        SELECT c.*, COUNT(CASE WHEN n.status = 'published' THEN 1 END) AS note_count
        FROM categories c
        LEFT JOIN notes n ON n.category_id = c.id
        {where}
        GROUP BY c.id
        ORDER BY c.sort_order, c.id
        """
    ).fetchall()
    return [_category_dict(row) for row in rows]


def _category_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "name": data["name"],
        "slug": data["slug"],
        "description": data["description"],
        "accent": data["accent"],
        "sortOrder": data["sort_order"],
        "isActive": bool(data["is_active"]),
        "noteCount": data.get("note_count", 0),
        "createdAt": data["created_at"],
    }


def _note_dict(row: sqlite3.Row, *, include_content: bool = False) -> dict:
    data = dict(row)
    result = {
        "id": data["id"],
        "categoryId": data["category_id"],
        "categoryName": data.get("category_name"),
        "categorySlug": data.get("category_slug"),
        "title": data["title"],
        "slug": data["slug"],
        "summary": data["summary"],
        "coverStyle": data["cover_style"],
        "readingMinutes": data["reading_minutes"],
        "status": data["status"],
        "isFeatured": bool(data["is_featured"]),
        "views": data["views"],
        "createdAt": data["created_at"],
        "updatedAt": data["updated_at"],
    }
    if include_content:
        result["content"] = data["content"]
    return result


def _announcement_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "title": data["title"],
        "content": data["content"],
        "status": data["status"],
        "isPinned": bool(data["is_pinned"]),
        "createdAt": data["created_at"],
        "updatedAt": data["updated_at"],
    }


def _user_dict(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "username": data["username"],
        "displayName": data["display_name"],
        "role": data["role"],
        "isActive": bool(data["is_active"]),
        "createdAt": data["created_at"],
    }


def _comment_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "noteId": data["note_id"],
        "noteTitle": data.get("note_title"),
        "userId": data["user_id"],
        "author": data.get("display_name", "已注销用户"),
        "parentId": data["parent_id"],
        "content": data["content"],
        "status": data["status"],
        "createdAt": data["created_at"],
    }


def _client_subject(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _enforce_rate(connection: sqlite3.Connection, action: str, subject: str, limit: int, seconds: int) -> None:
    threshold = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(timespec="seconds")
    count = connection.execute(
        "SELECT COUNT(*) FROM auth_attempts WHERE action = ? AND subject = ? AND created_at >= ?",
        (action, subject, threshold),
    ).fetchone()[0]
    if count >= limit:
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")
    connection.execute(
        "INSERT INTO auth_attempts (action, subject, created_at) VALUES (?, ?, ?)",
        (action, subject, utc_now()),
    )
    connection.execute(
        "DELETE FROM auth_attempts WHERE created_at < ?",
        ((datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds"),),
    )
    # 限流记录必须独立持久化；后续认证失败引发的事务回滚不能抹掉失败次数。
    connection.commit()


def _render_markdown(source: str) -> str:
    rendered = markdown.markdown(source, extensions=["fenced_code", "tables"])
    allowed_tags = set(bleach.sanitizer.ALLOWED_TAGS) | {
        "p", "h1", "h2", "h3", "h4", "pre", "code", "hr", "br",
        "table", "thead", "tbody", "tr", "th", "td",
    }
    return bleach.clean(
        rendered,
        tags=allowed_tags,
        attributes={"a": ["href", "title", "rel"]},
        protocols={"http", "https", "mailto"},
    )


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    q: str = Query(default="", max_length=100),
    category: str = Query(default="", max_length=50),
):
    connection = connect()
    try:
        sql = """
            SELECT n.*, c.name AS category_name, c.slug AS category_slug
            FROM notes n JOIN categories c ON c.id = n.category_id
            WHERE n.status = 'published' AND c.is_active = 1
        """
        params: list[object] = []
        if category:
            sql += " AND c.slug = ?"
            params.append(category)
        if q.strip():
            sql += " AND (n.title LIKE ? OR n.summary LIKE ? OR n.content LIKE ?)"
            term = f"%{q.strip()}%"
            params.extend([term, term, term])
        sql += " ORDER BY n.is_featured DESC, n.updated_at DESC, n.id DESC"
        notes = [_note_dict(row) for row in connection.execute(sql, params).fetchall()]
        announcements = [
            _announcement_dict(row)
            for row in connection.execute(
                """
                SELECT * FROM announcements WHERE status = 'published'
                ORDER BY is_pinned DESC, created_at DESC LIMIT 3
                """
            ).fetchall()
        ]
        context = {
            "request": request,
            "site": _site_context(connection),
            "categories": _categories(connection),
            "notes": notes,
            "announcements": announcements,
            "query": q,
            "activeCategory": category,
            "totalNotes": connection.execute(
                "SELECT COUNT(*) FROM notes WHERE status = 'published'"
            ).fetchone()[0],
        }
    finally:
        connection.close()
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/notes/{slug}", response_class=HTMLResponse)
def note_detail(request: Request, slug: str):
    with transaction() as connection:
        row = connection.execute(
            """
            SELECT n.*, c.name AS category_name, c.slug AS category_slug
            FROM notes n JOIN categories c ON c.id = n.category_id
            WHERE n.slug = ? AND n.status = 'published' AND c.is_active = 1
            """,
            (slug,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="笔记不存在")
        connection.execute("UPDATE notes SET views = views + 1 WHERE id = ?", (row["id"],))
        note = _note_dict(row, include_content=True)
        note["html"] = _render_markdown(note["content"])
        context = {
            "request": request,
            "site": _site_context(connection),
            "categories": _categories(connection),
            "note": note,
        }
    return templates.TemplateResponse(request, "note.html", context)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    connection = connect()
    try:
        context = {
            "request": request,
            "site": _site_context(connection),
            "categories": _categories(connection),
            "registrationEnabled": get_setting(connection, "registration_enabled", "true") == "true",
        }
    finally:
        connection.close()
    return templates.TemplateResponse(request, "login.html", context)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    connection = connect()
    try:
        context = {
            "request": request,
            "site": _site_context(connection),
            "categories": _categories(connection),
        }
    finally:
        connection.close()
    return templates.TemplateResponse(request, "admin.html", context)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/auth/register", status_code=201)
def register(payload: RegisterInput, request: Request):
    if payload.password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的密码不一致")
    with transaction() as connection:
        if get_setting(connection, "registration_enabled", "true") != "true":
            raise HTTPException(status_code=403, detail="当前未开放注册")
        limit = int(get_setting(connection, "register_per_hour", "5"))
        _enforce_rate(connection, "register", _client_subject(request), limit, 3600)
        try:
            cursor = connection.execute(
                """
                INSERT INTO users (username, display_name, password_hash, role, is_active, created_at)
                VALUES (?, ?, ?, 'user', 1, ?)
                """,
                (payload.username.strip(), payload.display_name.strip(), hash_password(payload.password), utc_now()),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="用户名已存在") from exc
        user_id = cursor.lastrowid
    return {"accessToken": create_token(user_id), "tokenType": "bearer"}


@app.post("/api/auth/login")
def login(payload: LoginInput, request: Request):
    with transaction() as connection:
        limit = int(get_setting(connection, "login_per_minute", "10"))
        _enforce_rate(connection, "login", _client_subject(request), limit, 60)
        user = connection.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (payload.username.strip(),)
        ).fetchone()
        if not user or not user["is_active"] or not verify_password(payload.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"accessToken": create_token(user["id"]), "tokenType": "bearer"}


@app.get("/api/auth/me")
def me(user: Annotated[dict, Depends(current_user)]):
    return {"data": _user_dict(user)}


@app.get("/api/notes/{note_id}/comments")
def list_comments(note_id: int):
    connection = connect()
    try:
        rows = connection.execute(
            """
            SELECT c.*, u.display_name
            FROM comments c JOIN users u ON u.id = c.user_id
            WHERE c.note_id = ? AND c.status = 'visible'
            ORDER BY c.created_at, c.id
            """,
            (note_id,),
        ).fetchall()
        return {"data": [_comment_dict(row) for row in rows]}
    finally:
        connection.close()


@app.post("/api/notes/{note_id}/comments", status_code=201)
def create_comment(
    note_id: int,
    payload: CommentInput,
    request: Request,
    user: Annotated[dict, Depends(current_user)],
):
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="留言不能为空")
    with transaction() as connection:
        note = connection.execute(
            "SELECT id FROM notes WHERE id = ? AND status = 'published'", (note_id,)
        ).fetchone()
        if not note:
            raise HTTPException(status_code=404, detail="笔记不存在")
        if payload.parent_id:
            parent = connection.execute(
                "SELECT id FROM comments WHERE id = ? AND note_id = ? AND status = 'visible'",
                (payload.parent_id, note_id),
            ).fetchone()
            if not parent:
                raise HTTPException(status_code=404, detail="回复的留言不存在")
        limit = int(get_setting(connection, "comment_per_minute", "8"))
        _enforce_rate(connection, "comment", str(user["id"]), limit, 60)
        cursor = connection.execute(
            """
            INSERT INTO comments (note_id, user_id, parent_id, content, status, created_at)
            VALUES (?, ?, ?, ?, 'visible', ?)
            """,
            (note_id, user["id"], payload.parent_id, content, utc_now()),
        )
    return {"data": {"id": cursor.lastrowid}}


@app.get("/api/admin/dashboard")
def admin_dashboard(_: Annotated[dict, Depends(admin_user)]):
    connection = connect()
    try:
        counts = {
            "notes": connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0],
            "publishedNotes": connection.execute(
                "SELECT COUNT(*) FROM notes WHERE status = 'published'"
            ).fetchone()[0],
            "users": connection.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "comments": connection.execute("SELECT COUNT(*) FROM comments").fetchone()[0],
            "views": connection.execute("SELECT COALESCE(SUM(views), 0) FROM notes").fetchone()[0],
        }
        recent = [_note_dict(row) for row in connection.execute(
            """
            SELECT n.*, c.name AS category_name, c.slug AS category_slug
            FROM notes n JOIN categories c ON c.id = n.category_id
            ORDER BY n.updated_at DESC LIMIT 5
            """
        ).fetchall()]
        return {"data": {"counts": counts, "recentNotes": recent}}
    finally:
        connection.close()


@app.get("/api/admin/users")
def admin_users(_: Annotated[dict, Depends(admin_user)]):
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT id, username, display_name, role, is_active, created_at FROM users ORDER BY id DESC"
        ).fetchall()
        return {"data": [_user_dict(row) for row in rows]}
    finally:
        connection.close()


@app.post("/api/admin/users", status_code=201)
def admin_create_user(
    payload: UserCreateInput,
    _: dict = Depends(admin_user),
):
    with transaction() as connection:
        try:
            cursor = connection.execute(
                """
                INSERT INTO users (username, display_name, password_hash, role, is_active, created_at)
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                (
                    payload.username.strip(), payload.display_name.strip(),
                    hash_password(payload.password), payload.role, utc_now(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="用户名已存在") from exc
    return {"data": {"id": cursor.lastrowid}}


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(
    user_id: int,
    payload: UserUpdateInput,
    operator: Annotated[dict, Depends(admin_user)],
):
    if user_id == operator["id"] and (not payload.is_active or payload.role != "admin"):
        raise HTTPException(status_code=409, detail="不能停用自己或移除自己的管理员角色")
    with transaction() as connection:
        if not connection.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone():
            raise HTTPException(status_code=404, detail="用户不存在")
        if payload.password:
            connection.execute(
                """
                UPDATE users SET display_name = ?, role = ?, is_active = ?, password_hash = ?
                WHERE id = ?
                """,
                (payload.display_name.strip(), payload.role, int(payload.is_active), hash_password(payload.password), user_id),
            )
        else:
            connection.execute(
                "UPDATE users SET display_name = ?, role = ?, is_active = ? WHERE id = ?",
                (payload.display_name.strip(), payload.role, int(payload.is_active), user_id),
            )
    return {"data": {"id": user_id}}


@app.delete("/api/admin/users/{user_id}", status_code=204)
def admin_delete_user(
    user_id: int,
    operator: Annotated[dict, Depends(admin_user)],
):
    if user_id == operator["id"]:
        raise HTTPException(status_code=409, detail="不能删除当前登录账号")
    with transaction() as connection:
        cursor = connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="用户不存在")


@app.get("/api/admin/categories")
def admin_categories(_: Annotated[dict, Depends(admin_user)]):
    connection = connect()
    try:
        return {"data": _categories(connection, include_inactive=True)}
    finally:
        connection.close()


@app.post("/api/admin/categories", status_code=201)
def admin_create_category(payload: CategoryInput, _: Annotated[dict, Depends(admin_user)]):
    with transaction() as connection:
        try:
            cursor = connection.execute(
                """
                INSERT INTO categories
                  (name, slug, description, accent, sort_order, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.name.strip(), payload.slug, payload.description.strip(), payload.accent,
                    payload.sort_order, int(payload.is_active), utc_now(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="栏目名称或标识已存在") from exc
    return {"data": {"id": cursor.lastrowid}}


@app.patch("/api/admin/categories/{category_id}")
def admin_update_category(
    category_id: int,
    payload: CategoryInput,
    _: Annotated[dict, Depends(admin_user)],
):
    with transaction() as connection:
        try:
            cursor = connection.execute(
                """
                UPDATE categories
                SET name = ?, slug = ?, description = ?, accent = ?, sort_order = ?, is_active = ?
                WHERE id = ?
                """,
                (
                    payload.name.strip(), payload.slug, payload.description.strip(), payload.accent,
                    payload.sort_order, int(payload.is_active), category_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="栏目名称或标识已存在") from exc
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="栏目不存在")
    return {"data": {"id": category_id}}


@app.delete("/api/admin/categories/{category_id}", status_code=204)
def admin_delete_category(category_id: int, _: Annotated[dict, Depends(admin_user)]):
    with transaction() as connection:
        if connection.execute("SELECT COUNT(*) FROM notes WHERE category_id = ?", (category_id,)).fetchone()[0]:
            raise HTTPException(status_code=409, detail="栏目中仍有笔记，不能删除")
        cursor = connection.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="栏目不存在")


@app.get("/api/admin/notes")
def admin_notes(_: Annotated[dict, Depends(admin_user)]):
    connection = connect()
    try:
        rows = connection.execute(
            """
            SELECT n.*, c.name AS category_name, c.slug AS category_slug
            FROM notes n JOIN categories c ON c.id = n.category_id
            ORDER BY n.updated_at DESC, n.id DESC
            """
        ).fetchall()
        return {"data": [_note_dict(row, include_content=True) for row in rows]}
    finally:
        connection.close()


def _write_note(connection: sqlite3.Connection, payload: NoteInput, note_id: int | None = None) -> int:
    if not connection.execute("SELECT id FROM categories WHERE id = ?", (payload.category_id,)).fetchone():
        raise HTTPException(status_code=422, detail="栏目不存在")
    now = utc_now()
    values = (
        payload.category_id, payload.title.strip(), payload.slug, payload.summary.strip(), payload.content,
        payload.cover_style, payload.reading_minutes, payload.status, int(payload.is_featured), now,
    )
    try:
        if note_id is None:
            cursor = connection.execute(
                """
                INSERT INTO notes
                  (category_id, title, slug, summary, content, cover_style, reading_minutes,
                   status, is_featured, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values + (now,),
            )
            return int(cursor.lastrowid)
        cursor = connection.execute(
            """
            UPDATE notes SET category_id = ?, title = ?, slug = ?, summary = ?, content = ?,
              cover_style = ?, reading_minutes = ?, status = ?, is_featured = ?, updated_at = ?
            WHERE id = ?
            """,
            values + (note_id,),
        )
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="笔记不存在")
        return note_id
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="笔记标识已存在") from exc


@app.post("/api/admin/notes", status_code=201)
def admin_create_note(payload: NoteInput, _: Annotated[dict, Depends(admin_user)]):
    with transaction() as connection:
        note_id = _write_note(connection, payload)
    return {"data": {"id": note_id}}


@app.patch("/api/admin/notes/{note_id}")
def admin_update_note(
    note_id: int,
    payload: NoteInput,
    _: Annotated[dict, Depends(admin_user)],
):
    with transaction() as connection:
        _write_note(connection, payload, note_id)
    return {"data": {"id": note_id}}


@app.delete("/api/admin/notes/{note_id}", status_code=204)
def admin_delete_note(note_id: int, _: Annotated[dict, Depends(admin_user)]):
    with transaction() as connection:
        cursor = connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="笔记不存在")


@app.get("/api/admin/announcements")
def admin_announcements(_: Annotated[dict, Depends(admin_user)]):
    connection = connect()
    try:
        return {"data": [_announcement_dict(row) for row in connection.execute(
            "SELECT * FROM announcements ORDER BY is_pinned DESC, created_at DESC"
        ).fetchall()]}
    finally:
        connection.close()


@app.post("/api/admin/announcements", status_code=201)
def admin_create_announcement(
    payload: AnnouncementInput,
    _: Annotated[dict, Depends(admin_user)],
):
    now = utc_now()
    with transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO announcements (title, content, status, is_pinned, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (payload.title.strip(), payload.content.strip(), payload.status, int(payload.is_pinned), now, now),
        )
    return {"data": {"id": cursor.lastrowid}}


@app.patch("/api/admin/announcements/{announcement_id}")
def admin_update_announcement(
    announcement_id: int,
    payload: AnnouncementInput,
    _: Annotated[dict, Depends(admin_user)],
):
    with transaction() as connection:
        cursor = connection.execute(
            """
            UPDATE announcements SET title = ?, content = ?, status = ?, is_pinned = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                payload.title.strip(), payload.content.strip(), payload.status,
                int(payload.is_pinned), utc_now(), announcement_id,
            ),
        )
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="公告不存在")
    return {"data": {"id": announcement_id}}


@app.delete("/api/admin/announcements/{announcement_id}", status_code=204)
def admin_delete_announcement(
    announcement_id: int,
    _: Annotated[dict, Depends(admin_user)],
):
    with transaction() as connection:
        cursor = connection.execute("DELETE FROM announcements WHERE id = ?", (announcement_id,))
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="公告不存在")


@app.get("/api/admin/comments")
def admin_comments(_: Annotated[dict, Depends(admin_user)]):
    connection = connect()
    try:
        rows = connection.execute(
            """
            SELECT c.*, u.display_name, n.title AS note_title
            FROM comments c
            JOIN users u ON u.id = c.user_id
            JOIN notes n ON n.id = c.note_id
            ORDER BY c.created_at DESC, c.id DESC
            """
        ).fetchall()
        return {"data": [_comment_dict(row) for row in rows]}
    finally:
        connection.close()


@app.patch("/api/admin/comments/{comment_id}")
def admin_toggle_comment(
    comment_id: int,
    status: str = Body(embed=True),
    _: dict = Depends(admin_user),
):
    if status not in {"visible", "hidden"}:
        raise HTTPException(status_code=422, detail="留言状态无效")
    with transaction() as connection:
        cursor = connection.execute("UPDATE comments SET status = ? WHERE id = ?", (status, comment_id))
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="留言不存在")
    return {"data": {"id": comment_id}}


@app.delete("/api/admin/comments/{comment_id}", status_code=204)
def admin_delete_comment(comment_id: int, _: Annotated[dict, Depends(admin_user)]):
    with transaction() as connection:
        cursor = connection.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="留言不存在")


@app.get("/api/admin/settings")
def admin_settings(_: Annotated[dict, Depends(admin_user)]):
    connection = connect()
    try:
        return {
            "data": {
                "siteName": get_setting(connection, "site_name", "CAS Notes"),
                "siteTagline": get_setting(connection, "site_tagline", ""),
                "registrationEnabled": get_setting(connection, "registration_enabled", "true") == "true",
                "loginPerMinute": int(get_setting(connection, "login_per_minute", "10")),
                "registerPerHour": int(get_setting(connection, "register_per_hour", "5")),
                "commentPerMinute": int(get_setting(connection, "comment_per_minute", "8")),
            }
        }
    finally:
        connection.close()


@app.patch("/api/admin/settings")
def admin_update_settings(
    payload: SettingsInput,
    _: Annotated[dict, Depends(admin_user)],
):
    values = {
        "site_name": payload.site_name.strip(),
        "site_tagline": payload.site_tagline.strip(),
        "registration_enabled": str(payload.registration_enabled).lower(),
        "login_per_minute": str(payload.login_per_minute),
        "register_per_hour": str(payload.register_per_hour),
        "comment_per_minute": str(payload.comment_per_minute),
    }
    with transaction() as connection:
        for key, value in values.items():
            connection.execute(
                """
                INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, utc_now()),
            )
    return {"data": values}


@app.get("/api/admin/export")
def admin_export(_: Annotated[dict, Depends(admin_user)]):
    connection = connect()
    try:
        payload = {
            "formatVersion": 1,
            "exportedAt": utc_now(),
            "categories": [_category_dict(row) for row in connection.execute(
                """
                SELECT c.*, COUNT(CASE WHEN n.status = 'published' THEN 1 END) AS note_count
                FROM categories c LEFT JOIN notes n ON n.category_id = c.id
                GROUP BY c.id ORDER BY c.sort_order, c.id
                """
            ).fetchall()],
            "notes": [_note_dict(row, include_content=True) for row in connection.execute(
                """
                SELECT n.*, c.name AS category_name, c.slug AS category_slug
                FROM notes n JOIN categories c ON c.id = n.category_id ORDER BY n.id
                """
            ).fetchall()],
            "announcements": [_announcement_dict(row) for row in connection.execute(
                "SELECT * FROM announcements ORDER BY id"
            ).fetchall()],
        }
    finally:
        connection.close()
    return JSONResponse(payload, headers={"Content-Disposition": "attachment; filename=cas-notes-export.json"})


@app.post("/api/admin/import")
def admin_import(
    payload: dict = Body(),
    _: dict = Depends(admin_user),
):
    if payload.get("formatVersion") != 1:
        raise HTTPException(status_code=422, detail="不支持的数据格式版本")
    imported = {"categories": 0, "notes": 0, "announcements": 0}
    with transaction() as connection:
        category_map: dict[int, int] = {}
        for item in payload.get("categories", []):
            existing = connection.execute("SELECT id FROM categories WHERE slug = ?", (item.get("slug"),)).fetchone()
            if existing:
                category_map[int(item.get("id", 0))] = existing["id"]
                continue
            model = CategoryInput(
                name=item.get("name", ""), slug=item.get("slug", ""),
                description=item.get("description", ""), accent=item.get("accent", "#8b7cff"),
                sort_order=item.get("sortOrder", 10), is_active=item.get("isActive", True),
            )
            cursor = connection.execute(
                """
                INSERT INTO categories (name, slug, description, accent, sort_order, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (model.name, model.slug, model.description, model.accent, model.sort_order, int(model.is_active), utc_now()),
            )
            category_map[int(item.get("id", 0))] = cursor.lastrowid
            imported["categories"] += 1
        for item in payload.get("notes", []):
            if connection.execute("SELECT id FROM notes WHERE slug = ?", (item.get("slug"),)).fetchone():
                continue
            category_id = category_map.get(int(item.get("categoryId", 0)))
            if not category_id:
                continue
            model = NoteInput(
                category_id=category_id, title=item.get("title", ""), slug=item.get("slug", ""),
                summary=item.get("summary", ""), content=item.get("content", ""),
                cover_style=item.get("coverStyle", "violet"), reading_minutes=item.get("readingMinutes", 5),
                status=item.get("status", "draft"), is_featured=item.get("isFeatured", False),
            )
            _write_note(connection, model)
            imported["notes"] += 1
        for item in payload.get("announcements", []):
            model = AnnouncementInput(
                title=item.get("title", ""), content=item.get("content", ""),
                status=item.get("status", "published"), is_pinned=item.get("isPinned", False),
            )
            now = utc_now()
            connection.execute(
                """
                INSERT INTO announcements (title, content, status, is_pinned, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (model.title, model.content, model.status, int(model.is_pinned), now, now),
            )
            imported["announcements"] += 1
    return {"data": imported}


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_reload,
    )
