"""将本地 resources 安全迁移到 Codex 私有 R2（默认仅 dry-run）。"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings, validate_media_storage_settings  # noqa: E402
from app.gallery_assets import IMAGE_EXTENSIONS  # noqa: E402
from app.media_library import create_thumbnail, validate_image_file  # noqa: E402
from app.media_storage import (  # noqa: E402
    R2WorkerStorage,
    content_type_for_key,
    sha256_file,
)


_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & _WINDOWS_REPARSE_POINT)


def resolve_safe_source(path: Path, allowed_root: Path) -> Path:
    """Resolve an existing path without crossing symlinks or reparse points."""

    root = Path(os.path.abspath(os.fspath(allowed_root)))
    candidate = Path(os.path.abspath(os.fspath(path)))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"源路径超出允许目录：{path}") from exc

    current = root
    for part in (Path(), *relative.parts):
        if part != Path():
            current /= part
        if _is_link_or_reparse(current):
            raise RuntimeError(f"源路径包含符号链接、目录联接或重解析点：{current}")

    root_resolved = root.resolve(strict=True)
    resolved = candidate.resolve(strict=True)
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise RuntimeError(f"源路径解析后超出允许目录：{path}")
    return resolved


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=PROJECT_ROOT / "resources")
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=PROJECT_ROOT / "data" / "r2-media-migration-checkpoint.json",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="真正调用网关；省略时只输出计划，不读取或写入云端",
    )
    return parser.parse_args()


def source_images(root: Path) -> list[Path]:
    root = resolve_safe_source(root, root)
    images: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        for item in directory.iterdir():
            safe_item = resolve_safe_source(item, root)
            if safe_item.is_dir():
                pending.append(safe_item)
            elif (
                safe_item.is_file()
                and safe_item.suffix.casefold() in IMAGE_EXTENSIONS
                and ".thumbs" not in safe_item.parts
                and ".manifests" not in safe_item.parts
            ):
                images.append(safe_item)
    return sorted(
        images, key=lambda path: path.relative_to(root).as_posix().casefold()
    )


def load_checkpoint(path: Path) -> dict:
    if not path.exists():
        return {"formatVersion": 1, "objects": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("formatVersion") != 1 or not isinstance(payload.get("objects"), dict):
        raise RuntimeError("断点清单格式不受支持")
    return payload


def save_checkpoint(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def verify_or_conflict(storage: R2WorkerStorage, key: str, digest: str, size: int) -> str:
    existing = storage.head(key)
    if existing is None:
        return "missing"
    if existing.size != size:
        return "conflict"
    if existing.sha256:
        return "same" if existing.sha256 == digest else "conflict"
    if key.startswith(("galleries/", "thumbnails/")):
        return "same" if storage.verify_public_sha256(key, digest) else "conflict"
    return "conflict"


def upload_and_verify(
    storage: R2WorkerStorage,
    key: str,
    path: Path,
    digest: str,
) -> None:
    expected_size = path.stat().st_size
    storage.put_file(key, path, content_type=content_type_for_key(key))
    uploaded = storage.head(key)
    if uploaded is None or uploaded.size != expected_size:
        raise RuntimeError(f"上传后 HEAD 校验失败：{key}")
    if key.startswith(("galleries/", "thumbnails/")):
        if not storage.verify_public_sha256(key, digest):
            raise RuntimeError(f"上传后流式回读哈希不一致：{key}")
    elif uploaded.sha256 != digest:
        raise RuntimeError(f"manifest 上传后 SHA-256 元数据不一致：{key}")


def main() -> int:
    args = parse_args()
    root = resolve_safe_source(args.source, args.source)
    if not root.is_dir():
        raise RuntimeError(f"源目录不存在：{root}")
    images = source_images(root)
    print(f"模式：{'EXECUTE' if args.execute else 'DRY-RUN'}")
    print(f"源目录：{root}")
    print(f"发现图片：{len(images)}")
    if not args.execute:
        for source in images:
            relative = source.relative_to(root).as_posix()
            print(f"PLAN galleries/{relative} + thumbnails/{relative}.webp")
        print(f"将为 {len({path.parent for path in images})} 个目录生成 manifest")
        return 0

    validate_media_storage_settings(settings, require_r2=True)
    storage = R2WorkerStorage()
    checkpoint = load_checkpoint(args.checkpoint)
    completed = checkpoint["objects"]

    with tempfile.TemporaryDirectory(prefix="cas-r2-migration-") as temporary_dir:
        temporary_root = Path(temporary_dir)
        directory_entries: dict[str, list[dict]] = {}
        directory_mtimes: dict[str, float] = {}
        for index, source in enumerate(images):
            relative = source.relative_to(root).as_posix()
            original_key = f"galleries/{relative}"
            thumbnail_key = f"thumbnails/{relative}.webp"
            thumbnail = temporary_root / f"thumbnail-{index}.webp"
            validate_image_file(source, source.name)
            create_thumbnail(source, thumbnail)
            for key, local_path in ((original_key, source), (thumbnail_key, thumbnail)):
                digest = sha256_file(local_path)
                size = local_path.stat().st_size
                state = verify_or_conflict(storage, key, digest, size)
                if state == "conflict":
                    raise RuntimeError(f"冲突：{key} 已存在且内容不同；工具不会覆盖或删除")
                if state == "missing":
                    upload_and_verify(storage, key, local_path, digest)
                    state = "uploaded"
                completed[key] = {"sha256": digest, "size": size, "status": state}
                checkpoint["updatedAt"] = datetime.now(UTC).isoformat(timespec="seconds")
                save_checkpoint(args.checkpoint, checkpoint)
                print(f"{state.upper()} {key}")
            directory = source.relative_to(root).parent.as_posix()
            if directory != ".":
                directory_mtimes[directory] = max(
                    directory_mtimes.get(directory, 0), source.stat().st_mtime
                )
                directory_entries.setdefault(directory, []).append(
                    {
                        "name": source.name,
                        "size": source.stat().st_size,
                        "sha256": sha256_file(source),
                    }
                )

        for index, (directory, entries) in enumerate(sorted(directory_entries.items())):
            payload = {
                "formatVersion": 1,
                "resourceDir": directory,
                "generatedAt": datetime.fromtimestamp(
                    directory_mtimes[directory], UTC
                ).isoformat(timespec="seconds"),
                "imageCount": len(entries),
                "images": entries,
            }
            manifest = temporary_root / f"manifest-{index}.json"
            manifest.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            key = f"manifests/{directory}.json"
            digest = sha256_file(manifest)
            state = verify_or_conflict(storage, key, digest, manifest.stat().st_size)
            if state == "conflict":
                raise RuntimeError(f"冲突：{key} 已存在且内容不同；工具不会覆盖或删除")
            if state == "missing":
                upload_and_verify(storage, key, manifest, digest)
                state = "uploaded"
            completed[key] = {
                "sha256": digest,
                "size": manifest.stat().st_size,
                "status": state,
            }
            checkpoint["updatedAt"] = datetime.now(UTC).isoformat(timespec="seconds")
            save_checkpoint(args.checkpoint, checkpoint)
            print(f"{state.upper()} {key}")
    print("迁移与最终校验完成；未覆盖、未删除任何已有对象。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
