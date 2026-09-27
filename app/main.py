"""FastAPI 一体化入口：同一端口提供页面、静态资源和 JSON API。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

from fastapi import BackgroundTasks, Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

from app.auth import (
    admin_user,
    browser_request_is_same_origin,
    clear_oidc_flow_cookie,
    clear_session_cookie,
    complete_oidc_login,
    consume_login_state,
    current_user,
    oidc_flow_cookie_name,
    revoke_backchannel_sessions,
    revoke_current_session,
    safe_return_path,
    set_session_cookie,
    start_oidc_login,
)
from app.config import PROJECT_ROOT, settings, validate_runtime_settings
from app.database import D1GatewayAdapter, D1IntegrityError, connect, get_setting, initialize_database, transaction, utc_now
from app.gallery_assets import IMAGE_EXTENSIONS, RESOURCE_DIR, normalize_folder_upload_path, normalize_resource_path, validate_entry_name
from app.media_library import (
    create_folder,
    create_thumbnail,
    delete_image,
    folder_url,
    gallery_images,
    gallery_summary,
    list_directory,
    protected_download_url,
    upload_prepared_batch,
    upload_prepared_image,
    validate_gallery_directory,
    validate_image_file,
)
from app.media_storage import MediaStorageError, close_media_storage
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
@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_runtime_settings()
    if settings.media_storage_backend == "local":
        RESOURCE_DIR.mkdir(parents=True, exist_ok=True)
    initialize_database()
    try:
        yield
    finally:
        close_media_storage()


app = FastAPI(
    title="Note Gallery",
    description="一体化图片图集站",
    version="0.1.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
if settings.media_storage_backend == "local":
    app.mount("/resources", StaticFiles(directory=RESOURCE_DIR), name="resources")
templates = Jinja2Templates(directory=TEMPLATE_DIR)


@app.exception_handler(MediaStorageError)
async def media_storage_error_handler(_: Request, exc: MediaStorageError):
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


@app.middleware("http")
async def no_store_dynamic_pages(request: Request, call_next):
    if (
        request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and request.url.path != "/api/auth/backchannel-logout"
        and not browser_request_is_same_origin(request)
    ):
        return JSONResponse(status_code=403, content={"detail": "拒绝跨站请求"})
    response = await call_next(request)
    path = request.url.path
    is_public_gallery_page = request.method == "GET" and (
        path == "/" or (path.startswith("/galleries/") and path.removeprefix("/galleries/").isdigit())
    )
    if is_public_gallery_page:
        # Gallery HTML is public. Keep the shared cache short enough for edits
        # to become visible while avoiding a full origin render per visitor.
        response.headers["Cache-Control"] = "public, max-age=60, s-maxage=300, must-revalidate"
    elif not path.startswith(("/static/", "/resources/")):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    elif path.startswith("/resources/"):
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
    rows = connection.execute(_categories_sql(include_inactive=include_inactive)).fetchall()
    return [_category_dict(row) for row in rows]


def _categories_sql(*, include_inactive: bool = False) -> str:
    where = "" if include_inactive else "WHERE c.is_active = 1"
    return f"""
        SELECT c.*, COUNT(CASE WHEN g.status = 'published' THEN 1 END) AS gallery_count
        FROM categories c
        LEFT JOIN galleries g ON g.category_id = c.id
        {where}
        GROUP BY c.id
        ORDER BY c.sort_order, c.id
        """


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
        summary = gallery_summary(data["resource_dir"])
    except (HTTPException, MediaStorageError, ValueError):
        summary = {"imageCount": 0, "cover": None}
    cover = summary["cover"]
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
        "imageCount": summary["imageCount"],
        "coverSrc": cover["src"] if cover else None,
        "coverThumbSrc": cover["thumbSrc"] if cover else None,
    }
    if include_images:
        page = gallery_images(data["resource_dir"], limit=30)
        result["images"] = page.images
        result["imagesNextCursor"] = page.next_cursor
        result["imagesHasMore"] = page.has_more
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
        announcements_sql = """
            SELECT * FROM announcements WHERE status = 'published'
            ORDER BY is_pinned DESC, created_at DESC LIMIT 3
        """
        if isinstance(connection, D1GatewayAdapter):
            gallery_rows, announcement_rows, setting_rows, category_rows, count_rows = (
                cursor.fetchall()
                for cursor in connection.batch([
                    {"sql": sql, "params": params},
                    {"sql": announcements_sql, "params": []},
                    {"sql": "SELECT key, value FROM settings WHERE key IN (?, ?)",
                     "params": ["site_name", "site_tagline"]},
                    {"sql": _categories_sql(), "params": []},
                    {"sql": "SELECT COUNT(*) AS count FROM galleries WHERE status = 'published'", "params": []},
                ])
            )
            settings_values = {row["key"]: row["value"] for row in setting_rows}
            site = {
                "siteName": settings_values.get("site_name", "Note Gallery"),
                "siteTagline": settings_values.get("site_tagline", "一个简单的笔记集合站。"),
            }
            categories = [_category_dict(row) for row in category_rows]
            total_galleries = count_rows[0]["count"]
        else:
            gallery_rows = connection.execute(sql, params).fetchall()
            announcement_rows = connection.execute(announcements_sql).fetchall()
            site = _site_context(connection)
            categories = _categories(connection)
            total_galleries = connection.execute(
                "SELECT COUNT(*) FROM galleries WHERE status = 'published'"
            ).fetchone()[0]
        context = {
            "request": request,
            "site": site,
            "categories": categories,
            "galleries": [_gallery_dict(row) for row in gallery_rows],
            "announcements": [_announcement_dict(row) for row in announcement_rows],
            "query": q,
            "activeCategory": category,
            "totalGalleries": total_galleries,
        }
    finally:
        connection.close()
    return templates.TemplateResponse(request, "index.html", context)


def _increment_gallery_views(gallery_id: int) -> None:
    try:
        with transaction() as connection:
            connection.execute("UPDATE galleries SET views = views + 1 WHERE id = ?", (gallery_id,))
    except Exception:
        # A view counter must never turn a successful page render into a 500.
        return


@app.get("/galleries/{gallery_id}", response_class=HTMLResponse)
def gallery_detail(request: Request, gallery_id: int, background_tasks: BackgroundTasks):
    connection = connect()
    try:
        if isinstance(connection, D1GatewayAdapter):
            batch_results = connection.batch([
                {
                    "sql": """
                        SELECT g.*, c.name AS category_name, c.slug AS category_slug
                        FROM galleries g JOIN categories c ON c.id = g.category_id
                        WHERE g.id = ? AND g.status = 'published' AND c.is_active = 1
                    """,
                    "params": [gallery_id],
                },
                {"sql": "SELECT key, value FROM settings WHERE key IN (?, ?)", "params": ["site_name", "site_tagline"]},
                {"sql": _categories_sql(), "params": []},
            ])
            row = batch_results[0].fetchone()
            setting_rows = batch_results[1].fetchall()
            category_rows = batch_results[2].fetchall()
            if not row:
                raise HTTPException(status_code=404, detail="图集不存在")
            settings_values = {item["key"]: item["value"] for item in setting_rows}
            site = {
                "siteName": settings_values.get("site_name", "Note Gallery"),
                "siteTagline": settings_values.get("site_tagline", "一个简单的笔记集合站。"),
            }
            categories = [_category_dict(item) for item in category_rows]
        else:
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
            site = _site_context(connection)
            categories = _categories(connection)
    finally:
        connection.close()

    gallery = _gallery_dict(row, include_images=True)
    background_tasks.add_task(_increment_gallery_views, gallery_id)
    context = {
        "request": request,
        "site": site,
        "categories": categories,
        "gallery": gallery,
    }
    return templates.TemplateResponse(request, "gallery.html", context)


@app.get("/api/galleries/{gallery_id}/images")
def gallery_image_page(
    gallery_id: int,
    cursor: str | None = Query(default=None, max_length=2048),
    limit: int = Query(default=30, ge=1, le=100),
):
    connection = connect()
    try:
        row = connection.execute(
            """
            SELECT g.resource_dir
            FROM galleries g JOIN categories c ON c.id = g.category_id
            WHERE g.id = ? AND g.status = 'published' AND c.is_active = 1
            """,
            (gallery_id,),
        ).fetchone()
    finally:
        connection.close()
    if not row:
        raise HTTPException(status_code=404, detail="图集不存在")
    try:
        page = gallery_images(row["resource_dir"], cursor=cursor, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="图片分页游标不合法") from exc
    return {
        "data": page.images,
        "nextCursor": page.next_cursor,
        "hasMore": page.has_more,
    }


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
            "auth_error": None,
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
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
):
    if not state or len(state) > 512:
        detail = "登录请求缺少有效状态，请重新登录"
        with transaction() as connection:
            context = {
                "request": request,
                "site": _site_context(connection),
                "next": "/",
                "auth_error": detail,
            }
        return templates.TemplateResponse(
            request, "login.html", context, status_code=400
        )

    browser_state = request.cookies.get(oidc_flow_cookie_name(state))
    try:
        if error:
            login_state = consume_login_state(state, browser_state)
            destination = request.url_for("login_page").include_query_params(
                next=login_state["return_path"]
            )
            response = RedirectResponse(destination, status_code=303)
            clear_oidc_flow_cookie(response, state)
            return response
        if not code or len(code) > 4096:
            raise HTTPException(status_code=400, detail="账号中心回调缺少有效授权码")
        _, return_path, session_token = complete_oidc_login(code, state, browser_state)
    except HTTPException as exc:
        with transaction() as connection:
            context = {
                "request": request,
                "site": _site_context(connection),
                "next": "/",
                "auth_error": str(exc.detail),
            }
        response = templates.TemplateResponse(
            request, "login.html", context, status_code=exc.status_code
        )
        clear_oidc_flow_cookie(response, state)
        return response

    response = RedirectResponse(return_path, status_code=303)
    set_session_cookie(response, session_token)
    clear_oidc_flow_cookie(response, state)
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
    connection = connect()
    if isinstance(connection, D1GatewayAdapter):
        try:
            limit = int(get_setting(connection, "comment_per_minute", "8"))
            threshold = (datetime.now(UTC) - timedelta(seconds=60)).isoformat(timespec="seconds")
            row = connection.execute(
                """
                INSERT INTO comments (gallery_id, user_id, parent_id, content, status, created_at)
                SELECT ?, ?, ?, ?, 'visible', ?
                WHERE EXISTS (SELECT 1 FROM galleries WHERE id = ? AND status = 'published')
                  AND (? IS NULL OR EXISTS (
                    SELECT 1 FROM comments
                    WHERE id = ? AND gallery_id = ? AND status = 'visible'
                  ))
                  AND (
                    SELECT COUNT(*) FROM comments
                    WHERE user_id = ? AND created_at >= ?
                  ) < ?
                RETURNING id
                """,
                (
                    gallery_id, user["id"], payload.parent_id, content, utc_now(), gallery_id,
                    payload.parent_id, payload.parent_id, gallery_id,
                    user["id"], threshold, limit,
                ),
            ).fetchone()
            if row is not None:
                return {"data": {"id": row["id"]}}
            gallery = connection.execute(
                "SELECT id FROM galleries WHERE id = ? AND status = 'published'", (gallery_id,)
            ).fetchone()
            if not gallery:
                raise HTTPException(status_code=404, detail="图集不存在")
            if payload.parent_id and not connection.execute(
                "SELECT id FROM comments WHERE id = ? AND gallery_id = ? AND status = 'visible'",
                (payload.parent_id, gallery_id),
            ).fetchone():
                raise HTTPException(status_code=404, detail="回复的留言不存在")
            raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")
        finally:
            connection.close()
    connection.close()
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
        if isinstance(connection, D1GatewayAdapter):
            statements = [{
                "sql": (
                    "UPDATE users SET display_name = ?, role = ?, is_active = ? "
                    "WHERE id = ? AND auth_sub IS NOT NULL RETURNING id"
                ),
                "params": [payload.display_name.strip(), payload.role, int(payload.is_active), user_id],
            }]
            if not payload.is_active:
                statements.append({
                    "sql": "DELETE FROM local_sessions WHERE user_id = ?",
                    "params": [user_id],
                })
            results = connection.batch(statements)
            if not results[0].fetchone():
                raise HTTPException(status_code=404, detail="用户不存在")
            return {"data": {"id": user_id}}
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
                VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id
                """,
                (
                    payload.name.strip(), payload.slug, payload.description.strip(), payload.accent,
                    payload.sort_order, int(payload.is_active), utc_now(),
                ),
            )
        except (sqlite3.IntegrityError, D1IntegrityError) as exc:
            raise HTTPException(status_code=409, detail="栏目名称或标识已存在") from exc
        row = cursor.fetchone()
        category_id = row["id"] if row else cursor.lastrowid
    return {"data": {"id": category_id}}


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
        except (sqlite3.IntegrityError, D1IntegrityError) as exc:
            raise HTTPException(status_code=409, detail="栏目名称或标识已存在") from exc
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="栏目不存在")
    return {"data": {"id": category_id}}


@app.delete("/api/admin/categories/{category_id}", status_code=204)
def admin_delete_category(category_id: int, _: Annotated[dict, Depends(admin_user)]):
    with transaction() as connection:
        if isinstance(connection, D1GatewayAdapter):
            deleted = connection.execute(
                """DELETE FROM categories
                   WHERE id = ? AND NOT EXISTS (
                     SELECT 1 FROM galleries WHERE category_id = categories.id
                   ) RETURNING id""",
                (category_id,),
            ).fetchone()
            if deleted:
                return
            if connection.execute("SELECT 1 FROM categories WHERE id = ?", (category_id,)).fetchone():
                raise HTTPException(status_code=409, detail="栏目中仍有图集，不能删除")
            raise HTTPException(status_code=404, detail="栏目不存在")
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


def _write_gallery(connection: sqlite3.Connection | D1GatewayAdapter, payload: GalleryInput, gallery_id: int | None = None) -> int:
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
                VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id
                """,
                values + (now,),
            )
            row = cursor.fetchone()
            return int(row["id"] if row else cursor.lastrowid)
        cursor = connection.execute(
            """
            UPDATE galleries SET category_id = ?, title = ?, resource_dir = ?,
              status = ?, is_featured = ?, updated_at = ?
            WHERE id = ? RETURNING id
            """,
            values + (gallery_id,),
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="图集不存在")
        return gallery_id
    except (sqlite3.IntegrityError, D1IntegrityError) as exc:
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
            VALUES (?, ?, ?, ?, ?, ?) RETURNING id
            """,
            (payload.title.strip(), payload.content.strip(), payload.status, int(payload.is_pinned), now, now),
        )
        row = cursor.fetchone()
        announcement_id = row["id"] if row else cursor.lastrowid
    return {"data": {"id": announcement_id}}


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
    connection = connect()
    if isinstance(connection, D1GatewayAdapter):
        try:
            now = utc_now()
            connection.batch([
                {
                    "sql": (
                        "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at"
                    ),
                    "params": [key, value, now],
                }
                for key, value in values.items()
            ])
        finally:
            connection.close()
        return {"data": values}
    connection.close()
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
    relative = normalize_resource_path(path)
    if relative:
        validate_gallery_directory(relative)
    items = list_directory(relative)
    return {"path": relative, "url": folder_url(relative), "data": items}


@app.post("/api/admin/files/folders", status_code=201)
def admin_create_folder(
    payload: dict = Body(),
    _: dict = Depends(admin_user),
):
    parent = normalize_resource_path(payload.get("parentPath"))
    if parent:
        validate_gallery_directory(parent)
    return {"data": create_folder(parent, payload.get("name"))}


async def _save_upload(upload: UploadFile, target: Path, *, display_name: str) -> int:
    size = 0
    with target.open("xb") as output:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > settings.upload_max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"文件超过上传大小限制：{display_name}",
                )
            output.write(chunk)
    return size


@app.post("/api/admin/uploads", status_code=201)
async def admin_upload_file(
    file: UploadFile = File(...),
    target_path: str = Form(default="", alias="targetPath"),
    _: dict = Depends(admin_user),
):
    name = validate_entry_name(file.filename, "文件名")
    if Path(name).suffix.lower() not in IMAGE_EXTENSIONS:
        raise HTTPException(status_code=422, detail="只允许上传 JPG、PNG、WebP 或 GIF 图片")
    target_path = normalize_resource_path(target_path)
    if target_path:
        await asyncio.to_thread(validate_gallery_directory, target_path)
    relative = f"{target_path}/{name}" if target_path else name
    with TemporaryDirectory(prefix="cas-upload-") as temporary_dir:
        source = Path(temporary_dir) / "source"
        thumbnail = Path(temporary_dir) / "thumbnail.webp"
        await _save_upload(file, source, display_name=name)
        await asyncio.to_thread(validate_image_file, source, name)
        await asyncio.to_thread(create_thumbnail, source, thumbnail)
        item = await asyncio.to_thread(upload_prepared_image, relative, source, thumbnail)
    return {"data": {**item, "path": relative, "type": "file", "url": item["src"]}}


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
    target_path = normalize_resource_path(target_path)
    if target_path:
        await asyncio.to_thread(validate_gallery_directory, target_path)
    root_name = roots.pop()
    uploaded_root = f"{target_path}/{root_name}" if target_path else root_name
    try:
        await asyncio.to_thread(validate_gallery_directory, uploaded_root)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
    else:
        raise HTTPException(status_code=409, detail="同名文件夹已存在")
    total_size = 0
    prepared: list[tuple[str, Path, Path]] = []
    with TemporaryDirectory(prefix="cas-folder-upload-") as temporary_dir:
        temporary_root = Path(temporary_dir)
        for index, (upload, relative) in enumerate(zip(files, paths)):
            source = temporary_root / f"source-{index}"
            thumbnail = temporary_root / f"thumbnail-{index}.webp"
            size = await _save_upload(upload, source, display_name=relative.as_posix())
            try:
                await asyncio.to_thread(validate_image_file, source, relative.name)
                await asyncio.to_thread(create_thumbnail, source, thumbnail)
            except HTTPException as exc:
                exc.detail = f"{exc.detail}：{relative.as_posix()}"
                raise
            object_path = f"{target_path}/{relative.as_posix()}" if target_path else relative.as_posix()
            prepared.append((object_path, source, thumbnail))
            total_size += size
        await asyncio.to_thread(upload_prepared_batch, prepared)
    return {
        "folderPath": uploaded_root,
        "folderUrl": folder_url(uploaded_root),
        "fileCount": len(files),
        "size": total_size,
    }


@app.get("/api/admin/files/download")
def admin_file_download(
    path: str = Query(max_length=500),
    _: dict = Depends(admin_user),
):
    return RedirectResponse(protected_download_url(path), status_code=307)


@app.delete("/api/admin/files", status_code=204)
def admin_delete_file(
    path: str = Query(max_length=500),
    _: dict = Depends(admin_user),
):
    delete_image(path)
    return Response(status_code=204)


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
        if isinstance(connection, D1GatewayAdapter):
            statements: list[dict] = []
            result_kinds: list[str] = []
            category_slugs: dict[int, str] = {}
            for item in payload.get("categories", []):
                model = CategoryInput(
                    name=item.get("name", ""), slug=item.get("slug", ""),
                    description=item.get("description", ""), accent=item.get("accent", "#8b7cff"),
                    sort_order=item.get("sortOrder", 10), is_active=item.get("isActive", True),
                )
                category_slugs[int(item.get("id", 0))] = model.slug
                statements.append({
                    "sql": (
                        "INSERT INTO categories "
                        "(name, slug, description, accent, sort_order, is_active, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(slug) DO NOTHING RETURNING id"
                    ),
                    "params": [model.name, model.slug, model.description, model.accent,
                               model.sort_order, int(model.is_active), utc_now()],
                })
                result_kinds.append("categories")
            for item in payload.get("galleries", []):
                category_slug = category_slugs.get(int(item.get("categoryId", 0)))
                if not category_slug:
                    continue
                model = GalleryInput(
                    category_id=1, title=item.get("title", ""),
                    resource_dir=item.get("resourceDir", ""),
                    status=item.get("status", "draft"), is_featured=item.get("isFeatured", False),
                )
                title = model.title.strip()
                if not title:
                    raise HTTPException(status_code=422, detail="图集标题不能为空")
                resource_dir = validate_gallery_directory(
                    model.resource_dir, require_images=model.status == "published"
                )
                now = utc_now()
                statements.append({
                    "sql": (
                        "INSERT INTO galleries "
                        "(category_id, title, resource_dir, status, is_featured, created_at, updated_at) "
                        "SELECT c.id, ?, ?, ?, ?, ?, ? FROM categories c WHERE c.slug = ? "
                        "AND NOT EXISTS (SELECT 1 FROM galleries WHERE resource_dir = ? COLLATE NOCASE) "
                        "RETURNING id"
                    ),
                    "params": [title, resource_dir, model.status, int(model.is_featured), now, now,
                               category_slug, resource_dir],
                })
                result_kinds.append("galleries")
            for item in payload.get("announcements", []):
                model = AnnouncementInput(
                    title=item.get("title", ""), content=item.get("content", ""),
                    status=item.get("status", "published"), is_pinned=item.get("isPinned", False),
                )
                now = utc_now()
                statements.append({
                    "sql": (
                        "INSERT INTO announcements "
                        "(title, content, status, is_pinned, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?) RETURNING id"
                    ),
                    "params": [model.title, model.content, model.status, int(model.is_pinned), now, now],
                })
                result_kinds.append("announcements")
            if len(statements) > 100:
                raise HTTPException(status_code=422, detail="一次最多导入 100 条数据")
            if statements:
                try:
                    results = connection.batch(statements)
                except D1IntegrityError as exc:
                    raise HTTPException(status_code=409, detail="导入数据与现有内容冲突") from exc
                for kind, result in zip(result_kinds, results):
                    if result.fetchone():
                        imported[kind] += 1
            return {"data": imported}
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
