"""图集对象布局、缩略图、分页与目录摘要。"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import settings
from app.gallery_assets import (
    IMAGE_EXTENSIONS,
    normalize_resource_path,
    validate_entry_name,
)
from app.media_storage import MediaObject, MediaStorageError, get_media_storage


_manifest_locks_guard = threading.Lock()
_manifest_locks: dict[str, threading.Lock] = {}
_summary_lock = threading.Lock()
_summary_cache: dict[str, tuple[float, dict]] = {}
_SUMMARY_CACHE_TTL_SECONDS = 300


@dataclass(frozen=True)
class GalleryPage:
    images: list[dict]
    next_cursor: str | None
    has_more: bool


def gallery_key(relative_path: str) -> str:
    return f"galleries/{normalize_resource_path(relative_path, allow_root=False)}"


def thumbnail_key(relative_path: str) -> str:
    return f"thumbnails/{normalize_resource_path(relative_path, allow_root=False)}.webp"


def manifest_key(resource_dir: str) -> str:
    return f"manifests/{normalize_resource_path(resource_dir, allow_root=False)}.json"


def folder_url(relative: str) -> str:
    relative = normalize_resource_path(relative)
    return f"/resources/{relative}/" if relative else "/resources/"


def _direct_child(key: str, prefix: str) -> str | None:
    relative = key.removeprefix(prefix)
    return relative if relative and "/" not in relative else None


def _natural_name_key(item: MediaObject) -> list[int | str]:
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", PurePosixPath(item.key).name)
    ]


def _image_dict(item: MediaObject, resource_dir: str) -> dict:
    name = PurePosixPath(item.key).name
    relative = f"{resource_dir}/{name}" if resource_dir else name
    storage = get_media_storage()
    return {
        "name": name,
        "src": storage.media_url(gallery_key(relative)),
        "thumbSrc": storage.media_url(thumbnail_key(relative)),
        "size": item.size,
        "updatedAt": item.uploaded,
        "sha256": item.sha256,
    }


def gallery_images(
    resource_dir: str,
    *,
    cursor: str | None = None,
    limit: int = 30,
) -> GalleryPage:
    resource_dir = normalize_resource_path(resource_dir, allow_root=False)
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="limit 必须在 1-100 之间")
    storage = get_media_storage()
    prefix = f"galleries/{resource_dir}/"
    images: list[dict] = []
    next_cursor = cursor
    has_more = True
    while len(images) < limit and has_more:
        page = storage.list(prefix, cursor=next_cursor, limit=limit - len(images))
        for item in page.objects:
            if _direct_child(item.key, prefix) is not None:
                images.append(_image_dict(item, resource_dir))
        next_cursor = page.next_cursor
        has_more = page.has_more
    images.sort(
        key=lambda item: [
            int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", item["name"])
        ]
    )
    return GalleryPage(images, next_cursor if has_more else None, has_more)


def _all_gallery_objects(resource_dir: str) -> list[MediaObject]:
    resource_dir = normalize_resource_path(resource_dir, allow_root=False)
    storage = get_media_storage()
    prefix = f"galleries/{resource_dir}/"
    cursor = None
    result: list[MediaObject] = []
    while True:
        page = storage.list(prefix, cursor=cursor, limit=100)
        result.extend(item for item in page.objects if _direct_child(item.key, prefix) is not None)
        if not page.has_more:
            break
        cursor = page.next_cursor
    result.sort(key=_natural_name_key)
    return result


def invalidate_summary(resource_dir: str) -> None:
    with _summary_lock:
        _summary_cache.pop(resource_dir.casefold(), None)


def gallery_summary(resource_dir: str) -> dict:
    resource_dir = normalize_resource_path(resource_dir, allow_root=False)
    cache_key = resource_dir.casefold()
    now = time.monotonic()
    with _summary_lock:
        cached = _summary_cache.get(cache_key)
        if cached and now - cached[0] < _SUMMARY_CACHE_TTL_SECONDS:
            return dict(cached[1])
    objects = _all_gallery_objects(resource_dir)
    first = _image_dict(objects[0], resource_dir) if objects else None
    summary = {"imageCount": len(objects), "cover": first}
    with _summary_lock:
        if len(_summary_cache) >= 256:
            oldest = min(_summary_cache, key=lambda key: _summary_cache[key][0])
            _summary_cache.pop(oldest, None)
        _summary_cache[cache_key] = (now, summary)
    return dict(summary)


def validate_gallery_directory(resource_dir: str, *, require_images: bool = False) -> str:
    relative = normalize_resource_path(resource_dir, allow_root=False)
    if require_images and gallery_summary(relative)["imageCount"] == 0:
        raise HTTPException(status_code=422, detail="发布图集前，资源文件夹中至少需要一张图片")
    if not folder_exists(relative):
        raise HTTPException(status_code=404, detail="资源目录不存在")
    return relative


def validate_image_file(path: Path, filename: str | None = None) -> None:
    try:
        with Image.open(path) as image:
            if filename:
                expected = {
                    ".jpg": "JPEG",
                    ".jpeg": "JPEG",
                    ".png": "PNG",
                    ".webp": "WEBP",
                    ".gif": "GIF",
                }.get(PurePosixPath(filename).suffix.casefold())
                if not expected or image.format != expected:
                    raise HTTPException(status_code=422, detail="图片内容与文件扩展名不一致")
            image.verify()
    except HTTPException:
        raise
    except (OSError, UnidentifiedImageError, ValueError, Image.DecompressionBombError) as exc:
        raise HTTPException(status_code=422, detail="文件内容不是可识别的图片") from exc


def create_thumbnail(source: Path, target: Path) -> None:
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened)
            if getattr(image, "is_animated", False):
                image.seek(0)
            image.thumbnail((settings.thumbnail_max_width, settings.thumbnail_max_height))
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGB")
            image.save(
                target,
                "WEBP",
                quality=settings.thumbnail_webp_quality,
                method=settings.thumbnail_webp_method,
            )
    except (OSError, UnidentifiedImageError, ValueError, Image.DecompressionBombError) as exc:
        raise HTTPException(status_code=422, detail="图片缩略图生成失败") from exc


def upload_prepared_image(
    relative_path: str, source: Path, thumbnail: Path, *, update_manifest: bool = True
) -> dict:
    relative = normalize_resource_path(relative_path, allow_root=False)
    extension = PurePosixPath(relative).suffix.casefold()
    if extension not in IMAGE_EXTENSIONS:
        raise HTTPException(status_code=422, detail="只允许上传 JPG、PNG、WebP 或 GIF 图片")
    original_key = gallery_key(relative)
    preview_key = thumbnail_key(relative)
    storage = get_media_storage()
    if storage.head(original_key) or storage.head(preview_key):
        raise HTTPException(status_code=409, detail="同名图片或缩略图已存在")
    uploaded: list[str] = []
    try:
        item = storage.put_file(original_key, source)
        uploaded.append(original_key)
        storage.put_file(preview_key, thumbnail, content_type="image/webp")
        uploaded.append(preview_key)
    except MediaStorageError as exc:
        for key in reversed(uploaded):
            storage.delete(key, missing_ok=True)
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    resource_dir = PurePosixPath(relative).parent.as_posix()
    if resource_dir == ".":
        resource_dir = ""
    invalidate_summary(resource_dir)
    result = _image_dict(item, resource_dir)
    if update_manifest and resource_dir:
        try:
            write_manifest(resource_dir)
        except Exception:
            storage.delete(preview_key, missing_ok=True)
            storage.delete(original_key, missing_ok=True)
            invalidate_summary(resource_dir)
            raise
    return result


def upload_prepared_batch(items: list[tuple[str, Path, Path]]) -> list[dict]:
    storage = get_media_storage()
    normalized: list[tuple[str, Path, Path]] = []
    all_keys: list[str] = []
    for relative_path, source, thumbnail in items:
        relative = normalize_resource_path(relative_path, allow_root=False)
        if PurePosixPath(relative).suffix.casefold() not in IMAGE_EXTENSIONS:
            raise HTTPException(status_code=422, detail=f"只允许上传图片：{relative}")
        normalized.append((relative, source, thumbnail))
        all_keys.extend((gallery_key(relative), thumbnail_key(relative)))
    if len({key.casefold() for key in all_keys}) != len(all_keys):
        raise HTTPException(status_code=422, detail="文件夹中包含重名图片")
    if any(storage.head(key) is not None for key in all_keys):
        raise HTTPException(status_code=409, detail="文件夹中包含已存在的对象")

    uploaded: list[str] = []
    results: list[dict] = []
    directories: set[str] = set()
    try:
        for relative, source, thumbnail in normalized:
            original_key = gallery_key(relative)
            preview_key = thumbnail_key(relative)
            item = storage.put_file(original_key, source)
            uploaded.append(original_key)
            storage.put_file(preview_key, thumbnail, content_type="image/webp")
            uploaded.append(preview_key)
            directory = PurePosixPath(relative).parent.as_posix()
            if directory == ".":
                directory = ""
            if directory:
                directories.add(directory)
            invalidate_summary(directory)
            results.append(_image_dict(item, directory))
        for directory in sorted(directories):
            write_manifest(directory)
    except MediaStorageError as exc:
        for key in reversed(uploaded):
            storage.delete(key, missing_ok=True)
        for directory in directories:
            invalidate_summary(directory)
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception:
        for key in reversed(uploaded):
            storage.delete(key, missing_ok=True)
        for directory in directories:
            invalidate_summary(directory)
        raise
    return results


def _lock_for_manifest(key: str) -> threading.Lock:
    with _manifest_locks_guard:
        return _manifest_locks.setdefault(key, threading.Lock())


def write_manifest(resource_dir: str) -> dict:
    resource_dir = normalize_resource_path(resource_dir, allow_root=False)
    storage = get_media_storage()
    objects = _all_gallery_objects(resource_dir)
    payload = {
        "formatVersion": 1,
        "resourceDir": resource_dir,
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "imageCount": len(objects),
        "images": [
            {
                "name": PurePosixPath(item.key).name,
                "size": item.size,
                "sha256": item.sha256,
                "etag": item.etag,
            }
            for item in objects
        ],
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    key = manifest_key(resource_dir)
    with _lock_for_manifest(key):
        # Worker 的冻结协议不支持覆盖或内部 GET，只能在应用侧串行替换。
        storage.delete(key, missing_ok=True)
        try:
            storage.put_bytes(key, data, content_type="application/json")
        except MediaStorageError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    invalidate_summary(resource_dir)
    return payload


def folder_exists(relative: str) -> bool:
    relative = normalize_resource_path(relative, allow_root=False)
    storage = get_media_storage()
    if storage.head(manifest_key(relative)):
        return True
    prefix = f"galleries/{relative}/"
    return bool(storage.list(prefix, limit=1).objects)


def create_folder(parent_path: str | None, name: str | None) -> dict:
    parent = normalize_resource_path(parent_path)
    folder_name = validate_entry_name(name, "文件夹名称")
    relative = f"{parent}/{folder_name}" if parent else folder_name
    if folder_exists(relative):
        raise HTTPException(status_code=409, detail="同名文件或文件夹已存在")
    write_manifest(relative)
    return {"name": folder_name, "path": relative, "url": folder_url(relative), "type": "folder", "size": None, "updatedAt": None}


def list_directory(relative: str | None) -> list[dict]:
    relative = normalize_resource_path(relative)
    storage = get_media_storage()
    gallery_prefix = f"galleries/{relative}/" if relative else "galleries/"
    folders: set[str] = set()
    files: dict[str, MediaObject] = {}
    cursor = None
    while True:
        page = storage.list(gallery_prefix, cursor=cursor, limit=100)
        for item in page.objects:
            child = item.key.removeprefix(gallery_prefix)
            if "/" in child:
                folders.add(child.split("/", 1)[0])
            elif child:
                files[child] = item
        if not page.has_more:
            break
        cursor = page.next_cursor

    manifest_prefix = "manifests/"
    cursor = None
    while True:
        page = storage.list(manifest_prefix, cursor=cursor, limit=100)
        for item in page.objects:
            manifest_path = item.key.removeprefix(manifest_prefix).removesuffix(".json")
            parent = PurePosixPath(manifest_path).parent.as_posix()
            if parent == ".":
                parent = ""
            if parent == relative:
                folders.add(PurePosixPath(manifest_path).name)
        if not page.has_more:
            break
        cursor = page.next_cursor

    items = [
        {
            "name": name,
            "path": f"{relative}/{name}" if relative else name,
            "url": folder_url(f"{relative}/{name}" if relative else name),
            "type": "folder",
            "size": None,
            "updatedAt": None,
        }
        for name in sorted(folders, key=str.casefold)
    ]
    for name, item in sorted(files.items(), key=lambda pair: pair[0].casefold()):
        path = f"{relative}/{name}" if relative else name
        items.append(
            {
                "name": name,
                "path": path,
                "url": storage.media_url(gallery_key(path)),
                "downloadUrl": f"/api/admin/files/download?path={path}",
                "type": "file",
                "size": item.size,
                "updatedAt": item.uploaded,
            }
        )
    return items


def delete_image(relative_path: str) -> None:
    relative = normalize_resource_path(relative_path, allow_root=False)
    storage = get_media_storage()
    original = gallery_key(relative)
    if storage.head(original) is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    try:
        storage.delete(original)
        storage.delete(thumbnail_key(relative), missing_ok=True)
    except MediaStorageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    directory = PurePosixPath(relative).parent.as_posix()
    if directory != ".":
        write_manifest(directory)


def protected_download_url(relative_path: str) -> str:
    relative = normalize_resource_path(relative_path, allow_root=False)
    storage = get_media_storage()
    key = gallery_key(relative)
    if storage.head(key) is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    return storage.download_url(key)
