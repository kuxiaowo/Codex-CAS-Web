"""受控图集资源目录、图片扫描与缩略图生成。"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from fastapi import HTTPException

from app.config import PROJECT_ROOT, settings


RESOURCE_DIR = PROJECT_ROOT / "resources"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
THUMB_DIR_NAME = ".thumbs"
_THUMBNAIL_LOCK = threading.RLock()
WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:[/\\]")
WINDOWS_FILENAME_RESERVED_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


@dataclass(frozen=True)
class ThumbnailSyncResult:
    scanned: int = 0
    generated: int = 0
    current: int = 0
    removed: int = 0
    failed: int = 0


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


def thumbnail_path(source: Path) -> Path:
    return source.parent / THUMB_DIR_NAME / f"{source.name}.webp"


def _thumbnail_is_current(source: Path, thumb_path: Path) -> bool:
    try:
        if not thumb_path.is_file() or thumb_path.stat().st_mtime_ns < source.stat().st_mtime_ns:
            return False
        from PIL import Image
        with Image.open(thumb_path) as image:
            return (
                image.format == "WEBP"
                and image.width <= settings.thumbnail_max_width
                and image.height <= settings.thumbnail_max_height
            )
    except (ImportError, OSError, ValueError):
        return False


def ensure_thumbnail(source: Path) -> str | None:
    """按源图修改时间缓存一个保持比例的 WebP 预览图。"""

    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError:
        return None

    thumb_path = thumbnail_path(source)
    temporary = thumb_path.with_name(
        f".{thumb_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    with _THUMBNAIL_LOCK:
        try:
            if not _thumbnail_is_current(source, thumb_path):
                thumb_path.parent.mkdir(exist_ok=True)
                with Image.open(source) as opened:
                    image = ImageOps.exif_transpose(opened)
                    if getattr(image, "is_animated", False):
                        image.seek(0)
                    image.thumbnail((settings.thumbnail_max_width, settings.thumbnail_max_height))
                    if image.mode not in {"RGB", "RGBA"}:
                        image = image.convert("RGB")
                    image.save(
                        temporary,
                        "WEBP",
                        quality=settings.thumbnail_webp_quality,
                        method=settings.thumbnail_webp_method,
                    )
                os.replace(temporary, thumb_path)
            return resource_url(thumb_path)
        except (OSError, UnidentifiedImageError, ValueError):
            return None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _sync_sources(sources: list[Path], thumb_dirs: list[Path]) -> ThumbnailSyncResult:
    expected = {thumbnail_path(source).resolve() for source in sources}
    generated = 0
    current = 0
    failed = 0
    for source in sources:
        thumb = thumbnail_path(source)
        if _thumbnail_is_current(source, thumb):
            current += 1
        elif ensure_thumbnail(source) is None:
            failed += 1
        else:
            generated += 1

    removed = 0
    for thumb_dir in thumb_dirs:
        if not thumb_dir.is_dir() or thumb_dir.is_symlink():
            continue
        for item in thumb_dir.iterdir():
            try:
                if item.is_file() and item.suffix.casefold() == ".webp" and item.resolve() not in expected:
                    item.unlink()
                    removed += 1
            except OSError:
                failed += 1
    return ThumbnailSyncResult(
        scanned=len(sources), generated=generated, current=current,
        removed=removed, failed=failed,
    )


def sync_gallery_thumbnails(resource_dir: str) -> ThumbnailSyncResult:
    """立即同步一个图集当前层的全部缩略图。"""

    directory, _ = resolve_resource_path(resource_dir, allow_root=False, must_exist=True)
    if not directory.is_dir():
        return ThumbnailSyncResult(failed=1)
    return _sync_sources(image_files(directory), [directory / THUMB_DIR_NAME])


def sync_all_thumbnails() -> ThumbnailSyncResult:
    """递归同步 resources 中的缩略图，并清理已删除源图对应的缓存。"""

    ensure_resource_root()
    root = RESOURCE_DIR.resolve()
    sources: list[Path] = []
    thumb_dirs: list[Path] = []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        directory = Path(current)
        safe_directories: list[str] = []
        for name in directory_names:
            candidate = directory / name
            if name.casefold() == THUMB_DIR_NAME.casefold():
                thumb_dirs.append(candidate)
                continue
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if not candidate.is_symlink() and (resolved == root or root in resolved.parents):
                safe_directories.append(name)
        directory_names[:] = safe_directories
        for name in file_names:
            source = directory / name
            if source.suffix.casefold() not in IMAGE_EXTENSIONS or source.is_symlink():
                continue
            try:
                resolved = source.resolve()
            except OSError:
                continue
            if root in resolved.parents and is_valid_image(source):
                sources.append(source)
    sources.sort(key=lambda path: natural_sort_key(path))
    return _sync_sources(sources, thumb_dirs)


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
                "thumbSrc": ensure_thumbnail(source),
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
