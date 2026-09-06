"""FastAPI 一体化入口：同一端口提供页面、静态资源和 JSON API。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
import shutil
import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

from app.auth import (
    OIDC_FLOW_COOKIE,
    admin_user,
    browser_request_is_same_origin,
    clear_oidc_flow_cookie,
    clear_session_cookie,
    complete_oidc_login,
    consume_login_state,
    current_user,
    revoke_backchannel_sessions,
    revoke_current_session,
    safe_return_path,
    set_session_cookie,
    start_oidc_login,
)
from app.config import PROJECT_ROOT, settings, validate_runtime_settings
from app.database import connect, get_setting, initialize_database, transaction, utc_now
from app.gallery_assets import (
    IMAGE_EXTENSIONS,
    RESOURCE_DIR,
    ensure_thumbnail,
    ensure_resource_root,
    folder_url,
    format_file_item,
    image_files,
    is_valid_image,
    normalize_folder_upload_path,
    resolve_resource_path,
    scan_gallery,
    sync_all_thumbnails,
    sync_gallery_thumbnails,
    validate_entry_name,
    validate_gallery_directory,
)
from app.schemas import (
    AnnouncementInput,
    CategoryInput,
    CommentInput,
    GalleryInput,
    SettingsInput,
    UserUpdateInput,
)

STATIC_DIR = PROJECT_ROOT / "static"
TEMPLATE_DIR = PROJECT_ROOT / "templates"
logger = logging.getLogger(__name__)


async def _thumbnail_sync_loop() -> None:
    interval_seconds = settings.thumbnail_sync_minutes * 60
    while interval_seconds > 0:
        await asyncio.sleep(interval_seconds)
        try:
            result = await asyncio.to_thread(sync_all_thumbnails)
            logger.info(
                "缩略图定时同步完成：扫描 %s，生成 %s，已有 %s，清理 %s，失败 %s",
                result.scanned, result.generated, result.current, result.removed, result.failed,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("缩略图定时同步失败")


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_runtime_settings()
    ensure_resource_root()
    initialize_database()
    result = await asyncio.to_thread(sync_all_thumbnails)
    logger.info(
        "缩略图启动同步完成：扫描 %s，生成 %s，已有 %s，清理 %s，失败 %s",
        result.scanned, result.generated, result.current, result.removed, result.failed,
    )
    sync_task = (
        asyncio.create_task(_thumbnail_sync_loop(), name="thumbnail-sync")
        if settings.thumbnail_sync_minutes > 0 else None
    )
    try:
        yield
    finally:
        if sync_task is not None:
            sync_task.cancel()
            with suppress(asyncio.CancelledError):
                await sync_task


app = FastAPI(
    title="Note Gallery",
    description="一体化图片图集站",
    version="0.1.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/resources", StaticFiles(directory=RESOURCE_DIR), name="resources")
templates = Jinja2Templates(directory=TEMPLATE_DIR)


@app.middleware("http")
async def no_store_dynamic_pages(request: Request, call_next):
    if (
        request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and request.url.path != "/api/auth/backchannel-logout"
        and not browser_request_is_same_origin(request)
    ):
        return JSONResponse(status_code=403, content={"detail": "拒绝跨站请求"})
    response = await call_next(request)
    if not request.url.path.startswith(("/static/", "/resources/")):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    elif request.url.path.startswith("/resources/"):
        response.headers["Cache-Control"] = "public, max-age=3600, must-revalidate"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


def _site_context(connection: sqlite3.Connection) -> dict:
    return {
        "siteName": get_setting(connection, "site_name", "Note Gallery"),
        "siteTagline": get_setting(
            connection,
            "site_tagline",
            "一个简单的笔记集合站。",
        ),
    }


def _categories(connection: sqlite3.Connection, *, include_inactive: bool = False) -> list[dict]:
    where = "" if include_inactive else "WHERE c.is_active = 1"
    rows = connection.execute(
        f"""
        SELECT c.*, COUNT(CASE WHEN g.status = 'published' THEN 1 END) AS gallery_count
        FROM categories c
        LEFT JOIN galleries g ON g.category_id = c.id
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
        "galleryCount": data.get("gallery_count", 0),
        "createdAt": data["created_at"],
    }


def _gallery_dict(row: sqlite3.Row, *, include_images: bool = False) -> dict:
    data = dict(row)
    try:
        directory, _ = resolve_resource_path(data["resource_dir"], allow_root=False)
        count = len(image_files(directory))
    except HTTPException:
        count = 0
    cover = scan_gallery(data["resource_dir"], cover_only=True)
    result = {
        "id": data["id"],
        "categoryId": data["category_id"],
        "categoryName": data.get("category_name"),
        "categorySlug": data.get("category_slug"),
        "title": data["title"],
        "resourceDir": data["resource_dir"],
        "status": data["status"],
        "isFeatured": bool(data["is_featured"]),
        "views": data["views"],
        "createdAt": data["created_at"],
        "updatedAt": data["updated_at"],
        "imageCount": count,
        "coverSrc": cover[0]["src"] if cover else None,
        "coverThumbSrc": cover[0]["thumbSrc"] if cover else None,
    }
    if include_images:
        result["images"] = scan_gallery(data["resource_dir"])
    return result


def _gallery_export_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "categoryId": data["category_id"],
        "title": data["title"],
        "resourceDir": data["resource_dir"],
        "status": data["status"],
        "isFeatured": bool(data["is_featured"]),
        "views": data["views"],
        "createdAt": data["created_at"],
        "updatedAt": data["updated_at"],
    }


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
        "avatarUrl": (
            f"{settings.oidc_issuer}/avatars/{data['auth_sub']}" if data.get("auth_sub") else ""
        ),
        "authSub": data.get("auth_sub"),
        "role": data["role"],
        "isActive": bool(data["is_active"]),
        "createdAt": data["created_at"],
    }


def _comment_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    return {
        "id": data["id"],
        "galleryId": data["gallery_id"],
        "galleryTitle": data.get("gallery_title"),
        "userId": data["user_id"],
        "author": data.get("display_name", "已注销用户"),
        "authorAvatarUrl": (
            f"{settings.oidc_issuer}/avatars/{data['auth_sub']}" if data.get("auth_sub") else ""
        ),
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


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    q: str = Query(default="", max_length=100),
    category: str = Query(default="", max_length=50),
):
    connection = connect()
    try:
        sql = """
            SELECT g.*, c.name AS category_name, c.slug AS category_slug
            FROM galleries g JOIN categories c ON c.id = g.category_id
            WHERE g.status = 'published' AND c.is_active = 1
        """
        params: list[object] = []
        if category:
            sql += " AND c.slug = ?"
            params.append(category)
        if q.strip():
            sql += " AND g.title LIKE ?"
            term = f"%{q.strip()}%"
            params.append(term)
        sql += " ORDER BY g.is_featured DESC, g.updated_at DESC, g.id DESC"
        galleries = [_gallery_dict(row) for row in connection.execute(sql, params).fetchall()]
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
            "galleries": galleries,
            "announcements": announcements,
            "query": q,
            "activeCategory": category,
            "totalGalleries": connection.execute(
                "SELECT COUNT(*) FROM galleries WHERE status = 'published'"
            ).fetchone()[0],
        }
    finally:
        connection.close()
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/galleries/{gallery_id}", response_class=HTMLResponse)
def gallery_detail(request: Request, gallery_id: int):
    with transaction() as connection:
        row = connection.execute(
            """
            SELECT g.*, c.name AS category_name, c.slug AS category_slug
            FROM galleries g JOIN categories c ON c.id = g.category_id
            WHERE g.id = ? AND g.status = 'published' AND c.is_active = 1
            """,
            (gallery_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="图集不存在")
        connection.execute("UPDATE galleries SET views = views + 1 WHERE id = ?", (row["id"],))
        gallery = _gallery_dict(row, include_images=True)
        context = {
            "request": request,
            "site": _site_context(connection),
            "categories": _categories(connection),
            "gallery": gallery,
        }
    return templates.TemplateResponse(request, "gallery.html", context)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = Query(default="/", max_length=1000)):
    try:
        current_user(request)
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
    else:
        return RedirectResponse(safe_return_path(next), status_code=303)
    with transaction() as connection:
        context = {
            "request": request,
            "site": _site_context(connection),
            "next": next,
        }
    return templates.TemplateResponse(request, "login.html", context)


@app.get("/auth/login")
def start_login(
    next: str = Query(default="/", max_length=1000),
    prompt: str | None = Query(default=None),
    screen_hint: str | None = Query(default=None),
):
    return_path = safe_return_path(next)
    return start_oidc_login(
        return_path,
        prompt="none" if prompt == "none" else None,
        screen_hint="signup" if screen_hint == "signup" else None,
    )


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


@app.get("/api/auth/callback")
def oidc_callback(
    request: Request,
    code: str | None = Query(default=None, min_length=1, max_length=4096),
    state: str = Query(min_length=1, max_length=512),
    error: str | None = Query(default=None),
):
    browser_state = request.cookies.get(OIDC_FLOW_COOKIE)
    if error:
        login_state = consume_login_state(state, browser_state)
        destination = request.url_for("login_page").include_query_params(
            next=login_state["return_path"]
        )
        response = RedirectResponse(destination, status_code=303)
        clear_oidc_flow_cookie(response)
        return response
    if not code:
        raise HTTPException(status_code=400, detail="账号中心回调缺少授权码")
    _, return_path, session_token = complete_oidc_login(code, state, browser_state)
    response = RedirectResponse(return_path, status_code=303)
    set_session_cookie(response, session_token)
    clear_oidc_flow_cookie(response)
    return response


@app.post("/api/auth/logout", status_code=204)
def logout(request: Request):
    revoke_current_session(request)
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response


@app.post("/api/auth/backchannel-logout", status_code=204)
def backchannel_logout(logout_token: str = Form(min_length=1, max_length=10000)):
    revoke_backchannel_sessions(logout_token)
    return Response(status_code=204)


@app.api_route("/api/auth/{legacy_action}", methods=["POST", "PUT", "PATCH"])
def legacy_auth_closed(legacy_action: str):
    raise HTTPException(status_code=410, detail="本站已改用 NetHub Accounts 登录")


@app.get("/api/auth/me")
def me(user: Annotated[dict, Depends(current_user)]):
    return {"data": _user_dict(user)}


@app.get("/api/galleries/{gallery_id}/comments")
def list_comments(gallery_id: int):
    connection = connect()
    try:
        rows = connection.execute(
            """
            SELECT c.*, u.display_name, u.auth_sub
            FROM comments c JOIN users u ON u.id = c.user_id
            WHERE c.gallery_id = ? AND c.status = 'visible'
            ORDER BY c.created_at, c.id
            """,
            (gallery_id,),
        ).fetchall()
        return {"data": [_comment_dict(row) for row in rows]}
    finally:
        connection.close()


@app.post("/api/galleries/{gallery_id}/comments", status_code=201)
def create_comment(
    gallery_id: int,
    payload: CommentInput,
    request: Request,
    user: Annotated[dict, Depends(current_user)],
):
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="留言不能为空")
    with transaction() as connection:
        gallery = connection.execute(
            "SELECT id FROM galleries WHERE id = ? AND status = 'published'", (gallery_id,)
        ).fetchone()
        if not gallery:
            raise HTTPException(status_code=404, detail="图集不存在")
        if payload.parent_id:
            parent = connection.execute(
                "SELECT id FROM comments WHERE id = ? AND gallery_id = ? AND status = 'visible'",
                (payload.parent_id, gallery_id),
            ).fetchone()
            if not parent:
                raise HTTPException(status_code=404, detail="回复的留言不存在")
        limit = int(get_setting(connection, "comment_per_minute", "8"))
        _enforce_rate(connection, "comment", str(user["id"]), limit, 60)
        cursor = connection.execute(
            """
            INSERT INTO comments (gallery_id, user_id, parent_id, content, status, created_at)
            VALUES (?, ?, ?, ?, 'visible', ?)
            """,
            (gallery_id, user["id"], payload.parent_id, content, utc_now()),
        )
    return {"data": {"id": cursor.lastrowid}}


@app.get("/api/admin/dashboard")
def admin_dashboard(_: Annotated[dict, Depends(admin_user)]):
    connection = connect()
    try:
        counts = {
            "galleries": connection.execute("SELECT COUNT(*) FROM galleries").fetchone()[0],
            "publishedGalleries": connection.execute(
                "SELECT COUNT(*) FROM galleries WHERE status = 'published'"
            ).fetchone()[0],
            "users": connection.execute(
                "SELECT COUNT(*) FROM users WHERE auth_sub IS NOT NULL"
            ).fetchone()[0],
            "comments": connection.execute("SELECT COUNT(*) FROM comments").fetchone()[0],
            "views": connection.execute("SELECT COALESCE(SUM(views), 0) FROM galleries").fetchone()[0],
        }
        recent = [_gallery_dict(row) for row in connection.execute(
            """
            SELECT g.*, c.name AS category_name, c.slug AS category_slug
            FROM galleries g JOIN categories c ON c.id = g.category_id
            ORDER BY g.updated_at DESC LIMIT 5
            """
        ).fetchall()]
        return {"data": {"counts": counts, "recentGalleries": recent}}
    finally:
        connection.close()


@app.get("/api/admin/users")
def admin_users(_: Annotated[dict, Depends(admin_user)]):
    connection = connect()
    try:
        rows = connection.execute(
            """SELECT id, username, display_name, auth_sub, role, is_active, created_at
               FROM users WHERE auth_sub IS NOT NULL ORDER BY id DESC"""
        ).fetchall()
        return {"data": [_user_dict(row) for row in rows]}
    finally:
        connection.close()


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(
    user_id: int,
    payload: UserUpdateInput,
    operator: Annotated[dict, Depends(admin_user)],
):
    if user_id == operator["id"] and (not payload.is_active or payload.role != "admin"):
        raise HTTPException(status_code=409, detail="不能停用自己或移除自己的管理员角色")
    with transaction() as connection:
        if not connection.execute(
            "SELECT id FROM users WHERE id = ? AND auth_sub IS NOT NULL", (user_id,)
        ).fetchone():
            raise HTTPException(status_code=404, detail="用户不存在")
        connection.execute(
            "UPDATE users SET display_name = ?, role = ?, is_active = ? WHERE id = ?",
            (payload.display_name.strip(), payload.role, int(payload.is_active), user_id),
        )
        if not payload.is_active:
            connection.execute("DELETE FROM local_sessions WHERE user_id = ?", (user_id,))
    return {"data": {"id": user_id}}


@app.delete("/api/admin/users/{user_id}", status_code=204)
def admin_delete_user(
    user_id: int,
    operator: Annotated[dict, Depends(admin_user)],
):
    if user_id == operator["id"]:
        raise HTTPException(status_code=409, detail="不能删除当前登录账号")
    with transaction() as connection:
        cursor = connection.execute(
            "DELETE FROM users WHERE id = ? AND auth_sub IS NOT NULL", (user_id,)
        )
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
        if connection.execute("SELECT COUNT(*) FROM galleries WHERE category_id = ?", (category_id,)).fetchone()[0]:
            raise HTTPException(status_code=409, detail="栏目中仍有图集，不能删除")
        cursor = connection.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="栏目不存在")


@app.get("/api/admin/galleries")
def admin_galleries(_: Annotated[dict, Depends(admin_user)]):
    connection = connect()
    try:
        rows = connection.execute(
            """
            SELECT g.*, c.name AS category_name, c.slug AS category_slug
            FROM galleries g JOIN categories c ON c.id = g.category_id
            ORDER BY g.updated_at DESC, g.id DESC
            """
        ).fetchall()
        return {"data": [_gallery_dict(row) for row in rows]}
    finally:
        connection.close()


def _write_gallery(connection: sqlite3.Connection, payload: GalleryInput, gallery_id: int | None = None) -> int:
    if not connection.execute("SELECT id FROM categories WHERE id = ?", (payload.category_id,)).fetchone():
        raise HTTPException(status_code=422, detail="栏目不存在")
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="图集标题不能为空")
    duplicate_query = "SELECT id FROM galleries WHERE resource_dir = ? COLLATE NOCASE"
    duplicate_params: tuple[object, ...] = (payload.resource_dir,)
    if gallery_id is not None:
        duplicate_query += " AND id != ?"
        duplicate_params += (gallery_id,)
    if connection.execute(duplicate_query, duplicate_params).fetchone():
        raise HTTPException(status_code=409, detail="该资源文件夹已绑定其他图集")
    resource_dir = validate_gallery_directory(
        payload.resource_dir,
        require_images=payload.status == "published",
    )
    thumbnail_result = sync_gallery_thumbnails(resource_dir)
    if thumbnail_result.failed:
        raise HTTPException(status_code=500, detail="图集缩略图生成失败，请检查资源文件")
    now = utc_now()
    values = (
        payload.category_id, title, resource_dir,
        payload.status, int(payload.is_featured), now,
    )
    try:
        if gallery_id is None:
            cursor = connection.execute(
                """
                INSERT INTO galleries
                  (category_id, title, resource_dir, status, is_featured, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                values + (now,),
            )
            return int(cursor.lastrowid)
        cursor = connection.execute(
            """
            UPDATE galleries SET category_id = ?, title = ?, resource_dir = ?,
              status = ?, is_featured = ?, updated_at = ?
            WHERE id = ?
            """,
            values + (gallery_id,),
        )
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="图集不存在")
        return gallery_id
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="该资源文件夹已绑定其他图集") from exc


@app.post("/api/admin/galleries", status_code=201)
def admin_create_gallery(payload: GalleryInput, _: Annotated[dict, Depends(admin_user)]):
    with transaction() as connection:
        gallery_id = _write_gallery(connection, payload)
    return {"data": {"id": gallery_id}}


@app.patch("/api/admin/galleries/{gallery_id}")
def admin_update_gallery(
    gallery_id: int,
    payload: GalleryInput,
    _: Annotated[dict, Depends(admin_user)],
):
    with transaction() as connection:
        _write_gallery(connection, payload, gallery_id)
    return {"data": {"id": gallery_id}}


@app.delete("/api/admin/galleries/{gallery_id}", status_code=204)
def admin_delete_gallery(gallery_id: int, _: Annotated[dict, Depends(admin_user)]):
    with transaction() as connection:
        cursor = connection.execute("DELETE FROM galleries WHERE id = ?", (gallery_id,))
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="图集不存在")


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
            SELECT c.*, u.display_name, u.auth_sub, g.title AS gallery_title
            FROM comments c
            JOIN users u ON u.id = c.user_id
            JOIN galleries g ON g.id = c.gallery_id
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
                "siteName": get_setting(connection, "site_name", "Note Gallery"),
                "siteTagline": get_setting(connection, "site_tagline", ""),
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


@app.get("/api/admin/files/tree")
def admin_file_tree(
    path: str = Query(default="", max_length=500),
    _: dict = Depends(admin_user),
):
    target, relative = resolve_resource_path(path, must_exist=True)
    if not target.is_dir():
        raise HTTPException(status_code=422, detail="只能浏览资源目录")
    items = []
    for item in target.iterdir():
        if item.name == ".thumbs":
            continue
        if item.is_file() and item.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        try:
            items.append(format_file_item(item))
        except (ValueError, OSError):
            continue
    items.sort(key=lambda item: (item["type"] != "folder", item["name"].casefold()))
    return {"path": relative, "url": folder_url(relative), "data": items}


@app.post("/api/admin/files/folders", status_code=201)
def admin_create_folder(
    payload: dict = Body(),
    _: dict = Depends(admin_user),
):
    parent, _ = resolve_resource_path(payload.get("parentPath"), must_exist=True)
    if not parent.is_dir():
        raise HTTPException(status_code=422, detail="目标路径必须是目录")
    name = validate_entry_name(payload.get("name"), "文件夹名称")
    target = parent / name
    if target.exists():
        raise HTTPException(status_code=409, detail="同名文件或文件夹已存在")
    try:
        target.mkdir()
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="同名文件或文件夹已存在") from exc
    return {"data": format_file_item(target)}


@app.post("/api/admin/uploads", status_code=201)
async def admin_upload_file(
    file: UploadFile = File(...),
    target_path: str = Form(default="", alias="targetPath"),
    _: dict = Depends(admin_user),
):
    name = validate_entry_name(file.filename, "文件名")
    if Path(name).suffix.lower() not in IMAGE_EXTENSIONS:
        raise HTTPException(status_code=422, detail="只允许上传 JPG、PNG、WebP 或 GIF 图片")
    target_dir, _ = resolve_resource_path(target_path, must_exist=True)
    if not target_dir.is_dir():
        raise HTTPException(status_code=422, detail="上传目标必须是目录")
    target = target_dir / name
    if target.exists():
        raise HTTPException(status_code=409, detail="同名文件已存在")
    size = 0
    try:
        with target.open("xb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.upload_max_bytes:
                    raise HTTPException(status_code=413, detail="文件超过上传大小限制")
                output.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    if not is_valid_image(target):
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="文件内容不是可识别的图片")
    if await asyncio.to_thread(ensure_thumbnail, target) is None:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="图片缩略图生成失败")
    return {"data": format_file_item(target)}


@app.post("/api/admin/files/folder-upload", status_code=201)
async def admin_upload_folder(
    files: list[UploadFile] = File(...),
    relative_paths: list[str] = Form(..., alias="relativePaths"),
    target_path: str = Form(default="", alias="targetPath"),
    _: dict = Depends(admin_user),
):
    if not files or len(files) != len(relative_paths):
        raise HTTPException(status_code=422, detail="文件与相对路径数量不一致")
    paths = [normalize_folder_upload_path(value) for value in relative_paths]
    keys = [path.as_posix().casefold() for path in paths]
    if len(keys) != len(set(keys)):
        raise HTTPException(status_code=422, detail="文件夹中包含重名文件")
    key_set = set(keys)
    for path in paths:
        for depth in range(2, len(path.parts)):
            if "/".join(path.parts[:depth]).casefold() in key_set:
                raise HTTPException(status_code=422, detail="文件夹内文件和子目录名称冲突")
    roots = {path.parts[0] for path in paths}
    if len(roots) != 1:
        raise HTTPException(status_code=422, detail="一次只能上传一个文件夹")
    target_dir, _ = resolve_resource_path(target_path, must_exist=True)
    if not target_dir.is_dir():
        raise HTTPException(status_code=422, detail="上传目标必须是目录")
    uploaded_root = target_dir / roots.pop()
    if uploaded_root.exists():
        raise HTTPException(status_code=409, detail="同名文件夹已存在")
    total_size = 0
    created = False
    try:
        uploaded_root.mkdir()
        created = True
        for upload, relative in zip(files, paths):
            target = target_dir.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with target.open("xb") as output:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.upload_max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"文件超过上传大小限制：{relative.as_posix()}",
                        )
                    output.write(chunk)
            if not is_valid_image(target):
                raise HTTPException(
                    status_code=422,
                    detail=f"文件内容不是可识别的图片：{relative.as_posix()}",
                )
            if await asyncio.to_thread(ensure_thumbnail, target) is None:
                raise HTTPException(
                    status_code=500,
                    detail=f"图片缩略图生成失败：{relative.as_posix()}",
                )
            total_size += size
    except FileExistsError as exc:
        if created:
            shutil.rmtree(uploaded_root, ignore_errors=True)
        raise HTTPException(status_code=409, detail="文件夹中包含冲突路径") from exc
    except Exception:
        if created:
            shutil.rmtree(uploaded_root, ignore_errors=True)
        raise
    relative = uploaded_root.resolve().relative_to(RESOURCE_DIR.resolve()).as_posix()
    return {
        "folderPath": relative,
        "folderUrl": folder_url(relative),
        "fileCount": len(files),
        "size": total_size,
    }


@app.get("/api/admin/export")
def admin_export(_: Annotated[dict, Depends(admin_user)]):
    connection = connect()
    try:
        payload = {
            "formatVersion": 2,
            "exportedAt": utc_now(),
            "categories": [_category_dict(row) for row in connection.execute(
                """
                SELECT c.*, COUNT(CASE WHEN g.status = 'published' THEN 1 END) AS gallery_count
                FROM categories c LEFT JOIN galleries g ON g.category_id = c.id
                GROUP BY c.id ORDER BY c.sort_order, c.id
                """
            ).fetchall()],
            "galleries": [_gallery_export_dict(row) for row in connection.execute(
                """
                SELECT g.*, c.name AS category_name, c.slug AS category_slug
                FROM galleries g JOIN categories c ON c.id = g.category_id ORDER BY g.id
                """
            ).fetchall()],
            "announcements": [_announcement_dict(row) for row in connection.execute(
                "SELECT * FROM announcements ORDER BY id"
            ).fetchall()],
        }
    finally:
        connection.close()
    return JSONResponse(payload, headers={"Content-Disposition": "attachment; filename=cas-gallery-export.json"})


@app.post("/api/admin/import")
def admin_import(
    payload: dict = Body(),
    _: dict = Depends(admin_user),
):
    if payload.get("formatVersion") != 2:
        raise HTTPException(status_code=422, detail="不支持的数据格式版本")
    imported = {"categories": 0, "galleries": 0, "announcements": 0}
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
        for item in payload.get("galleries", []):
            if connection.execute(
                "SELECT id FROM galleries WHERE resource_dir = ?", (item.get("resourceDir"),)
            ).fetchone():
                continue
            category_id = category_map.get(int(item.get("categoryId", 0)))
            if not category_id:
                continue
            model = GalleryInput(
                category_id=category_id, title=item.get("title", ""),
                resource_dir=item.get("resourceDir", ""),
                status=item.get("status", "draft"), is_featured=item.get("isFeatured", False),
            )
            _write_gallery(connection, model)
            imported["galleries"] += 1
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
