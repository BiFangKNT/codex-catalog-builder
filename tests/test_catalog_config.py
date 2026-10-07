import contextlib
import io
import tempfile
import tomllib
import unittest
from pathlib import Path, PureWindowsPath, PurePosixPath
from unittest.mock import patch

import codex_catalog_builder as builder


class CatalogConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / "config.toml"
        self.catalog = Path(self.directory.name) / "custom_catalog.json"
        for name, value in (("CONFIG_PATH", self.config), ("CATALOG_PATH", self.catalog)):
            patcher = patch.object(builder, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_adds_top_level_key_preserving_original_bytes(self):
        original = b'# comment\r\n[model_providers.proxy]\r\nmodel_catalog_json = "nested.json"'
        self.config.write_bytes(original)
        self.assertTrue(builder.ensure_model_catalog_config())
        updated = self.config.read_bytes()
        self.assertTrue(updated.endswith(original))
        self.assertIn(b"\r\n", updated.split(original)[0])
        parsed = tomllib.loads(updated.decode("utf-8"))
        self.assertEqual(parsed["model_catalog_json"], str(self.catalog))
        self.assertEqual(parsed["model_providers"]["proxy"]["model_catalog_json"], "nested.json")
        self.assertFalse(builder.ensure_model_catalog_config())
        self.assertEqual(self.config.read_bytes(), updated)

    def test_existing_top_level_key_is_unchanged(self):
        for value in ('"other.json"', '""'):
            with self.subTest(value=value):
                original = f"'model_catalog_json' = {value}\n# preserved\n".encode()
                self.config.write_bytes(original)
                self.assertFalse(builder.ensure_model_catalog_config())
                self.assertEqual(self.config.read_bytes(), original)

    def test_paths_round_trip_through_toml(self):
        paths = (
            PureWindowsPath(r"C:\Users\测试\.codex\custom_catalog.json"),
            PurePosixPath('/home/test/目录 "quoted"/custom_catalog.json'),
            PurePosixPath('/Users/test/.codex/custom_catalog.json'),
        )
        for path in paths:
            with self.subTest(path=path), patch.object(builder, "CATALOG_PATH", path):
                self.config.write_text("# empty config\n", encoding="utf-8")
                builder.ensure_model_catalog_config()
                text = self.config.read_text(encoding="utf-8")
                self.assertEqual(tomllib.loads(text)["model_catalog_json"], str(path))
                if isinstance(path, PureWindowsPath):
                    self.assertIn(r"C:\\Users\\", text)
                else:
                    self.assertNotIn(r"\/", text)

    def test_invalid_toml_is_not_modified(self):
        original = b"invalid = ["
        self.config.write_bytes(original)
        with self.assertRaises(tomllib.TOMLDecodeError):
            builder.ensure_model_catalog_config()
        self.assertEqual(self.config.read_bytes(), original)

    def test_replace_failure_preserves_config_and_removes_temp(self):
        original = b"# preserved\n"
        self.config.write_bytes(original)
        with patch.object(builder.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                builder.ensure_model_catalog_config()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(list(self.config.parent.glob("*.tmp.*")), [])

    def make_app(self):
        app = builder.CodexCatalogApp.__new__(builder.CodexCatalogApp)
        app.builtin_models_map = {}
        app.items = []
        return app

    def test_apply_saves_catalog_before_updating_config(self):
        self.config.write_text("# config\n", encoding="utf-8")
        update = builder.ensure_model_catalog_config

        def check_order():
            self.assertTrue(self.catalog.exists())
            return update()

        with patch.object(builder, "ensure_model_catalog_config", side_effect=check_order):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as result:
                self.make_app().apply_and_save()
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(tomllib.loads(self.config.read_text())["model_catalog_json"], str(self.catalog))

    def test_catalog_save_failure_does_not_update_config(self):
        with patch.object(builder, "atomic_save_json", side_effect=OSError("save failed")):
            with patch.object(builder, "ensure_model_catalog_config") as update:
                with self.assertRaises(OSError):
                    self.make_app().apply_and_save()
                update.assert_not_called()

    def test_config_failure_reports_saved_catalog(self):
        self.config.write_bytes(b"invalid = [")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as result:
            self.make_app().apply_and_save()
        self.assertEqual(result.exception.code, 1)
        self.assertTrue(self.catalog.exists())
        self.assertIn("目录已保存", output.getvalue())


if __name__ == "__main__":
    unittest.main()
