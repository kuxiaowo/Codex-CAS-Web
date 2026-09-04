from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from app import database, gallery_assets


class GalleryAssetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "resources"
        self.root.mkdir()
        self.root_patch = patch.object(gallery_assets, "RESOURCE_DIR", self.root)
        self.root_patch.start()

    def tearDown(self) -> None:
        self.root_patch.stop()
        self.temp_dir.cleanup()

    def test_scan_is_non_recursive_natural_and_uses_distinct_thumbnail_names(self) -> None:
        gallery = self.root / "pages"
        gallery.mkdir()
        Image.new("RGB", (800, 1600), "red").save(gallery / "10.jpg")
        Image.new("RGB", (800, 1600), "blue").save(gallery / "2.jpg")
        Image.new("RGB", (800, 1600), "green").save(gallery / "2.png")
        nested = gallery / "nested"
        nested.mkdir()
        Image.new("RGB", (80, 80), "black").save(nested / "1.jpg")
        (gallery / "broken.jpg").write_bytes(b"not an image")

        images = gallery_assets.scan_gallery("pages")
        self.assertEqual([item["name"] for item in images], ["2.jpg", "2.png", "10.jpg"])
        self.assertTrue((gallery / ".thumbs" / "2.jpg.webp").is_file())
        self.assertTrue((gallery / ".thumbs" / "2.png.webp").is_file())
        self.assertNotIn("nested/1.jpg", str(images))
        self.assertNotIn("broken.jpg", str(images))

    def test_scan_skips_symlink_that_resolves_outside_resource_root(self) -> None:
        gallery = self.root / "safe"
        gallery.mkdir()
        outside = Path(self.temp_dir.name) / "outside.jpg"
        Image.new("RGB", (80, 80), "red").save(outside)
        link = gallery / "linked.jpg"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("当前系统不允许创建测试符号链接")
        self.assertEqual(gallery_assets.scan_gallery("safe"), [])

    def test_thumbnail_preserves_ratio_and_rebuilds_after_source_change(self) -> None:
        gallery = self.root / "long"
        gallery.mkdir()
        source = gallery / "page.jpg"
        Image.new("RGB", (2000, 6000), "white").save(source)
        first_url = gallery_assets.ensure_thumbnail(source)
        thumb = gallery / ".thumbs" / "page.jpg.webp"
        with Image.open(thumb) as image:
            self.assertLessEqual(image.width, gallery_assets.settings.thumbnail_max_width)
            self.assertLessEqual(image.height, gallery_assets.settings.thumbnail_max_height)
            self.assertAlmostEqual(image.width / image.height, 1 / 3, places=2)
        previous_mtime = thumb.stat().st_mtime_ns

        Image.new("RGB", (1200, 1200), "black").save(source)
        source_time = max(source.stat().st_mtime_ns, previous_mtime + 10_000_000)
        os.utime(source, ns=(source_time, source_time))
        second_url = gallery_assets.ensure_thumbnail(source)
        with Image.open(thumb) as image:
            self.assertEqual(image.size, (640, 640))
        self.assertNotEqual(first_url, second_url)

    def test_thumbnail_applies_exif_orientation(self) -> None:
        gallery = self.root / "exif"
        gallery.mkdir()
        source = gallery / "rotated.jpg"
        image = Image.new("RGB", (40, 80), "white")
        exif = image.getexif()
        exif[274] = 6
        image.save(source, exif=exif)

        gallery_assets.ensure_thumbnail(source)
        with Image.open(gallery / ".thumbs" / "rotated.jpg.webp") as thumbnail:
            self.assertEqual(thumbnail.size, (80, 40))

    def test_full_sync_recurses_generates_updates_and_removes_orphans(self) -> None:
        gallery = self.root / "subject" / "lesson"
        gallery.mkdir(parents=True)
        source = gallery / "page.jpg"
        Image.new("RGB", (1200, 800), "white").save(source)
        thumb_dir = gallery / ".thumbs"
        thumb_dir.mkdir()
        Image.new("RGB", (10, 10), "black").save(thumb_dir / "deleted.jpg.webp", "WEBP")

        first = gallery_assets.sync_all_thumbnails()
        thumb = gallery_assets.thumbnail_path(source)
        self.assertEqual(first.scanned, 1)
        self.assertEqual(first.generated, 1)
        self.assertEqual(first.removed, 1)
        self.assertTrue(thumb.is_file())
        self.assertFalse((thumb_dir / "deleted.jpg.webp").exists())

        second = gallery_assets.sync_all_thumbnails()
        self.assertEqual(second.generated, 0)
        self.assertEqual(second.current, 1)

        previous_mtime = thumb.stat().st_mtime_ns
        Image.new("RGB", (800, 1200), "blue").save(source)
        source_time = max(source.stat().st_mtime_ns, previous_mtime + 10_000_000)
        os.utime(source, ns=(source_time, source_time))
        third = gallery_assets.sync_all_thumbnails()
        self.assertEqual(third.generated, 1)
        with Image.open(thumb) as image:
            self.assertEqual(image.size, (427, 640))


class DatabaseMigrationTest(unittest.TestCase):
    def test_v1_migration_preserves_non_content_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, display_name TEXT,
                  password_hash TEXT, role TEXT, is_active INTEGER, created_at TEXT);
                CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT UNIQUE, slug TEXT UNIQUE,
                  description TEXT, accent TEXT, sort_order INTEGER, is_active INTEGER, created_at TEXT);
                CREATE TABLE notes (id INTEGER PRIMARY KEY, category_id INTEGER, title TEXT);
                CREATE TABLE comments (id INTEGER PRIMARY KEY, note_id INTEGER, user_id INTEGER);
                CREATE TABLE announcements (id INTEGER PRIMARY KEY, title TEXT, content TEXT, status TEXT,
                  is_pinned INTEGER, created_at TEXT, updated_at TEXT);
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
                CREATE TABLE auth_attempts (id INTEGER PRIMARY KEY, action TEXT, subject TEXT, created_at TEXT);
                INSERT INTO users VALUES (1, 'admin', '管理员', 'hash', 'admin', 1, 'now');
                INSERT INTO categories VALUES (1, '旧栏目', 'legacy', '', '#ffffff', 10, 1, 'now');
                INSERT INTO notes VALUES (1, 1, '旧笔记');
                INSERT INTO comments VALUES (1, 1, 1);
                INSERT INTO announcements VALUES (1, '公告', '内容', 'published', 0, 'now', 'now');
                INSERT INTO settings VALUES ('site_name', '保留站名', 'now');
                PRAGMA user_version = 1;
                """
            )
            connection.commit()
            connection.close()

            with patch.object(database, "database_path", return_value=path):
                database.initialize_database()
                migrated = database.connect()
                try:
                    self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], 2)
                    self.assertEqual(migrated.execute("SELECT username FROM users").fetchone()[0], "admin")
                    self.assertEqual(migrated.execute("SELECT name FROM categories").fetchone()[0], "旧栏目")
                    self.assertEqual(migrated.execute("SELECT COUNT(*) FROM categories").fetchone()[0], 1)
                    self.assertEqual(migrated.execute("SELECT title FROM announcements").fetchone()[0], "公告")
                    self.assertEqual(migrated.execute("SELECT value FROM settings WHERE key='site_name'").fetchone()[0], "保留站名")
                    self.assertIsNone(migrated.execute("SELECT name FROM sqlite_master WHERE name='notes'").fetchone())
                    self.assertEqual(migrated.execute("SELECT COUNT(*) FROM galleries").fetchone()[0], 0)
                finally:
                    migrated.close()


if __name__ == "__main__":
    unittest.main()
