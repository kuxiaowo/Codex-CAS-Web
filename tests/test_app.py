from __future__ import annotations

import importlib
import io
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image


def image_bytes(fmt: str = "JPEG", size: tuple[int, int] = (80, 160), color: str = "navy") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, fmt)
    return output.getvalue()


class AppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp_dir.name)
        os.environ["DATABASE_PATH"] = str(cls.root / "test.db")
        os.environ["AUTH_SECRET_KEY"] = "test-secret-key-that-is-longer-than-thirty-two-bytes"
        import app.config
        import app.database
        import app.auth
        import app.gallery_assets
        import app.main

        importlib.reload(app.config)
        importlib.reload(app.database)
        importlib.reload(app.auth)
        importlib.reload(app.gallery_assets)
        cls.resource_dir = cls.root / "resources"
        cls.resource_dir.mkdir()
        cls.assets_patch = patch.object(app.gallery_assets, "RESOURCE_DIR", cls.resource_dir)
        cls.main_resource_patch = patch.object(app.main, "RESOURCE_DIR", cls.resource_dir)
        cls.assets_patch.start()
        cls.main_resource_patch.start()
        from fastapi.testclient import TestClient

        cls.main = app.main
        cls.database = app.database
        cls.client = TestClient(app.main.app)
        cls.client.__enter__()
        from app.bootstrap_admin import create_initial_admin
        create_initial_admin("admin", "admin-pass-123", "管理员")
        response = cls.client.post(
            "/api/auth/login", json={"username": "admin", "password": "admin-pass-123"}
        )
        cls.admin_auth = {"Authorization": f"Bearer {response.json()['accessToken']}"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        cls.main_resource_patch.stop()
        cls.assets_patch.stop()
        cls.temp_dir.cleanup()

    def admin_headers(self) -> dict[str, str]:
        return self.admin_auth

    def category_id(self) -> int:
        return self.client.get("/api/admin/categories", headers=self.admin_headers()).json()["data"][0]["id"]

    def create_gallery(self, name: str, *, status: str = "published") -> dict:
        directory = self.resource_dir / name
        directory.mkdir(exist_ok=True)
        Image.new("RGB", (600, 1200), "white").save(directory / "10.jpg")
        Image.new("RGB", (600, 1200), "gray").save(directory / "2.jpg")
        response = self.client.post(
            "/api/admin/galleries",
            headers=self.admin_headers(),
            json={
                "categoryId": self.category_id(),
                "title": f"图集 {name}",
                "resourceDir": name,
                "status": status,
                "isFeatured": True,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertTrue((directory / ".thumbs" / "2.jpg.webp").is_file())
        self.assertTrue((directory / ".thumbs" / "10.jpg.webp").is_file())
        return response.json()["data"]

    def test_home_empty_and_health(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("图集", response.text)
        self.assertEqual(self.client.get("/api/health").json(), {"status": "ok"})

    def test_admin_page_contains_initialized_controls_and_versioned_script(self) -> None:
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        for control in (
            "data-create-gallery", "data-files-up", "data-upload-button",
            "data-picker-current", "data-settings-form", "data-export",
        ):
            self.assertIn(control, response.text)
        self.assertIn("/static/js/admin.js?v=gallery-v2.2", response.text)
        self.assertGreaterEqual(response.text.count('type="button" value="cancel"'), 2)
        self.assertGreaterEqual(response.text.count("data-dialog-close"), 2)

    def test_gallery_crud_search_detail_and_natural_sort(self) -> None:
        gallery = self.create_gallery("scan-pages")
        listed = self.client.get("/?q=scan-pages")
        self.assertEqual(listed.status_code, 200)
        self.assertIn("图集 scan-pages", listed.text)
        detail = self.client.get(f"/galleries/{gallery['id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertLess(detail.text.index("2.jpg"), detail.text.index("10.jpg"))
        image_sources = re.findall(
            r'<img src="([^"]+)" data-original-src="([^"]+)"', detail.text
        )
        self.assertEqual(len(image_sources), 2)
        self.assertTrue(all("/.thumbs/" in preview for preview, _ in image_sources))
        self.assertTrue(all("/.thumbs/" not in original for _, original in image_sources))
        admin = self.client.get("/api/admin/galleries", headers=self.admin_headers()).json()["data"]
        item = next(value for value in admin if value["id"] == gallery["id"])
        self.assertEqual(item["imageCount"], 2)
        self.assertGreaterEqual(item["views"], 1)

    def test_publish_requires_valid_image_but_draft_allows_empty_folder(self) -> None:
        (self.resource_dir / "empty").mkdir()
        payload = {
            "categoryId": self.category_id(), "title": "空目录", "resourceDir": "empty",
            "status": "published", "isFeatured": False,
        }
        rejected = self.client.post("/api/admin/galleries", headers=self.admin_headers(), json=payload)
        self.assertEqual(rejected.status_code, 422)
        payload["status"] = "draft"
        accepted = self.client.post("/api/admin/galleries", headers=self.admin_headers(), json=payload)
        self.assertEqual(accepted.status_code, 201, accepted.text)

    def test_comments_use_gallery_relation(self) -> None:
        gallery = self.create_gallery("comments")
        registered = self.client.post(
            "/api/auth/register",
            json={
                "username": "reader", "displayName": "读者", "password": "reader-pass-123",
                "confirmPassword": "reader-pass-123",
            },
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        headers = {"Authorization": f"Bearer {registered.json()['accessToken']}"}
        posted = self.client.post(
            f"/api/galleries/{gallery['id']}/comments",
            headers=headers,
            json={"content": "这份图集很清楚。"},
        )
        self.assertEqual(posted.status_code, 201, posted.text)
        comments = self.client.get(f"/api/galleries/{gallery['id']}/comments").json()["data"]
        self.assertEqual(comments[0]["content"], "这份图集很清楚。")

    def test_file_tree_upload_and_folder_upload(self) -> None:
        headers = self.admin_headers()
        folder = self.client.post(
            "/api/admin/files/folders", headers=headers,
            json={"parentPath": "", "name": "资料"},
        )
        self.assertEqual(folder.status_code, 201, folder.text)
        uploaded = self.client.post(
            "/api/admin/uploads", headers=headers, data={"targetPath": "资料"},
            files={"file": ("001.jpg", image_bytes(), "image/jpeg")},
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        self.assertTrue((self.resource_dir / "资料" / ".thumbs" / "001.jpg.webp").is_file())
        tree = self.client.get("/api/admin/files/tree", headers=headers, params={"path": "资料"})
        self.assertEqual(tree.json()["data"][0]["name"], "001.jpg")

        batch = self.client.post(
            "/api/admin/files/folder-upload", headers=headers,
            data={"targetPath": "", "relativePaths": ["批量/1.png", "批量/子目录/2.png"]},
            files=[
                ("files", ("1.png", image_bytes("PNG"), "image/png")),
                ("files", ("2.png", image_bytes("PNG", color="red"), "image/png")),
            ],
        )
        self.assertEqual(batch.status_code, 201, batch.text)
        self.assertTrue((self.resource_dir / "批量" / "子目录" / "2.png").is_file())
        self.assertTrue((self.resource_dir / "批量" / ".thumbs" / "1.png.webp").is_file())
        self.assertTrue((self.resource_dir / "批量" / "子目录" / ".thumbs" / "2.png.webp").is_file())

    def test_file_security_and_invalid_upload(self) -> None:
        headers = self.admin_headers()
        for path in ("../outside", "C:/Windows", "/absolute"):
            with self.subTest(path=path):
                response = self.client.get("/api/admin/files/tree", headers=headers, params={"path": path})
                self.assertEqual(response.status_code, 422)
        invalid = self.client.post(
            "/api/admin/uploads", headers=headers, data={"targetPath": ""},
            files={"file": ("fake.jpg", b"not-an-image", "image/jpeg")},
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertFalse((self.resource_dir / "fake.jpg").exists())

        reserved = self.client.post(
            "/api/admin/files/folders", headers=headers,
            json={"parentPath": "", "name": "CON"},
        )
        self.assertEqual(reserved.status_code, 422)

    def test_folder_upload_rolls_back_on_invalid_image(self) -> None:
        response = self.client.post(
            "/api/admin/files/folder-upload", headers=self.admin_headers(),
            data={"targetPath": "", "relativePaths": ["回滚/1.jpg", "回滚/2.jpg"]},
            files=[
                ("files", ("1.jpg", image_bytes(), "image/jpeg")),
                ("files", ("2.jpg", b"broken", "image/jpeg")),
            ],
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse((self.resource_dir / "回滚").exists())

    def test_single_upload_enforces_size_limit_and_rolls_back(self) -> None:
        with patch.object(self.main, "settings", SimpleNamespace(upload_max_bytes=3)):
            response = self.client.post(
                "/api/admin/uploads", headers=self.admin_headers(), data={"targetPath": ""},
                files={"file": ("large.jpg", image_bytes(), "image/jpeg")},
            )
        self.assertEqual(response.status_code, 413)
        self.assertFalse((self.resource_dir / "large.jpg").exists())

    def test_resource_directory_is_unique(self) -> None:
        self.create_gallery("unique-dir")
        response = self.client.post(
            "/api/admin/galleries", headers=self.admin_headers(),
            json={
                "categoryId": self.category_id(), "title": "重复目录",
                "resourceDir": "UNIQUE-DIR", "status": "draft", "isFeatured": False,
            },
        )
        self.assertEqual(response.status_code, 409)

    def test_export_is_v2_and_contains_paths_not_files(self) -> None:
        gallery = self.create_gallery("export")
        response = self.client.get("/api/admin/export", headers=self.admin_headers())
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["formatVersion"], 2)
        exported = next(item for item in payload["galleries"] if item["id"] == gallery["id"])
        self.assertEqual(exported["resourceDir"], "export")
        self.assertNotIn("images", exported)
        self.assertEqual(
            self.client.post("/api/admin/import", headers=self.admin_headers(), json={"formatVersion": 1}).status_code,
            422,
        )

    def test_import_v2_maps_existing_category_and_adds_draft_gallery(self) -> None:
        directory = self.resource_dir / "import-empty"
        directory.mkdir()
        category = self.client.get("/api/admin/categories", headers=self.admin_headers()).json()["data"][0]
        payload = {
            "formatVersion": 2,
            "categories": [{**category, "id": 777}],
            "galleries": [{
                "categoryId": 777, "title": "导入图集", "resourceDir": "import-empty",
                "status": "draft", "isFeatured": False,
            }],
            "announcements": [],
        }
        response = self.client.post("/api/admin/import", headers=self.admin_headers(), json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["galleries"], 1)


if __name__ == "__main__":
    unittest.main()
