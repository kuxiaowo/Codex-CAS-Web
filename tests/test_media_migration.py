from __future__ import annotations

import importlib.util
import os
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest


os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("MEDIA_STORAGE_BACKEND", "local")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_media_to_r2.py"
SPEC = importlib.util.spec_from_file_location("codex_media_migration", SCRIPT)
assert SPEC and SPEC.loader
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class SafeSourceScanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "resources"
        (self.root / "album").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_symlink(self, link: Path, target: Path, *, directory: bool = False) -> None:
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"当前平台不允许创建测试符号链接：{exc}")

    def test_rejects_file_symlink_instead_of_silently_skipping_it(self) -> None:
        outside = self.root.parent / "secret.jpg"
        outside.write_bytes(b"outside")
        self.make_symlink(self.root / "album" / "leak.jpg", outside)

        with self.assertRaisesRegex(RuntimeError, "符号链接"):
            migration.source_images(self.root)

    def test_rejects_linked_directory_in_source_chain(self) -> None:
        outside = self.root.parent / "outside"
        outside.mkdir()
        (outside / "leak.jpg").write_bytes(b"outside")
        self.make_symlink(self.root / "album" / "linked", outside, directory=True)

        with self.assertRaisesRegex(RuntimeError, "符号链接"):
            migration.source_images(self.root)

    def test_rejects_source_root_that_is_itself_a_link(self) -> None:
        linked_root = self.root.parent / "linked-resources"
        self.make_symlink(linked_root, self.root, directory=True)

        with self.assertRaisesRegex(RuntimeError, "符号链接"):
            migration.resolve_safe_source(linked_root, linked_root)


class MigrationSettingsTest(unittest.TestCase):
    def valid_settings(self):
        return replace(
            migration.settings,
            app_environment="production",
            media_storage_backend="r2",
            media_gateway_url="https://codex-media.nethub.wiki",
            media_hmac_secret="s" * 32,
            oidc_client_id="",
            oidc_client_secret="",
        )

    def test_media_validation_does_not_require_oidc_settings(self) -> None:
        migration.validate_media_storage_settings(
            self.valid_settings(), require_r2=True
        )

    def test_media_validation_requires_r2_backend(self) -> None:
        configured = replace(self.valid_settings(), media_storage_backend="local")
        with self.assertRaisesRegex(RuntimeError, "MEDIA_STORAGE_BACKEND"):
            migration.validate_media_storage_settings(configured, require_r2=True)

    def test_media_validation_requires_https_gateway(self) -> None:
        configured = replace(self.valid_settings(), media_gateway_url="http://example.test")
        with self.assertRaisesRegex(RuntimeError, "MEDIA_GATEWAY_URL"):
            migration.validate_media_storage_settings(configured, require_r2=True)

    def test_media_validation_requires_hmac_secret(self) -> None:
        configured = replace(self.valid_settings(), media_hmac_secret="short")
        with self.assertRaisesRegex(RuntimeError, "MEDIA_HMAC_SECRET"):
            migration.validate_media_storage_settings(configured, require_r2=True)


if __name__ == "__main__":
    unittest.main()
