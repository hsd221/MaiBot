import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.plugin_system.base.component_types import ComponentType, PluginInfo
from src.plugin_system.core.plugin_manager import PluginManager

plugin_manager_module = importlib.import_module("src.plugin_system.core.plugin_manager")


def make_manager() -> PluginManager:
    manager = object.__new__(PluginManager)
    manager.plugin_directories = []
    manager.plugin_classes = {}
    manager.plugin_paths = {}
    manager.loaded_plugins = {}
    manager.failed_plugins = {}
    return manager


class PluginManagerDirectoryTest(unittest.TestCase):
    def test_add_plugin_directory_accepts_existing_unique_paths_and_rejects_duplicates_or_missing(self) -> None:
        manager = make_manager()

        with tempfile.TemporaryDirectory() as tmp_dir:
            self.assertTrue(manager.add_plugin_directory(tmp_dir))
            self.assertFalse(manager.add_plugin_directory(tmp_dir))
            self.assertEqual(manager.plugin_directories, [tmp_dir])

        self.assertFalse(manager.add_plugin_directory("/definitely/missing/plugin-dir"))

    def test_ensure_plugin_directories_creates_defaults_once(self) -> None:
        manager = make_manager()

        with (
            patch.object(plugin_manager_module.os.path, "exists", side_effect=[False, True]),
            patch.object(plugin_manager_module.os, "makedirs") as makedirs,
        ):
            manager._ensure_plugin_directories()

        self.assertEqual(manager.plugin_directories, ["src/plugins/built_in", "plugins"])
        makedirs.assert_called_once_with("src/plugins/built_in", exist_ok=True)

        with patch.object(plugin_manager_module.os.path, "exists", return_value=True):
            manager._ensure_plugin_directories()
        self.assertEqual(manager.plugin_directories, ["src/plugins/built_in", "plugins"])

    def test_load_plugin_modules_from_directory_scans_visible_plugin_packages_and_counts_failures(self) -> None:
        manager = make_manager()

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "ok_plugin").mkdir()
            (root / "ok_plugin" / "plugin.py").write_text("# ok", encoding="utf-8")
            (root / "bad_plugin").mkdir()
            (root / "bad_plugin" / "plugin.py").write_text("# bad", encoding="utf-8")
            (root / ".hidden").mkdir()
            (root / ".hidden" / "plugin.py").write_text("# hidden", encoding="utf-8")
            (root / "no_plugin_file").mkdir()

            with patch.object(
                manager,
                "_load_plugin_module_file",
                side_effect=lambda path: path.endswith("ok_plugin/plugin.py"),
            ) as load_file:
                self.assertEqual(manager._load_plugin_modules_from_directory(tmp_dir), (1, 1))

        self.assertEqual(load_file.call_count, 2)
        self.assertFalse(manager._load_plugin_modules_from_directory("/definitely/missing/plugins")[0])

    @unittest.skipUnless(hasattr(os, "symlink"), "platform does not support symlinks")
    def test_plugin_discovery_does_not_follow_symlinked_directories_or_module_files(self) -> None:
        manager = make_manager()

        with tempfile.TemporaryDirectory() as tmp_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(tmp_dir)
            outside = Path(outside_dir)

            good_plugin = root / "good_plugin"
            good_plugin.mkdir()
            (good_plugin / "plugin.py").write_text("# safe", encoding="utf-8")

            outside_plugin = outside / "outside_plugin"
            outside_plugin.mkdir()
            (outside_plugin / "plugin.py").write_text("# outside", encoding="utf-8")
            (root / "linked_directory").symlink_to(outside_plugin, target_is_directory=True)

            linked_file_plugin = root / "linked_file_plugin"
            linked_file_plugin.mkdir()
            (linked_file_plugin / "plugin.py").symlink_to(outside_plugin / "plugin.py")

            with patch.object(manager, "_load_plugin_module_file", return_value=True) as load_file:
                self.assertEqual(manager._load_plugin_modules_from_directory(tmp_dir), (1, 0))

        load_file.assert_called_once_with(str(good_plugin / "plugin.py"))

    def test_load_plugin_module_file_uses_importlib_spec_and_records_loader_failures(self) -> None:
        manager = make_manager()
        fake_loader = SimpleNamespace(exec_module=Mock())
        fake_spec = SimpleNamespace(loader=fake_loader)
        fake_module = SimpleNamespace()

        with (
            patch.object(manager, "_preflight_plugin_module"),
            patch.object(plugin_manager_module, "spec_from_file_location", return_value=fake_spec) as spec_from_file,
            patch.object(plugin_manager_module, "module_from_spec", return_value=fake_module) as module_from_spec,
        ):
            self.assertTrue(manager._load_plugin_module_file("plugins/demo/plugin.py"))

        spec_from_file.assert_called_once_with("plugins.demo", "plugins/demo/plugin.py")
        module_from_spec.assert_called_once_with(fake_spec)
        fake_loader.exec_module.assert_called_once_with(fake_module)
        self.assertEqual(fake_module.__package__, "plugins.demo")

        failing_loader = SimpleNamespace(exec_module=Mock(side_effect=RuntimeError("boom")))
        with (
            patch.object(manager, "_preflight_plugin_module"),
            patch.object(
                plugin_manager_module, "spec_from_file_location", return_value=SimpleNamespace(loader=failing_loader)
            ),
            patch.object(plugin_manager_module, "module_from_spec", return_value=SimpleNamespace()),
        ):
            self.assertFalse(manager._load_plugin_module_file("plugins/broken/plugin.py"))

        self.assertIn("plugins.broken", manager.failed_plugins)

    def test_load_plugin_module_file_rejects_missing_specs_or_loaders(self) -> None:
        manager = make_manager()

        with (
            patch.object(manager, "_preflight_plugin_module"),
            patch.object(plugin_manager_module, "spec_from_file_location", return_value=None),
        ):
            self.assertFalse(manager._load_plugin_module_file("plugins/missing_spec/plugin.py"))

        with (
            patch.object(manager, "_preflight_plugin_module"),
            patch.object(
                plugin_manager_module,
                "spec_from_file_location",
                return_value=SimpleNamespace(loader=None),
            ),
        ):
            self.assertFalse(manager._load_plugin_module_file("plugins/missing_loader/plugin.py"))

    def test_load_plugin_module_file_rejects_missing_manifest_before_import(self) -> None:
        manager = make_manager()

        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir) / "untrusted"
            plugin_dir.mkdir()
            plugin_file = plugin_dir / "plugin.py"
            plugin_file.write_text("raise RuntimeError('must not execute')", encoding="utf-8")

            fake_loader = SimpleNamespace(exec_module=Mock())
            with patch.object(
                plugin_manager_module,
                "spec_from_file_location",
                return_value=SimpleNamespace(loader=fake_loader),
            ) as spec_from_file:
                self.assertFalse(manager._load_plugin_module_file(str(plugin_file)))

        spec_from_file.assert_not_called()
        fake_loader.exec_module.assert_not_called()

    def test_preflight_plugin_module_accepts_regular_plugin_with_valid_manifest(self) -> None:
        manager = make_manager()

        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir) / "valid_plugin"
            plugin_dir.mkdir()
            plugin_file = plugin_dir / "plugin.py"
            plugin_file.write_text("# safe plugin entry", encoding="utf-8")
            (plugin_dir / "_manifest.json").write_text(
                json.dumps(
                    {
                        "manifest_version": 1,
                        "name": "valid_plugin",
                        "version": "1.0.0",
                        "description": "A valid test plugin",
                        "author": "MaiBot",
                    }
                ),
                encoding="utf-8",
            )

            manager._preflight_plugin_module(plugin_file)

    def test_preflight_plugin_module_rejects_duplicate_manifest_keys(self) -> None:
        manager = make_manager()

        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir) / "duplicate_manifest"
            plugin_dir.mkdir()
            plugin_file = plugin_dir / "plugin.py"
            plugin_file.write_text("# safe plugin entry", encoding="utf-8")
            (plugin_dir / "_manifest.json").write_text(
                '{"manifest_version": 2, "manifest_version": 2}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "插件清单格式无效"):
                manager._preflight_plugin_module(plugin_file)

    def test_plugin_import_failures_do_not_store_or_log_exception_text(self) -> None:
        manager = make_manager()
        secret_text = "api-key-super-secret"
        failing_loader = SimpleNamespace(exec_module=Mock(side_effect=RuntimeError(secret_text)))
        safe_logger = Mock()

        with (
            patch.object(manager, "_preflight_plugin_module"),
            patch.object(
                plugin_manager_module,
                "spec_from_file_location",
                return_value=SimpleNamespace(loader=failing_loader),
            ),
            patch.object(plugin_manager_module, "module_from_spec", return_value=SimpleNamespace()),
            patch.object(plugin_manager_module, "logger", safe_logger),
        ):
            self.assertFalse(manager._load_plugin_module_file("plugins/untrusted/plugin.py"))

        self.assertNotIn(secret_text, repr(manager.failed_plugins))
        self.assertNotIn(secret_text, repr(safe_logger.method_calls))
        safe_logger.exception.assert_not_called()


class PluginManagerLoadTest(unittest.IsolatedAsyncioTestCase):
    def test_version_compatibility_accepts_missing_requirements_and_reports_range_failures(self) -> None:
        manager = make_manager()

        self.assertEqual(manager._check_plugin_version_compatibility("plugin-a", {}), (True, ""))
        self.assertEqual(
            manager._check_plugin_version_compatibility("plugin-a", {"host_application": "bad"}), (True, "")
        )
        self.assertEqual(
            manager._check_plugin_version_compatibility("plugin-a", {"host_application": {}}),
            (True, ""),
        )

        with (
            patch.object(plugin_manager_module.VersionComparator, "get_current_host_version", return_value="1.2.0"),
            patch.object(
                plugin_manager_module.VersionComparator,
                "is_version_in_range",
                side_effect=[(True, ""), (False, "需要 >=2.0.0"), RuntimeError("bad version")],
            ),
        ):
            self.assertEqual(
                manager._check_plugin_version_compatibility(
                    "plugin-a", {"host_application": {"min_version": "1.0.0", "max_version": "2.0.0"}}
                ),
                (True, ""),
            )
            compatible, error = manager._check_plugin_version_compatibility(
                "plugin-a", {"host_application": {"min_version": "2.0.0"}}
            )
            self.assertFalse(compatible)
            self.assertIn("版本不兼容", error)
            compatible, error = manager._check_plugin_version_compatibility(
                "plugin-a", {"host_application": {"min_version": "bad"}}
            )
            self.assertFalse(compatible)
            self.assertIn("版本兼容性检查失败", error)

    def test_load_registered_plugin_classes_handles_missing_disabled_incompatible_success_and_failure(self) -> None:
        manager = make_manager()
        self.assertEqual(manager.load_registered_plugin_classes("missing"), (False, 1))

        manager.plugin_classes["no_path"] = Mock()
        self.assertEqual(manager.load_registered_plugin_classes("no_path"), (False, 1))

        class DisabledPlugin:
            def __init__(self, plugin_dir):
                self.plugin_dir = plugin_dir
                self.enable_plugin = False
                self.manifest_data = {}

        manager.plugin_classes["disabled"] = DisabledPlugin
        manager.plugin_paths["disabled"] = "/plugins/disabled"
        self.assertEqual(manager.load_registered_plugin_classes("disabled"), (False, 0))

        class IncompatiblePlugin:
            def __init__(self, plugin_dir):
                self.enable_plugin = True
                self.manifest_data = {"host_application": {"min_version": "99.0.0"}}

        manager.plugin_classes["incompatible"] = IncompatiblePlugin
        manager.plugin_paths["incompatible"] = "/plugins/incompatible"
        with patch.object(manager, "_check_plugin_version_compatibility", return_value=(False, "too new")):
            self.assertEqual(manager.load_registered_plugin_classes("incompatible"), (False, 1))
        self.assertEqual(manager.failed_plugins["incompatible"], "too new")

        class GoodPlugin:
            def __init__(self, plugin_dir):
                self.plugin_dir = plugin_dir
                self.enable_plugin = True
                self.manifest_data = {}

            def register_plugin(self):
                return True

        manager.plugin_classes["good"] = GoodPlugin
        manager.plugin_paths["good"] = "/plugins/good"
        with (
            patch.object(manager, "_check_plugin_version_compatibility", return_value=(True, "")),
            patch.object(manager, "_show_plugin_components") as show_components,
        ):
            self.assertEqual(manager.load_registered_plugin_classes("good"), (True, 1))

        self.assertIsInstance(manager.loaded_plugins["good"], GoodPlugin)
        show_components.assert_called_once_with("good")

        class RegisterFailurePlugin(GoodPlugin):
            def register_plugin(self):
                return False

        manager.plugin_classes["register_fail"] = RegisterFailurePlugin
        manager.plugin_paths["register_fail"] = "/plugins/register_fail"
        with patch.object(manager, "_check_plugin_version_compatibility", return_value=(True, "")):
            self.assertEqual(manager.load_registered_plugin_classes("register_fail"), (False, 1))
        self.assertEqual(manager.failed_plugins["register_fail"], "插件注册失败")

    def test_load_registered_plugin_classes_reports_instantiation_and_manifest_errors(self) -> None:
        manager = make_manager()

        class NonePlugin:
            def __new__(cls, plugin_dir):
                return None

        class MissingManifestPlugin:
            def __init__(self, plugin_dir):
                raise FileNotFoundError("missing manifest")

        class InvalidManifestPlugin:
            def __init__(self, plugin_dir):
                raise ValueError("bad manifest")

        class ExplodingPlugin:
            def __init__(self, plugin_dir):
                raise RuntimeError("boom")

        manager.plugin_classes.update(
            {
                "none": NonePlugin,
                "missing_manifest": MissingManifestPlugin,
                "invalid_manifest": InvalidManifestPlugin,
                "exploding": ExplodingPlugin,
            }
        )
        manager.plugin_paths.update(
            {
                "none": "/plugins/none",
                "missing_manifest": "/plugins/missing_manifest",
                "invalid_manifest": "/plugins/invalid_manifest",
                "exploding": "/plugins/exploding",
            }
        )

        self.assertEqual(manager.load_registered_plugin_classes("none"), (False, 1))
        self.assertEqual(manager.load_registered_plugin_classes("missing_manifest"), (False, 1))
        self.assertIn("缺少manifest文件", manager.failed_plugins["missing_manifest"])
        self.assertEqual(manager.load_registered_plugin_classes("invalid_manifest"), (False, 1))
        self.assertIn("manifest验证失败", manager.failed_plugins["invalid_manifest"])
        self.assertEqual(manager.load_registered_plugin_classes("exploding"), (False, 1))
        self.assertIn("未知错误", manager.failed_plugins["exploding"])

    async def test_remove_and_reload_registered_plugin_delegate_to_component_registry(self) -> None:
        manager = make_manager()
        component_a = SimpleNamespace(name="act", component_type=ComponentType.ACTION)
        component_b = SimpleNamespace(name="tool", component_type=ComponentType.TOOL)
        manager.loaded_plugins["plugin-a"] = SimpleNamespace(
            plugin_info=SimpleNamespace(components=[component_a, component_b])
        )
        fake_registry = SimpleNamespace(
            remove_component=AsyncMock(side_effect=[True, False]),
            remove_plugin_registry=Mock(return_value=True),
        )

        with patch.object(plugin_manager_module, "component_registry", fake_registry):
            self.assertFalse(await manager.remove_registered_plugin("plugin-a"))

        self.assertNotIn("plugin-a", manager.loaded_plugins)
        fake_registry.remove_component.assert_any_await("act", ComponentType.ACTION, "plugin-a")
        fake_registry.remove_component.assert_any_await("tool", ComponentType.TOOL, "plugin-a")
        fake_registry.remove_plugin_registry.assert_called_once_with("plugin-a")

        with self.assertRaisesRegex(ValueError, "插件名称不能为空"):
            await manager.remove_registered_plugin("")
        self.assertFalse(await manager.remove_registered_plugin("missing"))

        original_class = type("Original", (), {"__module__": "plugins.fixture"})
        manager.plugin_classes["plugin-a"] = original_class
        manager.plugin_paths["plugin-a"] = "plugins/fixture"

        def reload_module(*args, **kwargs):
            manager.plugin_classes["plugin-a"] = type("Updated", (), {"__module__": "plugins.fixture"})
            return True

        manager._load_plugin_module_file = Mock(side_effect=reload_module)
        manager.remove_registered_plugin = AsyncMock(side_effect=[False, True])
        manager.load_registered_plugin_classes = Mock(return_value=(True, 1))
        self.assertFalse(await manager.reload_registered_plugin("plugin-a"))
        self.assertTrue(await manager.reload_registered_plugin("plugin-a"))
        manager.load_registered_plugin_classes.assert_called_once_with("plugin-a")

        manager.remove_registered_plugin = AsyncMock(return_value=True)
        manager.load_registered_plugin_classes = Mock(return_value=(False, 1))
        self.assertFalse(await manager.reload_registered_plugin("plugin-a"))

    def test_load_all_and_rescan_aggregate_directory_and_registration_counts(self) -> None:
        manager = make_manager()
        manager.plugin_directories = ["dir-a", "dir-b"]
        manager.plugin_classes = {"good": Mock(), "bad": Mock()}
        manager._load_plugin_modules_from_directory = Mock(side_effect=[(1, 1), (2, 0)])
        manager.load_registered_plugin_classes = Mock(side_effect=[(True, 1), (False, 2)])
        manager._show_stats = Mock()

        self.assertEqual(manager.load_all_plugins(), (1, 2))
        manager._show_stats.assert_called_once_with(1, 2)

        manager._load_plugin_modules_from_directory = Mock(side_effect=[(3, 0)])
        with patch.object(plugin_manager_module.os.path, "exists", side_effect=[True, False]):
            self.assertEqual(manager.rescan_plugin_directory(), (3, 0))

        manager._load_plugin_modules_from_directory.assert_called_once_with("dir-a")

    def test_query_helpers_return_registered_loaded_and_path_state(self) -> None:
        manager = make_manager()
        plugin_instance = object()
        manager.loaded_plugins = {"loaded": plugin_instance}
        manager.plugin_classes = {"registered": Mock()}
        manager.plugin_paths = {"registered": "/plugins/registered"}

        self.assertIs(manager.get_plugin_instance("loaded"), plugin_instance)
        self.assertIsNone(manager.get_plugin_instance("missing"))
        self.assertEqual(manager.list_loaded_plugins(), ["loaded"])
        self.assertEqual(manager.list_registered_plugins(), ["registered"])
        self.assertEqual(manager.get_plugin_path("registered"), "/plugins/registered")
        self.assertIsNone(manager.get_plugin_path("missing"))

    def test_show_stats_reports_loaded_failed_and_empty_plugin_summaries(self) -> None:
        manager = make_manager()
        manager.loaded_plugins = {"plugin-a": object()}
        manager.plugin_paths = {"plugin-a": str(Path.cwd() / "plugins" / "plugin-a")}
        manager.plugin_directories = ["plugins"]
        manager.failed_plugins = {"plugin-b": "boom"}
        plugin_info = PluginInfo(
            display_name="Plugin A",
            name="plugin-a",
            description="desc",
            version="1.2.3",
            author="Mai",
            license="MIT",
            homepage_url="https://example.test",
            dependencies=["dep"],
            config_file="config.toml",
            components=[
                SimpleNamespace(component_type=ComponentType.ACTION),
                SimpleNamespace(component_type=ComponentType.ACTION),
                SimpleNamespace(component_type=ComponentType.TOOL),
            ],
        )
        fake_registry = SimpleNamespace(
            get_registry_stats=Mock(
                return_value={
                    "action_components": 2,
                    "command_components": 0,
                    "tool_components": 1,
                    "event_handlers": 0,
                    "total_components": 3,
                }
            ),
            get_plugin_info=Mock(return_value=plugin_info),
        )

        with (
            patch.object(plugin_manager_module, "component_registry", fake_registry),
            patch.object(plugin_manager_module.os.path, "exists", return_value=True),
        ):
            manager._show_stats(total_registered=1, total_failed_registration=1)
            manager._show_plugin_components("plugin-a")

        fake_registry.get_registry_stats.assert_called_once()
        fake_registry.get_plugin_info.assert_any_call("plugin-a")

        fake_registry.get_plugin_info = Mock(return_value=None)
        with patch.object(plugin_manager_module, "component_registry", fake_registry):
            manager._show_stats(total_registered=0, total_failed_registration=0)
            manager._show_plugin_components("missing")


if __name__ == "__main__":
    unittest.main()
