from __future__ import annotations

import importlib
import os
from pathlib import Path
import tempfile
import unittest


class AppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = str(Path(cls.temp_dir.name) / "test.db")
        os.environ["AUTH_SECRET_KEY"] = "test-secret-key-that-is-longer-than-thirty-two-bytes"
        import app.config
        import app.database
        import app.auth
        import app.main

        importlib.reload(app.config)
        importlib.reload(app.database)
        importlib.reload(app.auth)
        importlib.reload(app.main)
        from fastapi.testclient import TestClient

        cls.client = TestClient(app.main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        cls.temp_dir.cleanup()

    def test_home_and_health(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("CAS Notes", response.text)
        self.assertIn("学习笔记", response.text)
        self.assertEqual(self.client.get("/api/health").json(), {"status": "ok"})

    def test_search_and_note_detail(self) -> None:
        response = self.client.get("/?q=SQLite")
        self.assertEqual(response.status_code, 200)
        self.assertIn("SQLite WAL", response.text)
        detail = self.client.get("/notes/sqlite-wal-mode")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("为什么选择 WAL", detail.text)

    def test_register_login_comment_flow(self) -> None:
        registered = self.client.post(
            "/api/auth/register",
            json={
                "username": "reader",
                "displayName": "读者",
                "password": "reader-pass-123",
                "confirmPassword": "reader-pass-123",
            },
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        token = registered.json()["accessToken"]
        me = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["data"]["displayName"], "读者")

        from app.database import connect
        connection = connect()
        try:
            note_id = connection.execute(
                "SELECT id FROM notes WHERE slug = 'sqlite-wal-mode'"
            ).fetchone()[0]
        finally:
            connection.close()
        comment = self.client.post(
            f"/api/notes/{note_id}/comments",
            json={"content": "这条笔记很实用。"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(comment.status_code, 201, comment.text)
        comments = self.client.get(f"/api/notes/{note_id}/comments").json()["data"]
        self.assertEqual(comments[0]["content"], "这条笔记很实用。")

    def test_register_rejects_mismatched_passwords(self) -> None:
        response = self.client.post(
            "/api/auth/register",
            json={
                "username": "mismatch",
                "displayName": "密码不一致",
                "password": "reader-pass-123",
                "confirmPassword": "different-pass-123",
            },
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"], "两次输入的密码不一致")

    def test_admin_bootstrap_and_admin_api(self) -> None:
        from app.bootstrap_admin import create_initial_admin

        created = create_initial_admin("admin", "admin-pass-123", "管理员")
        self.assertEqual(created["username"], "admin")
        logged_in = self.client.post(
            "/api/auth/login", json={"username": "admin", "password": "admin-pass-123"}
        )
        self.assertEqual(logged_in.status_code, 200, logged_in.text)
        token = logged_in.json()["accessToken"]
        headers = {"Authorization": f"Bearer {token}"}
        dashboard = self.client.get("/api/admin/dashboard", headers=headers)
        self.assertEqual(dashboard.status_code, 200, dashboard.text)
        self.assertGreaterEqual(dashboard.json()["data"]["counts"]["notes"], 4)
        exported = self.client.get("/api/admin/export", headers=headers)
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(exported.json()["formatVersion"], 1)

    def test_admin_crud_uses_camel_case_json(self) -> None:
        logged_in = self.client.post(
            "/api/auth/login", json={"username": "admin", "password": "admin-pass-123"}
        )
        token = logged_in.json()["accessToken"]
        headers = {"Authorization": f"Bearer {token}"}
        category = self.client.post(
            "/api/admin/categories",
            headers=headers,
            json={
                "name": "工具箱",
                "slug": "toolbox",
                "description": "实用工具",
                "accent": "#55d8d2",
                "sortOrder": 20,
                "isActive": True,
            },
        )
        self.assertEqual(category.status_code, 201, category.text)
        category_id = category.json()["data"]["id"]
        note = self.client.post(
            "/api/admin/notes",
            headers=headers,
            json={
                "categoryId": category_id,
                "title": "测试笔记",
                "slug": "test-note",
                "summary": "测试摘要",
                "content": "# 测试",
                "coverStyle": "lime",
                "readingMinutes": 2,
                "status": "published",
                "isFeatured": False,
            },
        )
        self.assertEqual(note.status_code, 201, note.text)
        note_id = note.json()["data"]["id"]
        listed = self.client.get("/api/admin/notes", headers=headers).json()["data"]
        self.assertTrue(any(item["id"] == note_id and item["coverStyle"] == "lime" for item in listed))
        deleted_note = self.client.delete(f"/api/admin/notes/{note_id}", headers=headers)
        self.assertEqual(deleted_note.status_code, 204)
        deleted_category = self.client.delete(f"/api/admin/categories/{category_id}", headers=headers)
        self.assertEqual(deleted_category.status_code, 204)


if __name__ == "__main__":
    unittest.main()
