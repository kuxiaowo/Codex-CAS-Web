"""受控图集资源目录、图片扫描与缩略图生成。"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from fastapi import HTTPException

from app.config import PROJECT_ROOT, settings


RESOURCE_DIR = PROJECT_ROOT / "resources"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
THUMB_DIR_NAME = ".thumbs"
WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:[/\\]")
WINDOWS_FILENAME_RESERVED_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def ensure_resource_root() -> None:
    RESOURCE_DIR.mkdir(parents=True, exist_ok=True)


def normalize_resource_path(value: str | None, *, allow_root: bool = True) -> str:
    raw = (value or "").strip().replace("\\", "/")
    if raw.startswith("/") or WINDOWS_DRIVE_PATTERN.match(raw) or "://" in raw:
        raise HTTPException(status_code=422, detail="资源路径不合法")
    raw = raw.strip("/")
    if not raw:
        if allow_root:
            return ""
        raise HTTPException(status_code=422, detail="请选择 resources 下的子文件夹")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise HTTPException(status_code=422, detail="资源路径不合法")
    return path.as_posix()


def resolve_resource_path(
    value: str | None,
    *,
    allow_root: bool = True,
    must_exist: bool = False,
) -> tuple[Path, str]:
    relative = normalize_resource_path(value, allow_root=allow_root)
    root = RESOURCE_DIR.resolve()
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=422, detail="资源路径不合法")
    if must_exist and not target.exists():
        raise HTTPException(status_code=404, detail="资源目录不存在")
    return target, relative


def validate_entry_name(value: str | None, field_name: str = "名称") -> str:
    name = str(value or "").strip()
    if not name or name in {".", ".."}:
        raise HTTPException(status_code=422, detail=f"{field_name}不能为空")
    if WINDOWS_FILENAME_RESERVED_CHARS.search(name):
        raise HTTPException(status_code=422, detail=f"{field_name}包含不允许的字符")
    if name.endswith((" ", ".")) or len(name) > 255:
        raise HTTPException(status_code=422, detail=f"{field_name}不合法")
    if name.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        raise HTTPException(status_code=422, detail=f"{field_name}是系统保留名称")
    return name


def normalize_folder_upload_path(value: str) -> PurePosixPath:
    raw = str(value or "").replace("\\", "/")
    if raw.startswith("/") or WINDOWS_DRIVE_PATTERN.match(raw):
        raise HTTPException(status_code=422, detail="文件夹内路径不合法")
    parts = raw.split("/")
    if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=422, detail="文件夹上传必须保留顶层文件夹且不能越级")
    normalized = PurePosixPath(*(validate_entry_name(part, "文件夹内路径") for part in parts))
    if normalized.suffix.lower() not in IMAGE_EXTENSIONS:
        raise HTTPException(status_code=422, detail=f"只允许上传图片：{normalized.as_posix()}")
    return normalized


def natural_sort_key(path: Path) -> list[int | str]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", path.name)]


def image_files(directory: Path) -> list[Path]:
    if not directory.exists() or not directory.is_dir():
        return []
    root = RESOURCE_DIR.resolve()
    files = []
    for item in sorted(directory.iterdir(), key=natural_sort_key):
        try:
            resolved = item.resolve()
        except OSError:
            continue
        if (
            item.is_file()
            and item.suffix.lower() in IMAGE_EXTENSIONS
            and (resolved == root or root in resolved.parents)
            and is_valid_image(item)
        ):
            files.append(item)
    return files


def is_valid_image(path: Path) -> bool:
    try:
        from PIL import Image, UnidentifiedImageError
        with Image.open(path) as image:
            image.verify()
        return True
    except (ImportError, OSError, UnidentifiedImageError, ValueError):
        return False


def resource_url(path: Path) -> str:
    relative = path.resolve().relative_to(RESOURCE_DIR.resolve()).as_posix()
    version = path.stat().st_mtime_ns
    return f"/resources/{quote(relative, safe='/')}?v={version}"


def folder_url(relative: str) -> str:
    encoded = quote(relative, safe="/")
    return f"/resources/{encoded}/" if encoded else "/resources/"


def ensure_thumbnail(source: Path) -> str | None:
    """按源图修改时间缓存一个保持比例的 WebP 预览图。"""

    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError:
        return None

    thumb_dir = source.parent / THUMB_DIR_NAME
    thumb_path = thumb_dir / f"{source.name}.webp"
    try:
        if not thumb_path.is_file() or thumb_path.stat().st_mtime_ns < source.stat().st_mtime_ns:
            thumb_dir.mkdir(exist_ok=True)
            with Image.open(source) as opened:
                image = ImageOps.exif_transpose(opened)
                if getattr(image, "is_animated", False):
                    image.seek(0)
                image.thumbnail((settings.thumbnail_max_width, settings.thumbnail_max_height))
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGB")
                image.save(
                    thumb_path,
                    "WEBP",
                    quality=settings.thumbnail_webp_quality,
                    method=settings.thumbnail_webp_method,
                )
            source_stat = source.stat()
            os.utime(thumb_path, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
        return resource_url(thumb_path)
    except (OSError, UnidentifiedImageError, ValueError):
        return None


def scan_gallery(resource_dir: str, *, cover_only: bool = False) -> list[dict]:
    try:
        directory, _ = resolve_resource_path(resource_dir, allow_root=False)
    except HTTPException:
        return []
    files = image_files(directory)
    if cover_only:
        files = files[:1]
    images = []
    for index, source in enumerate(files, start=1):
        original = resource_url(source)
        images.append(
            {
                "index": index,
                "name": source.name,
                "src": original,
                "thumbSrc": ensure_thumbnail(source) or original,
            }
        )
    return images


def validate_gallery_directory(resource_dir: str, *, require_images: bool = False) -> str:
    target, relative = resolve_resource_path(resource_dir, allow_root=False, must_exist=True)
    if not target.is_dir():
        raise HTTPException(status_code=422, detail="资源路径必须指向文件夹")
    if require_images and not any(is_valid_image(path) for path in image_files(target)):
        raise HTTPException(status_code=422, detail="发布图集前，资源文件夹中至少需要一张图片")
    return relative


def format_file_item(path: Path) -> dict:
    root = RESOURCE_DIR.resolve()
    relative = path.resolve().relative_to(root).as_posix()
    is_dir = path.is_dir()
    stat = path.stat()
    return {
        "name": path.name,
        "path": relative,
        "url": folder_url(relative) if is_dir else resource_url(path),
        "type": "folder" if is_dir else "file",
        "size": None if is_dir else stat.st_size,
        "updatedAt": stat.st_mtime,
    }
