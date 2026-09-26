from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MEDIA_STORAGE_BACKEND", "local")

from app import media_library, media_storage


def image_file(path: Path, color: str = "white") -> None:
    Image.new("RGB", (40, 80), color).save(path, "JPEG")


class R2WorkerClientTest(unittest.TestCase):
    def settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            media_gateway_url="https://codex-media.example.test",
            media_hmac_secret="s" * 32,
            media_request_timeout_seconds=10,
            media_download_ttl_seconds=90,
            media_multipart_threshold_bytes=20 * 1024 * 1024,
            media_multipart_part_bytes=8 * 1024 * 1024,
        )

    def test_list_uses_frozen_hmac_v1_canonical_query(self) -> None:
        captured = {}

        def request(method, url, **kwargs):
            captured.update(method=method, url=url, headers=kwargs["headers"])
            return httpx.Response(
                200,
                json={"objects": [], "nextCursor": None, "hasMore": False},
            )

        with patch.object(media_storage, "settings", self.settings()):
            client = media_storage.R2WorkerStorage()
            with patch.object(client.client, "request", side_effect=request):
                client.list("galleries/中文/", cursor="opaque+/=", limit=30)
            client.close()

        target = str(captured["url"]).removeprefix("https://codex-media.example.test")
        timestamp = captured["headers"]["X-Media-Timestamp"]
        body_hash = media_storage.EMPTY_SHA256
        expected = hmac.new(
            b"s" * 32,
            f"v1\nGET\n{target}\n{timestamp}\n{body_hash}".encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(captured["headers"]["X-Media-Signature"], expected)
        self.assertIn("cursor=opaque%2B%2F%3D", target)
        self.assertLess(target.index("cursor="), target.index("limit="))
        self.assertLess(target.index("limit="), target.index("prefix="))

    def test_download_signature_is_method_bound_and_short_lived(self) -> None:
        with patch.object(media_storage, "settings", self.settings()), patch.object(
            media_storage.time, "time", return_value=2_000_000_000
        ):
            client = media_storage.R2WorkerStorage()
            get_url = client.download_url("galleries/中文/a.jpg")
            head_url = client.download_url("galleries/中文/a.jpg", method="HEAD")
            client.close()
        self.assertNotEqual(get_url, head_url)
        self.assertIn("expires=2000000090", get_url)
        self.assertIn("/download/galleries/%E4%B8%AD%E6%96%87/a.jpg", get_url)

    def test_large_file_uses_serial_multipart_and_completes_with_all_etags(self) -> None:
        configured = self.settings()
        configured.media_multipart_threshold_bytes = 5
        configured.media_multipart_part_bytes = 5
        calls = []

        def request(method, url, **kwargs):
            body = kwargs.get("content")
            if hasattr(body, "read"):
                body = body.read()
            calls.append((method, url, kwargs["headers"], body))
            if method == "POST" and "uploadId=" not in url:
                return httpx.Response(201, json={"uploadId": "upload-1"})
            if method == "PUT":
                part_number = len([call for call in calls if call[0] == "PUT"])
                return httpx.Response(201, json={"partNumber": part_number, "etag": f"etag-{part_number}"})
            return httpx.Response(201, json={"etag": "complete", "completed": True})

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "large.jpg"
            source.write_bytes(b"0123456789x")
            with patch.object(media_storage, "settings", configured):
                client = media_storage.R2WorkerStorage()
                with patch.object(client.client, "request", side_effect=request):
                    client.put_file("galleries/album/large.jpg", source)
                client.close()

        parts = [call for call in calls if call[0] == "PUT"]
        self.assertEqual([len(call[3]) for call in parts], [5, 5, 1])
        for _, _, headers, body in parts:
            self.assertEqual(headers["Content-Length"], str(len(body)))
            self.assertEqual(headers["X-Media-Content-SHA256"], hashlib.sha256(body).hexdigest())
        completion = json.loads(calls[-1][3])
        self.assertEqual(
            completion["parts"],
            [
                {"partNumber": 1, "etag": "etag-1"},
                {"partNumber": 2, "etag": "etag-2"},
                {"partNumber": 3, "etag": "etag-3"},
            ],
        )


class LocalMediaStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage = media_storage.LocalMediaStorage(self.root)
        media_storage._storage_instance = self.storage

    def tearDown(self) -> None:
        media_storage.reset_media_storage_for_tests()
        self.temporary.cleanup()

    def test_gallery_cursor_pages_are_bounded_and_include_gateway_urls(self) -> None:
        source = self.root / "source.jpg"
        image_file(source)
        thumbnail = self.root / "thumb.webp"
        Image.new("RGB", (10, 10), "gray").save(thumbnail, "WEBP")
        for index in range(35):
            relative = f"album/{index:02d}.jpg"
            self.storage.put_file(f"galleries/{relative}", source)
            self.storage.put_file(f"thumbnails/{relative}.webp", thumbnail)

        first = media_library.gallery_images("album", limit=30)
        second = media_library.gallery_images("album", cursor=first.next_cursor, limit=30)
        self.assertEqual(len(first.images), 30)
        self.assertTrue(first.has_more)
        self.assertEqual(len(second.images), 5)
        self.assertFalse(second.has_more)
        self.assertTrue(first.images[0]["src"].startswith("/resources/"))

    def test_manifest_and_delete_keep_original_and_thumbnail_in_sync(self) -> None:
        source = self.root / "source.jpg"
        thumb = self.root / "thumb.webp"
        image_file(source)
        Image.new("RGB", (10, 10), "gray").save(thumb, "WEBP")
        media_library.create_folder("", "album")
        media_library.upload_prepared_image("album/a.jpg", source, thumb)

        manifest = self.root / ".manifests" / "album.json"
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["imageCount"], 1)
        media_library.delete_image("album/a.jpg")
        self.assertFalse((self.root / "album" / "a.jpg").exists())
        self.assertFalse((self.root / "album" / ".thumbs" / "a.jpg.webp").exists())
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["imageCount"], 0)

    def test_invalid_key_and_cursor_are_rejected(self) -> None:
        for key in ("galleries/../a.jpg", "galleries/a%2fb.jpg", "/galleries/a.jpg"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                media_storage.normalize_key(key)
        with self.assertRaises(ValueError):
            self.storage.list("galleries/", cursor="not-base64!", limit=30)

    def test_application_startup_has_no_thumbnail_directory_scan(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("sync_all_thumbnails", source)
        self.assertNotIn("thumbnail-sync", source)


if __name__ == "__main__":
    unittest.main()
