"""R2 Worker HMAC v1 客户端与显式本地测试存储。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator
from urllib.parse import quote

import httpx

from app.config import PROJECT_ROOT, settings


EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
ALLOWED_PREFIXES = ("galleries/", "thumbnails/", "manifests/")
LOCAL_RESOURCE_DIR = PROJECT_ROOT / "resources"


class MediaStorageError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class MediaObject:
    key: str
    size: int
    etag: str | None = None
    uploaded: str | None = None
    sha256: str | None = None
    content_type: str | None = None


@dataclass(frozen=True)
class MediaPage:
    objects: list[MediaObject]
    next_cursor: str | None
    has_more: bool


def normalize_key(value: str) -> str:
    normalized = unicodedata.normalize("NFC", str(value or ""))
    if (
        not normalized
        or normalized.startswith("/")
        or normalized.endswith("/")
        or "\\" in normalized
        or "%" in normalized
    ):
        raise ValueError("媒体对象键不合法")
    path = PurePosixPath(normalized)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("媒体对象键不合法")
    if any(any(ord(char) < 32 or ord(char) == 127 for char in part) for part in path.parts):
        raise ValueError("媒体对象键不合法")
    key = path.as_posix()
    if len(key.encode("utf-8")) > 1024 or not key.startswith(ALLOWED_PREFIXES):
        raise ValueError("媒体对象键不合法")
    return key


def encode_key(key: str) -> str:
    return "/".join(quote(part, safe="-._~") for part in normalize_key(key).split("/"))


def _canonical_query(params: dict[str, str] | None) -> str:
    if not params:
        return ""
    pairs = sorted((str(key), str(value)) for key, value in params.items())
    return "&".join(
        f"{quote(key, safe='-._~')}={quote(value, safe='-._~')}" for key, value in pairs
    )


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def content_type_for_key(key: str) -> str:
    suffix = PurePosixPath(key).suffix.casefold()
    explicit = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".avif": "image/avif",
        ".json": "application/json",
    }
    return explicit.get(suffix) or mimetypes.guess_type(key)[0] or "application/octet-stream"


class R2WorkerStorage:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self.base_url = settings.media_gateway_url
        self.secret = settings.media_hmac_secret.encode("utf-8")
        self.timeout = settings.media_request_timeout_seconds
        self.client = client or httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    def _headers(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body_sha256: str = EMPTY_SHA256,
    ) -> tuple[str, dict[str, str]]:
        query = _canonical_query(params)
        target = f"{path}?{query}" if query else path
        timestamp = str(int(time.time()))
        canonical = f"v1\n{method.upper()}\n{target}\n{timestamp}\n{body_sha256}"
        signature = hmac.new(self.secret, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        return target, {
            "X-Media-Timestamp": timestamp,
            "X-Media-Content-SHA256": body_sha256,
            "X-Media-Signature": signature,
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: bytes | BinaryIO | None = None,
        body_sha256: str = EMPTY_SHA256,
        content_length: int | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        target, headers = self._headers(
            method, path, params=params, body_sha256=body_sha256
        )
        if content_length is not None:
            headers["Content-Length"] = str(content_length)
        if extra_headers:
            headers.update(extra_headers)
        try:
            response = self.client.request(
                method,
                target,
                headers=headers,
                content=body,
            )
        except httpx.HTTPError as exc:
            raise MediaStorageError(502, "gateway_unavailable", "媒体网关不可用") from exc
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            raise MediaStorageError(
                response.status_code,
                str(payload.get("error") or "gateway_error"),
                str(payload.get("message") or "媒体网关请求失败"),
            )
        return response

    def head(self, key: str) -> MediaObject | None:
        key = normalize_key(key)
        path = f"/internal/object/{encode_key(key)}"
        try:
            response = self._request("HEAD", path)
        except MediaStorageError as exc:
            if exc.status_code == 404:
                return None
            raise
        return MediaObject(
            key=key,
            size=int(response.headers.get("X-Media-Size", response.headers.get("Content-Length", "0"))),
            etag=response.headers.get("ETag"),
            uploaded=response.headers.get("X-Media-Uploaded"),
            sha256=response.headers.get("X-Media-SHA256"),
            content_type=response.headers.get("Content-Type"),
        )

    def list(self, prefix: str, *, cursor: str | None = None, limit: int = 50) -> MediaPage:
        if not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1-100 之间")
        params = {"prefix": prefix, "limit": str(limit)}
        if cursor:
            params["cursor"] = cursor
        response = self._request("GET", "/internal/list", params=params)
        payload = response.json()
        objects = [
            MediaObject(
                key=item["key"],
                size=int(item["size"]),
                etag=item.get("etag"),
                uploaded=item.get("uploaded"),
                sha256=(item.get("customMetadata") or {}).get("sha256"),
                content_type=(item.get("httpMetadata") or {}).get("contentType"),
            )
            for item in payload.get("objects", [])
        ]
        return MediaPage(objects, payload.get("nextCursor"), bool(payload.get("hasMore")))

    def delete(self, key: str, *, missing_ok: bool = False) -> None:
        path = f"/internal/object/{encode_key(key)}"
        try:
            self._request("DELETE", path)
        except MediaStorageError as exc:
            if missing_ok and exc.status_code == 404:
                return
            raise

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> MediaObject:
        key = normalize_key(key)
        digest = hashlib.sha256(data).hexdigest()
        response = self._request(
            "PUT",
            f"/internal/object/{encode_key(key)}",
            body=data,
            body_sha256=digest,
            content_length=len(data),
            extra_headers={"Content-Type": content_type or content_type_for_key(key)},
        )
        payload = response.json()
        return MediaObject(key, len(data), payload.get("etag"), sha256=digest)

    def put_file(self, key: str, path: Path, *, content_type: str | None = None) -> MediaObject:
        key = normalize_key(key)
        size = path.stat().st_size
        digest = sha256_file(path)
        media_type = content_type or content_type_for_key(key)
        if size <= settings.media_multipart_threshold_bytes:
            with path.open("rb") as source:
                response = self._request(
                    "PUT",
                    f"/internal/object/{encode_key(key)}",
                    body=source,
                    body_sha256=digest,
                    content_length=size,
                    extra_headers={"Content-Type": media_type},
                )
            payload = response.json()
            return MediaObject(key, size, payload.get("etag"), sha256=digest)
        return self._multipart_put(key, path, size=size, digest=digest, content_type=media_type)

    def _multipart_put(
        self, key: str, path: Path, *, size: int, digest: str, content_type: str
    ) -> MediaObject:
        created = self._request(
            "POST",
            f"/internal/multipart/{encode_key(key)}",
            extra_headers={"X-Media-Content-Type": content_type},
        ).json()
        upload_id = created["uploadId"]
        parts: list[dict[str, object]] = []
        try:
            with path.open("rb") as source:
                part_number = 1
                while chunk := source.read(settings.media_multipart_part_bytes):
                    part_hash = hashlib.sha256(chunk).hexdigest()
                    response = self._request(
                        "PUT",
                        f"/internal/multipart/{encode_key(key)}/part/{part_number}",
                        params={"uploadId": upload_id},
                        body=chunk,
                        body_sha256=part_hash,
                        content_length=len(chunk),
                    ).json()
                    parts.append({"partNumber": part_number, "etag": response["etag"]})
                    part_number += 1
            completion = json.dumps(
                {"parts": parts}, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            response = self._request(
                "POST",
                f"/internal/multipart/{encode_key(key)}",
                params={"uploadId": upload_id},
                body=completion,
                body_sha256=hashlib.sha256(completion).hexdigest(),
                content_length=len(completion),
                extra_headers={"Content-Type": "application/json"},
            ).json()
            return MediaObject(key, size, response.get("etag"), sha256=digest)
        except Exception:
            try:
                self._request(
                    "DELETE",
                    f"/internal/multipart/{encode_key(key)}",
                    params={"uploadId": upload_id},
                )
            except MediaStorageError:
                pass
            raise

    def media_url(self, key: str) -> str:
        return f"{self.base_url}/media/{encode_key(key)}"

    def download_url(self, key: str, *, method: str = "GET") -> str:
        key = normalize_key(key)
        path = f"/download/{encode_key(key)}"
        expires = int(time.time()) + settings.media_download_ttl_seconds
        canonical = f"v1\n{method.upper()}\n{path}\n{expires}\n{EMPTY_SHA256}"
        signature = hmac.new(self.secret, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{self.base_url}{path}?expires={expires}&sig={signature}"

    def verify_public_sha256(self, key: str, expected: str) -> bool:
        digest = hashlib.sha256()
        try:
            with self.client.stream("GET", self.media_url(key)) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    digest.update(chunk)
        except httpx.HTTPError as exc:
            raise MediaStorageError(502, "readback_failed", "媒体对象回读失败") from exc
        return hmac.compare_digest(digest.hexdigest(), expected)

    def close(self) -> None:
        self.client.close()


class LocalMediaStorage:
    """仅供显式 development/test 使用，布局兼容现有 resources 目录。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or LOCAL_RESOURCE_DIR

    def _path(self, key: str) -> Path:
        key = normalize_key(key)
        relative = key.split("/", 1)[1]
        if key.startswith("galleries/"):
            target = self.root / relative
        elif key.startswith("thumbnails/"):
            source = PurePosixPath(relative)
            target = self.root.joinpath(*source.parent.parts, ".thumbs", source.name)
        else:
            target = self.root / ".manifests" / relative
        root = self.root.resolve()
        resolved = target.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError("媒体对象键不合法")
        return target

    def _key_for_path(self, prefix: str, path: Path) -> str:
        if prefix.startswith("galleries/"):
            return f"galleries/{path.relative_to(self.root).as_posix()}"
        if prefix.startswith("thumbnails/"):
            relative = path.relative_to(self.root)
            parts = list(relative.parts)
            parts.remove(".thumbs")
            return f"thumbnails/{PurePosixPath(*parts).as_posix()}"
        return f"manifests/{path.relative_to(self.root / '.manifests').as_posix()}"

    def head(self, key: str) -> MediaObject | None:
        path = self._path(key)
        if not path.is_file():
            return None
        stat = path.stat()
        return MediaObject(
            normalize_key(key), stat.st_size, uploaded=str(stat.st_mtime),
            sha256=sha256_file(path), content_type=content_type_for_key(key)
        )

    def list(self, prefix: str, *, cursor: str | None = None, limit: int = 50) -> MediaPage:
        if not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1-100 之间")
        prefix = unicodedata.normalize("NFC", prefix)
        if not prefix.startswith(ALLOWED_PREFIXES) or not prefix.endswith("/"):
            raise ValueError("媒体前缀不合法")
        paths: Iterator[Path]
        if prefix.startswith("galleries/"):
            paths = (path for path in self.root.rglob("*") if path.is_file() and ".thumbs" not in path.parts and ".manifests" not in path.parts)
        elif prefix.startswith("thumbnails/"):
            paths = (path for path in self.root.rglob("*") if path.is_file() and ".thumbs" in path.parts)
        else:
            manifest_root = self.root / ".manifests"
            paths = (path for path in manifest_root.rglob("*") if path.is_file()) if manifest_root.exists() else iter(())
        objects = []
        for path in paths:
            key = self._key_for_path(prefix, path)
            if key.startswith(prefix):
                stat = path.stat()
                objects.append(MediaObject(key, stat.st_size, uploaded=str(stat.st_mtime), sha256=sha256_file(path), content_type=content_type_for_key(key)))
        objects.sort(key=lambda item: item.key.casefold())
        start = 0
        if cursor:
            try:
                start = int(base64.urlsafe_b64decode(cursor + "==").decode("ascii"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError("游标不合法") from exc
        page = objects[start : start + limit]
        next_index = start + len(page)
        has_more = next_index < len(objects)
        next_cursor = base64.urlsafe_b64encode(str(next_index).encode("ascii")).decode("ascii").rstrip("=") if has_more else None
        return MediaPage(page, next_cursor, has_more)

    def delete(self, key: str, *, missing_ok: bool = False) -> None:
        path = self._path(key)
        try:
            path.unlink()
        except FileNotFoundError:
            if not missing_ok:
                raise MediaStorageError(404, "not_found", "媒体对象不存在")

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> MediaObject:
        key = normalize_key(key)
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as output:
                output.write(data)
        except FileExistsError as exc:
            raise MediaStorageError(409, "object_exists", "同名媒体对象已存在") from exc
        return self.head(key)  # type: ignore[return-value]

    def put_file(self, key: str, path: Path, *, content_type: str | None = None) -> MediaObject:
        key = normalize_key(key)
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("rb") as source, target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
        except FileExistsError as exc:
            raise MediaStorageError(409, "object_exists", "同名媒体对象已存在") from exc
        return self.head(key)  # type: ignore[return-value]

    def media_url(self, key: str) -> str:
        key = normalize_key(key)
        if key.startswith("galleries/"):
            relative = key.removeprefix("galleries/")
        elif key.startswith("thumbnails/"):
            value = PurePosixPath(key.removeprefix("thumbnails/"))
            relative = PurePosixPath(value.parent, ".thumbs", value.name).as_posix()
        else:
            raise ValueError("manifest 没有公开地址")
        return f"/resources/{quote(relative, safe='/')}"

    def download_url(self, key: str, *, method: str = "GET") -> str:
        return self.media_url(key)

    def verify_public_sha256(self, key: str, expected: str) -> bool:
        return hmac.compare_digest(sha256_file(self._path(key)), expected)


_storage_lock = threading.Lock()
_storage_instance: R2WorkerStorage | LocalMediaStorage | None = None


def get_media_storage() -> R2WorkerStorage | LocalMediaStorage:
    global _storage_instance
    with _storage_lock:
        expected_type = LocalMediaStorage if settings.media_storage_backend == "local" else R2WorkerStorage
        if not isinstance(_storage_instance, expected_type):
            _storage_instance = expected_type()
        return _storage_instance


def reset_media_storage_for_tests() -> None:
    global _storage_instance
    with _storage_lock:
        if isinstance(_storage_instance, R2WorkerStorage):
            _storage_instance.close()
        _storage_instance = None


def close_media_storage() -> None:
    global _storage_instance
    with _storage_lock:
        if isinstance(_storage_instance, R2WorkerStorage):
            _storage_instance.close()
            _storage_instance = None
