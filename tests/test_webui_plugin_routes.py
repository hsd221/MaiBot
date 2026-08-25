import json
import os
import shutil
import tempfile
import unittest
import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import tomlkit
from fastapi import HTTPException

from src.plugin_system.base.config_types import ConfigField
from src.plugin_system.core.installation_store import InstallationSource
from src.plugin_system.marketplace import PluginRegistry
from src.webui import plugin_routes
from src.webui.git_mirror_service import MAX_RAW_FILE_BYTES
from src.webui.plugin_market_service import (
    PluginRegistryFetchError,
    PluginRegistryFormatError,
    PluginRegistrySnapshot,
)

from tests.test_plugin_marketplace import manifest_v2, registry_document


class FakeTokenManager:
    def verify_token(self, token: str) -> bool:
        return token == "valid-token"


class FakeMirrorConfig:
    def __init__(self) -> None:
        self.mirrors = {
            "github": {
                "id": "github",
                "name": "GitHub",
                "raw_prefix": "https://raw.githubusercontent.com",
                "clone_prefix": "https://github.com",
                "enabled": True,
                "priority": 10,
            }
        }

    def get_all_mirrors(self) -> list[dict]:
        return list(self.mirrors.values())

    def get_default_priority_list(self) -> list[str]:
        return ["github", "mirror"]

    def add_mirror(
        self,
        mirror_id: str,
        name: str,
        raw_prefix: str,
        clone_prefix: str,
        enabled: bool,
        priority: int,
    ) -> dict:
        if mirror_id in self.mirrors:
            raise ValueError("镜像源已存在")
        self.mirrors[mirror_id] = {
            "id": mirror_id,
            "name": name,
            "raw_prefix": raw_prefix,
            "clone_prefix": clone_prefix,
            "enabled": enabled,
            "priority": priority,
        }
        return self.mirrors[mirror_id]

    def update_mirror(
        self,
        mirror_id: str,
        name: str | None = None,
        raw_prefix: str | None = None,
        clone_prefix: str | None = None,
        enabled: bool | None = None,
        priority: int | None = None,
    ) -> dict | None:
        mirror = self.mirrors.get(mirror_id)
        if not mirror:
            return None
        updates = {
            "name": name,
            "raw_prefix": raw_prefix,
            "clone_prefix": clone_prefix,
            "enabled": enabled,
            "priority": priority,
        }
        mirror.update({key: value for key, value in updates.items() if value is not None})
        return mirror

    def delete_mirror(self, mirror_id: str) -> bool:
        return self.mirrors.pop(mirror_id, None) is not None


class FakeGitMirrorService:
    def __init__(self) -> None:
        self.config = FakeMirrorConfig()

    def check_git_installed(self) -> dict:
        return {"installed": True, "version": "git version 2.0", "error": None}

    def get_mirror_config(self) -> FakeMirrorConfig:
        return self.config


class FakeInstallationStore:
    def __init__(self) -> None:
        self.sources: dict[str, InstallationSource] = {}
        self.fail_save = False
        self.fail_delete = False

    def get(self, plugin_id: str) -> InstallationSource | None:
        return self.sources.get(plugin_id)

    def list_all(self) -> dict[str, InstallationSource]:
        return dict(self.sources)

    def save(self, source: InstallationSource) -> InstallationSource:
        if self.fail_save:
            raise RuntimeError("database write failed with secret")
        existing = self.sources.get(source.plugin_id)
        if existing is not None:
            source = InstallationSource(
                plugin_id=source.plugin_id,
                install_method=source.install_method,
                registry_url=source.registry_url,
                repository_url=source.repository_url,
                source_ref=source.source_ref,
                source_commit=source.source_commit,
                artifact_sha256=source.artifact_sha256,
                installed_version=source.installed_version,
                installed_at=existing.installed_at,
                updated_at=source.updated_at,
                last_checked_at=source.last_checked_at,
            )
        self.sources[source.plugin_id] = source
        return source

    def delete(self, plugin_id: str) -> bool:
        if self.fail_delete:
            raise RuntimeError("database delete failed with secret")
        return self.sources.pop(plugin_id, None) is not None


def write_manifest(plugin_dir: Path, manifest: dict) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


class PluginRouteHelperTest(unittest.TestCase):
    def test_token_path_id_version_and_config_helpers_handle_expected_edges(self) -> None:
        self.assertEqual(
            plugin_routes.get_token_from_cookie_or_header("cookie-token", "Bearer header-token"), "cookie-token"
        )
        self.assertEqual(plugin_routes.get_token_from_cookie_or_header(None, "Bearer header-token"), "header-token")
        self.assertIsNone(plugin_routes.get_token_from_cookie_or_header(None, "Token header-token"))

        with tempfile.TemporaryDirectory() as tmp_dir:
            base_path = Path(tmp_dir)
            self.assertEqual(
                plugin_routes.validate_safe_path("nested/plugin", base_path), (base_path / "nested/plugin").resolve()
            )
            for unsafe_path in ["../escape", "/absolute", "C:\\absolute", "bad\x00path"]:
                with self.assertRaises(HTTPException):
                    plugin_routes.validate_safe_path(unsafe_path, base_path)

        self.assertEqual(plugin_routes.validate_plugin_id("作者.Plugin-1"), "作者.Plugin-1")
        for bad_id in [
            "",
            ".hidden",
            "trailing.",
            "bad/name",
            "bad\\name",
            "bad\nname",
            "bad\x1bname",
            "bad\u202ename",
            "bad..name",
            "a" * 129,
        ]:
            with self.assertRaises(HTTPException):
                plugin_routes.validate_plugin_id(bad_id)

        self.assertEqual(plugin_routes.parse_version("1.2.3.snapshot.4"), (1, 2, 3))
        self.assertEqual(plugin_routes.parse_version("1.2"), (1, 2, 0))
        self.assertEqual(plugin_routes.parse_version("bad.version"), (0, 0, 0))

        normalized = plugin_routes.normalize_dotted_keys(
            {
                "plugin.enabled": True,
                "plugin.tags": "a,b",
                "nested": {"value.key": 1},
                "conflict": "old",
                "conflict.child": "new",
            }
        )
        self.assertEqual(
            normalized,
            {
                "plugin": {"enabled": True, "tags": "a,b"},
                "nested": {"value": {"key": 1}},
                "conflict": {"child": "new"},
            },
        )

        schema = {"plugin": {"tags": ConfigField(type=list, default=[], description="标签")}}
        config = {"plugin": {"tags": "alpha, beta,, "}}
        plugin_routes.coerce_types(schema, config)
        self.assertEqual(config["plugin"]["tags"], ["alpha", "beta"])

    def test_plugin_directory_swaps_and_uninstalls_roll_back_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            plugins_dir = Path(tmp_dir) / "plugins"
            plugin_path = plugins_dir / "Author_Plugin"
            staged_path = plugins_dir / "staged"
            plugin_path.mkdir(parents=True)
            staged_path.mkdir()
            (plugin_path / "state.txt").write_text("old", encoding="utf-8")
            (staged_path / "state.txt").write_text("new", encoding="utf-8")
            plugin_identity = plugin_routes._directory_identity(plugin_path)
            staged_identity = plugin_routes._directory_identity(staged_path)
            real_replace = os.replace
            replace_calls = 0

            def fail_new_version_replace(source, destination):
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 2:
                    raise OSError("simulated swap failure")
                return real_replace(source, destination)

            with patch.object(plugin_routes.os, "replace", side_effect=fail_new_version_replace):
                with self.assertRaises(OSError):
                    plugin_routes._replace_plugin_directory(
                        plugin_path,
                        staged_path,
                        plugins_dir,
                        plugin_identity,
                        staged_identity,
                    )

            self.assertEqual((plugin_path / "state.txt").read_text(encoding="utf-8"), "old")
            self.assertEqual((staged_path / "state.txt").read_text(encoding="utf-8"), "new")

            (plugin_path / "config.toml").write_text("runtime", encoding="utf-8")
            backup_path, runtime_entries = plugin_routes._activate_plugin_replacement(
                plugin_path,
                staged_path,
                plugins_dir,
                plugin_routes._directory_identity(plugin_path),
                plugin_routes._directory_identity(staged_path),
            )
            with (
                patch.object(plugin_routes.os, "replace", side_effect=OSError("simulated restore failure")),
                self.assertRaises(OSError),
            ):
                plugin_routes._restore_plugin_replacement(
                    plugin_path,
                    backup_path,
                    plugins_dir,
                    runtime_entries,
                )

            self.assertEqual((plugin_path / "config.toml").read_text(encoding="utf-8"), "runtime")
            plugin_routes._restore_plugin_replacement(plugin_path, backup_path, plugins_dir, runtime_entries)

            with patch.object(plugin_routes, "_remove_plugin_tree", side_effect=PermissionError("denied")):
                with self.assertRaises(PermissionError):
                    plugin_routes._uninstall_plugin_directory(
                        plugin_path,
                        plugins_dir,
                        plugin_routes._directory_identity(plugin_path),
                    )

            self.assertEqual((plugin_path / "state.txt").read_text(encoding="utf-8"), "old")

    def test_plugin_replacement_recovers_runtime_state_after_a_partial_move_rollback_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            plugins_dir = Path(tmp_dir) / "plugins"
            plugin_path = plugins_dir / "Author_Plugin"
            staged_path = plugins_dir / "staged"
            plugin_path.mkdir(parents=True)
            staged_path.mkdir()
            (plugin_path / "state.txt").write_text("old", encoding="utf-8")
            (plugin_path / "config.toml").write_text("runtime", encoding="utf-8")
            (plugin_path / "data").mkdir()
            (plugin_path / "data" / "state.db").write_text("database", encoding="utf-8")
            (staged_path / "state.txt").write_text("new", encoding="utf-8")
            plugin_identity = plugin_routes._directory_identity(plugin_path)
            staged_identity = plugin_routes._directory_identity(staged_path)
            real_replace = os.replace
            failed_partial_rollback = False

            def fail_runtime_move_and_first_rollback(source, destination):
                nonlocal failed_partial_rollback
                source_path = Path(source)
                destination_path = Path(destination)
                if source_path.name == "data" and source_path.parent.name.startswith(".plugin-backup-"):
                    raise OSError("simulated runtime move failure")
                if (
                    source_path == plugin_path / "config.toml"
                    and destination_path.name == "config.toml"
                    and destination_path.parent.name.startswith(".plugin-backup-")
                    and not failed_partial_rollback
                ):
                    failed_partial_rollback = True
                    raise OSError("simulated partial rollback failure")
                return real_replace(source, destination)

            with (
                patch.object(plugin_routes.os, "replace", side_effect=fail_runtime_move_and_first_rollback),
                self.assertRaises(OSError),
            ):
                plugin_routes._activate_plugin_replacement(
                    plugin_path,
                    staged_path,
                    plugins_dir,
                    plugin_identity,
                    staged_identity,
                )

            self.assertTrue(failed_partial_rollback)
            self.assertEqual((plugin_path / "state.txt").read_text(encoding="utf-8"), "old")
            self.assertEqual((plugin_path / "config.toml").read_text(encoding="utf-8"), "runtime")
            self.assertEqual((plugin_path / "data" / "state.db").read_text(encoding="utf-8"), "database")
            self.assertFalse(staged_path.exists())


class PluginRouteBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.old_cwd)

        self.token_patcher = patch.object(plugin_routes, "get_token_manager", return_value=FakeTokenManager())
        self.token_patcher.start()
        self.addCleanup(self.token_patcher.stop)

        self.service = FakeGitMirrorService()
        self.service_patcher = patch.object(plugin_routes, "get_git_mirror_service", return_value=self.service)
        self.service_patcher.start()
        self.addCleanup(self.service_patcher.stop)

        self.installation_store = FakeInstallationStore()
        self.store_patcher = patch.object(
            plugin_routes,
            "PluginInstallationStore",
            return_value=self.installation_store,
        )
        self.store_patcher.start()
        self.addCleanup(self.store_patcher.stop)

    @property
    def plugins_dir(self) -> Path:
        return Path(self.tmp.name) / "plugins"

    def auth_kwargs(self) -> dict:
        return {"maibot_session": "valid-token", "authorization": None}


class PluginMirrorRoutesTest(PluginRouteBase):
    async def test_version_git_status_and_mirror_routes_use_service_and_auth(self) -> None:
        version = await plugin_routes.get_maimai_version()
        self.assertGreaterEqual(version.version_major, 0)

        with self.assertRaises(HTTPException) as git_status_auth_error:
            await plugin_routes.check_git_status(maibot_session=None, authorization=None)
        self.assertEqual(git_status_auth_error.exception.status_code, 401)

        git_status = await plugin_routes.check_git_status(**self.auth_kwargs())
        self.assertTrue(git_status.installed)
        self.assertEqual(git_status.version, "git version 2.0")

        with self.assertRaises(HTTPException) as auth_error:
            await plugin_routes.get_available_mirrors(maibot_session=None, authorization=None)
        self.assertEqual(auth_error.exception.status_code, 401)

        mirrors = await plugin_routes.get_available_mirrors(**self.auth_kwargs())
        self.assertEqual([mirror.id for mirror in mirrors.mirrors], ["github"])
        self.assertEqual(mirrors.default_priority, ["github", "mirror"])

        added = await plugin_routes.add_mirror(
            plugin_routes.AddMirrorRequest(
                id="mirror",
                name="Mirror",
                raw_prefix="https://mirror/raw",
                clone_prefix="https://mirror/clone",
                enabled=False,
                priority=5,
            ),
            **self.auth_kwargs(),
        )
        self.assertEqual((added.id, added.enabled, added.priority), ("mirror", False, 5))

        updated = await plugin_routes.update_mirror(
            "mirror",
            plugin_routes.UpdateMirrorRequest(name="Mirror Updated", enabled=True),
            **self.auth_kwargs(),
        )
        self.assertEqual((updated.name, updated.enabled, updated.priority), ("Mirror Updated", True, 5))

        deleted = await plugin_routes.delete_mirror("mirror", **self.auth_kwargs())
        self.assertTrue(deleted["success"])

        with self.assertRaises(HTTPException) as not_found:
            await plugin_routes.delete_mirror("missing", **self.auth_kwargs())
        self.assertEqual(not_found.exception.status_code, 404)

        with self.assertRaises(HTTPException) as duplicate:
            await plugin_routes.add_mirror(
                plugin_routes.AddMirrorRequest(
                    id="github",
                    name="Duplicate",
                    raw_prefix="https://dup/raw",
                    clone_prefix="https://dup/clone",
                ),
                **self.auth_kwargs(),
            )
        self.assertEqual(duplicate.exception.status_code, 400)

        with self.assertRaises(HTTPException) as unsafe_url:
            await plugin_routes.add_mirror(
                plugin_routes.AddMirrorRequest(
                    id="unsafe",
                    name="Unsafe",
                    raw_prefix="file:///etc/passwd",
                    clone_prefix="ext::sh -c id",
                ),
                **self.auth_kwargs(),
            )
        self.assertEqual(unsafe_url.exception.status_code, 400)

    async def test_fetch_raw_file_reports_progress_and_wraps_service_result(self) -> None:
        self.service.fetch_raw_file = AsyncMock(
            return_value={
                "success": True,
                "data": json.dumps([{"id": "a"}, {"id": "b"}]),
                "mirror_used": "github",
                "attempts": 1,
                "url": "https://raw.githubusercontent.com/MaiM-with-u/plugins/main/index.json",
            }
        )

        with patch.object(plugin_routes, "update_progress", new=AsyncMock()) as progress:
            result = await plugin_routes.fetch_raw_file(
                plugin_routes.FetchRawFileRequest(
                    owner="MaiM-with-u",
                    repo="plugins",
                    branch="main",
                    file_path="index.json",
                ),
                **self.auth_kwargs(),
            )

        self.assertTrue(result.success)
        self.assertEqual(result.mirror_used, "github")
        self.assertEqual(result.attempts, 1)
        self.service.fetch_raw_file.assert_awaited_once()
        self.assertEqual(progress.await_args_list[-1].kwargs["stage"], "success")
        self.assertEqual(progress.await_args_list[-1].kwargs["total_plugins"], 2)

    async def test_mirror_and_raw_failures_do_not_expose_internal_details(self) -> None:
        secret = 'token="super-secret" at /private/mirrors.json'
        mirror_cases = [
            (
                plugin_routes.add_mirror,
                (
                    plugin_routes.AddMirrorRequest(
                        id="mirror",
                        name="Mirror",
                        raw_prefix="https://mirror.example/raw",
                        clone_prefix="https://mirror.example/clone",
                    ),
                ),
                "添加镜像源失败",
            ),
            (
                plugin_routes.update_mirror,
                ("github", plugin_routes.UpdateMirrorRequest(name="GitHub Updated")),
                "更新镜像源失败",
            ),
        ]

        for endpoint, args, expected_detail in mirror_cases:
            with self.subTest(endpoint=endpoint.__name__):
                with (
                    patch.object(self.service, "get_mirror_config", side_effect=RuntimeError(secret)),
                    patch.object(plugin_routes.logger, "error") as logged,
                    self.assertRaises(HTTPException) as failure,
                ):
                    await endpoint(*args, **self.auth_kwargs())

                self.assertEqual(failure.exception.status_code, 500)
                self.assertEqual(failure.exception.detail, expected_detail)
                self.assertNotIn(secret, repr(logged.call_args))

        self.service.fetch_raw_file = AsyncMock(side_effect=RuntimeError(secret))
        with (
            patch.object(plugin_routes, "update_progress", new=AsyncMock()) as progress,
            patch.object(plugin_routes.logger, "error") as logged,
            self.assertRaises(HTTPException) as raw_failure,
        ):
            await plugin_routes.fetch_raw_file(
                plugin_routes.FetchRawFileRequest(
                    owner="MaiM-with-u",
                    repo="plugins",
                    branch="main",
                    file_path="index.json",
                ),
                **self.auth_kwargs(),
            )

        self.assertEqual(raw_failure.exception.status_code, 500)
        self.assertEqual(raw_failure.exception.detail, "获取 Raw 文件失败")
        self.assertEqual(progress.await_args_list[-1].kwargs["error"], "获取 Raw 文件失败")
        self.assertNotIn(secret, repr(progress.await_args_list))
        self.assertNotIn(secret, repr(logged.call_args))

    async def test_fetch_raw_file_rejects_oversized_or_excessive_plugin_data(self) -> None:
        cases = [
            ("oversized", "x" * (MAX_RAW_FILE_BYTES + 1), "Raw 文件过大"),
            ("too-many-plugins", json.dumps([{} for _ in range(10_001)]), "插件列表条目过多"),
        ]

        for name, data, expected_detail in cases:
            with self.subTest(case=name):
                self.service.fetch_raw_file = AsyncMock(
                    return_value={
                        "success": True,
                        "data": data,
                        "mirror_used": "github",
                        "attempts": 1,
                        "url": "https://raw.githubusercontent.com/MaiM-with-u/plugins/main/index.json",
                    }
                )

                with (
                    patch.object(plugin_routes, "update_progress", new=AsyncMock()) as progress,
                    self.assertRaises(HTTPException) as failure,
                ):
                    await plugin_routes.fetch_raw_file(
                        plugin_routes.FetchRawFileRequest(
                            owner="MaiM-with-u",
                            repo="plugins",
                            branch="main",
                            file_path="index.json",
                        ),
                        **self.auth_kwargs(),
                    )

                self.assertEqual(failure.exception.status_code, 413)
                self.assertEqual(failure.exception.detail, expected_detail)
                self.assertEqual(progress.await_args_list[-1].kwargs["stage"], "error")
                self.assertEqual(progress.await_args_list[-1].kwargs["error"], expected_detail)


class PluginMarketRoutesTest(PluginRouteBase):
    def _snapshot(self, *, versions: list[dict] | None = None) -> PluginRegistrySnapshot:
        return PluginRegistrySnapshot(
            registry_url="https://plugins.riyabot.example/registry.json",
            registry=PluginRegistry.model_validate(registry_document(versions=versions)),
            fetched_at=datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.timezone.utc),
        )

    async def test_market_catalog_is_authenticated_paginated_and_source_aware(self) -> None:
        write_manifest(self.plugins_dir / "github_alice_weather", manifest_v2())
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)
        source = InstallationSource(
            plugin_id="github.alice.weather",
            install_method="market",
            registry_url="https://plugins.riyabot.example/registry.json",
            repository_url="https://github.com/alice/riyabot-weather",
            source_ref="v1.2.3",
            source_commit="a" * 40,
            artifact_sha256=None,
            installed_version="1.2.3",
            installed_at=installed_at,
            updated_at=installed_at,
            last_checked_at=installed_at,
        )
        store = SimpleNamespace(list_all=lambda: {source.plugin_id: source})

        with (
            patch.object(plugin_routes, "fetch_plugin_registry", new=AsyncMock(return_value=self._snapshot())) as fetch,
            patch.object(plugin_routes, "PluginInstallationStore", return_value=store),
            patch.object(
                plugin_routes.global_config.webui,
                "plugin_registry_url",
                "https://plugins.riyabot.example/registry.json",
            ),
        ):
            result = await plugin_routes.get_plugin_market(
                page=1,
                page_size=20,
                query="weather",
                **self.auth_kwargs(),
            )

        self.assertEqual(result.registry.name, "RiyaBot Official Plugin Market")
        self.assertEqual(result.pagination.total, 1)
        self.assertEqual(len(result.plugins), 1)
        plugin = result.plugins[0]
        self.assertEqual(plugin.id, "github.alice.weather")
        self.assertEqual(plugin.latest_version, "1.2.3")
        self.assertEqual(plugin.installation.install_method, "market")
        self.assertEqual(plugin.installation.source_ref, "v1.2.3")
        self.assertEqual(plugin.keywords, ["weather"])
        self.assertEqual(plugin.host_application.min_version, "0.14.0")
        self.assertIsNone(plugin.host_application.max_version)
        self.assertEqual(plugin.versions[0].ref, "v1.2.3")
        self.assertEqual(plugin.versions[0].host_application.min_version, "0.14.0")
        fetch.assert_awaited_once_with(
            "https://plugins.riyabot.example/registry.json",
            cache_ttl_seconds=plugin_routes.PLUGIN_REGISTRY_CATALOG_CACHE_SECONDS,
        )

        serialized = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
        for registry_only_field in ('"manifest"', '"artifact_url"', '"sha256"'):
            self.assertNotIn(registry_only_field, serialized)

    async def test_market_catalog_returns_empty_page_for_unmatched_query(self) -> None:
        with (
            patch.object(plugin_routes, "fetch_plugin_registry", new=AsyncMock(return_value=self._snapshot())),
            patch.object(
                plugin_routes,
                "PluginInstallationStore",
                return_value=SimpleNamespace(list_all=lambda: {}),
            ),
            patch.object(
                plugin_routes.global_config.webui,
                "plugin_registry_url",
                "https://plugins.riyabot.example/registry.json",
            ),
        ):
            result = await plugin_routes.get_plugin_market(
                page=2,
                page_size=10,
                query="not-present",
                **self.auth_kwargs(),
            )

        self.assertEqual(result.plugins, [])
        self.assertEqual(result.pagination.total, 0)
        self.assertEqual(result.pagination.total_pages, 0)

    async def test_market_catalog_builds_full_version_projections_only_for_the_requested_page(self) -> None:
        document = registry_document()
        template = document["plugins"].pop("github.alice.weather")
        for index in range(3):
            plugin_id = f"github.alice.weather-{index}"
            plugin = json.loads(json.dumps(template))
            plugin["versions"][0]["manifest"]["id"] = plugin_id
            document["plugins"][plugin_id] = plugin
        snapshot = PluginRegistrySnapshot(
            registry_url="https://plugins.riyabot.example/registry.json",
            registry=PluginRegistry.model_validate(document),
            fetched_at=datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.timezone.utc),
        )

        with (
            patch.object(plugin_routes, "fetch_plugin_registry", new=AsyncMock(return_value=snapshot)),
            patch.object(plugin_routes, "_market_item_response", wraps=plugin_routes._market_item_response) as project,
        ):
            result = await plugin_routes.get_plugin_market(
                page=2,
                page_size=1,
                query=None,
                **self.auth_kwargs(),
            )

        self.assertEqual(result.pagination.total, 3)
        self.assertEqual([plugin.id for plugin in result.plugins], ["github.alice.weather-1"])
        self.assertEqual(project.call_count, 1)

    async def test_market_search_builds_full_version_projections_only_for_the_requested_page(self) -> None:
        document = registry_document()
        template = document["plugins"].pop("github.alice.weather")
        for index in range(3):
            plugin_id = f"github.alice.weather-{index}"
            plugin = json.loads(json.dumps(template))
            plugin["versions"][0]["manifest"]["id"] = plugin_id
            document["plugins"][plugin_id] = plugin
        snapshot = PluginRegistrySnapshot(
            registry_url="https://plugins.riyabot.example/registry.json",
            registry=PluginRegistry.model_validate(document),
            fetched_at=datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.timezone.utc),
        )

        with (
            patch.object(plugin_routes, "fetch_plugin_registry", new=AsyncMock(return_value=snapshot)),
            patch.object(plugin_routes, "_market_item_response", wraps=plugin_routes._market_item_response) as project,
        ):
            result = await plugin_routes.get_plugin_market(
                page=2,
                page_size=1,
                query="weather",
                **self.auth_kwargs(),
            )

        self.assertEqual(result.pagination.total, 3)
        self.assertEqual([plugin.id for plugin in result.plugins], ["github.alice.weather-1"])
        self.assertEqual(project.call_count, 1)

    async def test_market_catalog_keeps_blocked_market_installations_visible(self) -> None:
        document = registry_document()
        plugin_id = "github.alice.weather"
        document["plugins"][plugin_id]["status"] = "blocked"
        snapshot = PluginRegistrySnapshot(
            registry_url="https://plugins.riyabot.example/registry.json",
            registry=PluginRegistry.model_validate(document),
            fetched_at=datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.timezone.utc),
        )
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)
        source = InstallationSource(
            plugin_id=plugin_id,
            install_method="market",
            registry_url=snapshot.registry_url,
            repository_url="https://github.com/alice/riyabot-weather",
            source_ref="v1.2.3",
            source_commit="a" * 40,
            artifact_sha256=None,
            installed_version="1.2.3",
            installed_at=installed_at,
            updated_at=installed_at,
            last_checked_at=installed_at,
        )
        write_manifest(self.plugins_dir / "github_alice_weather", manifest_v2())

        with (
            patch.object(plugin_routes, "fetch_plugin_registry", new=AsyncMock(return_value=snapshot)),
            patch.object(
                plugin_routes,
                "PluginInstallationStore",
                return_value=SimpleNamespace(list_all=lambda: {plugin_id: source}),
            ),
        ):
            result = await plugin_routes.get_plugin_market(
                page=1,
                page_size=20,
                query=None,
                **self.auth_kwargs(),
            )

        self.assertEqual(len(result.plugins), 1)
        self.assertEqual(result.plugins[0].status, "blocked")
        self.assertIsNone(result.plugins[0].latest_version)
        self.assertFalse(result.plugins[0].versions[0].installable)

    async def test_market_catalog_hides_blocked_plugins_for_unbound_installations(self) -> None:
        document = registry_document()
        plugin_id = "github.alice.weather"
        document["plugins"][plugin_id]["status"] = "blocked"
        snapshot = PluginRegistrySnapshot(
            registry_url="https://plugins.riyabot.example/registry.json",
            registry=PluginRegistry.model_validate(document),
            fetched_at=datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.timezone.utc),
        )
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)

        for install_method, registry_url in (
            ("git", None),
            ("market", "https://other.example/registry.json"),
        ):
            with self.subTest(install_method=install_method, registry_url=registry_url):
                source = InstallationSource(
                    plugin_id=plugin_id,
                    install_method=install_method,
                    registry_url=registry_url,
                    repository_url="https://github.com/alice/riyabot-weather",
                    source_ref="v1.2.3",
                    source_commit="a" * 40,
                    artifact_sha256=None,
                    installed_version="1.2.3",
                    installed_at=installed_at,
                    updated_at=installed_at,
                    last_checked_at=installed_at,
                )
                installations = {plugin_id: source}
                with (
                    patch.object(plugin_routes, "fetch_plugin_registry", new=AsyncMock(return_value=snapshot)),
                    patch.object(
                        plugin_routes,
                        "PluginInstallationStore",
                        return_value=SimpleNamespace(list_all=lambda installations=installations: installations),
                    ),
                ):
                    result = await plugin_routes.get_plugin_market(
                        page=1,
                        page_size=20,
                        query=None,
                        **self.auth_kwargs(),
                    )

                self.assertEqual(result.plugins, [])

    async def test_market_catalog_ignores_missing_or_drifted_market_installations(self) -> None:
        plugin_id = "github.alice.weather"
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)
        self.installation_store.sources[plugin_id] = InstallationSource(
            plugin_id=plugin_id,
            install_method="market",
            registry_url="https://plugins.riyabot.example/registry.json",
            repository_url="https://github.com/alice/riyabot-weather",
            source_ref="v1.2.3",
            source_commit="a" * 40,
            artifact_sha256=None,
            installed_version="1.2.3",
            installed_at=installed_at,
            updated_at=installed_at,
            last_checked_at=installed_at,
        )
        approved_snapshot = self._snapshot()
        blocked_document = registry_document()
        blocked_document["plugins"][plugin_id]["status"] = "blocked"
        blocked_snapshot = PluginRegistrySnapshot(
            registry_url=approved_snapshot.registry_url,
            registry=PluginRegistry.model_validate(blocked_document),
            fetched_at=approved_snapshot.fetched_at,
        )

        cases = (
            ("missing", None),
            ("drifted", manifest_v2(version="9.9.9")),
        )
        for state, local_manifest in cases:
            with self.subTest(state=state):
                plugin_path = self.plugins_dir / "github_alice_weather"
                if local_manifest is not None:
                    write_manifest(plugin_path, local_manifest)

                with patch.object(
                    plugin_routes,
                    "fetch_plugin_registry",
                    new=AsyncMock(return_value=approved_snapshot),
                ):
                    approved = await plugin_routes.get_plugin_market(
                        page=1,
                        page_size=20,
                        query=None,
                        **self.auth_kwargs(),
                    )

                self.assertEqual(len(approved.plugins), 1)
                self.assertIsNone(approved.plugins[0].installation)

                with patch.object(
                    plugin_routes,
                    "fetch_plugin_registry",
                    new=AsyncMock(return_value=blocked_snapshot),
                ):
                    blocked = await plugin_routes.get_plugin_market(
                        page=1,
                        page_size=20,
                        query=None,
                        **self.auth_kwargs(),
                    )

                self.assertEqual(blocked.plugins, [])
                if plugin_path.exists():
                    shutil.rmtree(plugin_path)

    async def test_market_catalog_hides_source_when_registry_coordinates_drift(self) -> None:
        plugin_id = "github.alice.weather"
        plugin_path = self.plugins_dir / "github_alice_weather"
        write_manifest(plugin_path, manifest_v2())
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)
        self.installation_store.sources[plugin_id] = InstallationSource(
            plugin_id=plugin_id,
            install_method="market",
            registry_url=self._snapshot().registry_url,
            repository_url="https://github.com/alice/riyabot-weather",
            source_ref="v1.2.3",
            source_commit="b" * 40,
            artifact_sha256=None,
            installed_version="1.2.3",
            installed_at=installed_at,
            updated_at=installed_at,
            last_checked_at=installed_at,
        )

        with patch.object(
            plugin_routes,
            "fetch_plugin_registry",
            new=AsyncMock(return_value=self._snapshot()),
        ):
            result = await plugin_routes.get_plugin_market(
                page=1,
                page_size=20,
                query=None,
                **self.auth_kwargs(),
            )

        self.assertEqual(len(result.plugins), 1)
        self.assertIsNone(result.plugins[0].installation)

    async def test_market_catalog_does_not_merge_installation_from_another_registry(self) -> None:
        plugin_id = "github.alice.weather"
        plugin_path = self.plugins_dir / "github_alice_weather"
        write_manifest(plugin_path, manifest_v2())
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)
        self.installation_store.sources[plugin_id] = InstallationSource(
            plugin_id=plugin_id,
            install_method="market",
            registry_url="https://old-registry.example/registry.json",
            repository_url="https://github.com/alice/riyabot-weather",
            source_ref="v1.2.3",
            source_commit="a" * 40,
            artifact_sha256=None,
            installed_version="1.2.3",
            installed_at=installed_at,
            updated_at=installed_at,
            last_checked_at=installed_at,
        )

        with patch.object(
            plugin_routes,
            "fetch_plugin_registry",
            new=AsyncMock(return_value=self._snapshot()),
        ):
            result = await plugin_routes.get_plugin_market(
                page=1,
                page_size=20,
                query=None,
                **self.auth_kwargs(),
            )

        self.assertEqual(len(result.plugins), 1)
        self.assertIsNone(result.plugins[0].installation)

    async def test_market_catalog_marks_incompatible_versions_uninstallable(self) -> None:
        plugin_id = "github.alice.weather"
        versions = [
            registry_document()["plugins"][plugin_id]["versions"][0],
            {
                "version": "1.3.0",
                "ref": "v1.3.0",
                "commit": "b" * 40,
                "status": "approved",
                "released_at": "2026-08-09T00:00:00Z",
                "manifest": manifest_v2(version="1.3.0"),
            },
        ]
        versions[1]["manifest"]["host_application"] = {"min_version": "9.0.0"}

        with (
            patch.object(
                plugin_routes,
                "fetch_plugin_registry",
                new=AsyncMock(return_value=self._snapshot(versions=versions)),
            ),
            patch.object(
                plugin_routes.global_config.webui,
                "plugin_registry_url",
                "https://plugins.riyabot.example/registry.json",
            ),
        ):
            result = await plugin_routes.get_plugin_market(
                page=1,
                page_size=50,
                query=None,
                **self.auth_kwargs(),
            )

        plugin = result.plugins[0]
        versions_by_number = {version.version: version for version in plugin.versions}
        self.assertEqual(plugin.latest_version, "1.2.3")
        self.assertTrue(versions_by_number["1.2.3"].installable)
        self.assertFalse(versions_by_number["1.3.0"].installable)

    async def test_market_catalog_marks_artifact_versions_uninstallable_until_artifact_support_exists(self) -> None:
        versions = [registry_document()["plugins"]["github.alice.weather"]["versions"][0]]
        versions[0]["artifact_url"] = "https://github.com/alice/riyabot-weather/releases/download/v1.2.3/plugin.zip"
        versions[0]["sha256"] = "c" * 64

        with (
            patch.object(
                plugin_routes,
                "fetch_plugin_registry",
                new=AsyncMock(return_value=self._snapshot(versions=versions)),
            ),
            patch.object(
                plugin_routes.global_config.webui,
                "plugin_registry_url",
                "https://plugins.riyabot.example/registry.json",
            ),
        ):
            result = await plugin_routes.get_plugin_market(
                page=1,
                page_size=50,
                query=None,
                **self.auth_kwargs(),
            )

        self.assertFalse(result.plugins[0].versions[0].installable)

    async def test_market_catalog_tiebreaks_equal_semver_precedence_by_release_time(self) -> None:
        versions = [
            {
                "version": "1.2.3+build.2",
                "ref": "v1.2.3-build.2",
                "commit": "b" * 40,
                "status": "approved",
                "released_at": "2026-08-08T00:00:00Z",
                "manifest": manifest_v2(version="1.2.3+build.2"),
            },
            {
                "version": "1.2.3+build.1",
                "ref": "v1.2.3-build.1",
                "commit": "a" * 40,
                "status": "approved",
                "released_at": "2026-08-09T00:00:00Z",
                "manifest": manifest_v2(version="1.2.3+build.1"),
            },
        ]

        with (
            patch.object(
                plugin_routes,
                "fetch_plugin_registry",
                new=AsyncMock(return_value=self._snapshot(versions=versions)),
            ),
            patch.object(
                plugin_routes.global_config.webui,
                "plugin_registry_url",
                "https://plugins.riyabot.example/registry.json",
            ),
        ):
            result = await plugin_routes.get_plugin_market(
                page=1,
                page_size=50,
                query=None,
                **self.auth_kwargs(),
            )

        self.assertEqual(result.plugins[0].latest_version, "1.2.3+build.1")

    async def test_market_catalog_requires_authentication_before_fetching(self) -> None:
        with patch.object(plugin_routes, "fetch_plugin_registry", new=AsyncMock()) as fetch:
            with self.assertRaises(HTTPException) as failure:
                await plugin_routes.get_plugin_market(
                    page=1,
                    page_size=20,
                    query=None,
                    maibot_session=None,
                    authorization=None,
                )

        self.assertEqual(failure.exception.status_code, 401)
        fetch.assert_not_awaited()

    async def test_market_catalog_maps_upstream_failures_without_exposing_details(self) -> None:
        cases = [
            (PluginRegistryFetchError("secret token"), "插件市场暂时不可用"),
            (PluginRegistryFormatError("secret path"), "插件市场数据无效"),
        ]

        for error, expected_detail in cases:
            with self.subTest(error=type(error).__name__):
                with (
                    patch.object(plugin_routes, "fetch_plugin_registry", new=AsyncMock(side_effect=error)),
                    patch.object(
                        plugin_routes.global_config.webui,
                        "plugin_registry_url",
                        "https://plugins.riyabot.example/registry.json",
                    ),
                    self.assertRaises(HTTPException) as failure,
                ):
                    await plugin_routes.get_plugin_market(
                        page=1,
                        page_size=20,
                        query=None,
                        **self.auth_kwargs(),
                    )

                self.assertEqual(failure.exception.status_code, 502)
                self.assertEqual(failure.exception.detail, expected_detail)
                self.assertNotIn("secret", str(failure.exception))

    async def test_bound_market_detail_uses_verified_installation_registry(self) -> None:
        plugin_id = "github.alice.weather"
        bound_registry_url = "https://old-registry.example/registry.json"
        plugin_path = self.plugins_dir / "github_alice_weather"
        write_manifest(plugin_path, manifest_v2())
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)
        source = InstallationSource(
            plugin_id=plugin_id,
            install_method="market",
            registry_url=bound_registry_url,
            repository_url="https://github.com/alice/riyabot-weather",
            source_ref="v1.2.3",
            source_commit="a" * 40,
            artifact_sha256=None,
            installed_version="1.2.3",
            installed_at=installed_at,
            updated_at=installed_at,
            last_checked_at=installed_at,
        )
        self.installation_store.sources[plugin_id] = source
        snapshot = PluginRegistrySnapshot(
            registry_url=bound_registry_url,
            registry=PluginRegistry.model_validate(registry_document()),
            fetched_at=datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.timezone.utc),
        )
        fetch = AsyncMock(return_value=snapshot)

        with (
            patch.object(plugin_routes, "fetch_plugin_registry", new=fetch),
            patch.object(
                plugin_routes.global_config.webui,
                "plugin_registry_url",
                "https://current-registry.example/registry.json",
            ),
        ):
            result = await plugin_routes.get_bound_market_plugin(plugin_id, **self.auth_kwargs())

        self.assertEqual(result.registry.url, bound_registry_url)
        self.assertEqual(result.plugin.id, plugin_id)
        self.assertEqual(result.plugin.installation.registry_url, bound_registry_url)
        self.assertEqual(result.plugin.versions[0].version, "1.2.3")
        fetch.assert_awaited_once_with(
            bound_registry_url,
            cache_ttl_seconds=plugin_routes.PLUGIN_REGISTRY_CATALOG_CACHE_SECONDS,
        )

    async def test_bound_market_detail_rejects_local_source_drift_before_fetching(self) -> None:
        plugin_id = "github.alice.weather"
        plugin_path = self.plugins_dir / "github_alice_weather"
        write_manifest(plugin_path, manifest_v2(version="9.9.9"))
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)
        self.installation_store.sources[plugin_id] = InstallationSource(
            plugin_id=plugin_id,
            install_method="market",
            registry_url="https://old-registry.example/registry.json",
            repository_url="https://github.com/alice/riyabot-weather",
            source_ref="v1.2.3",
            source_commit="a" * 40,
            artifact_sha256=None,
            installed_version="1.2.3",
            installed_at=installed_at,
            updated_at=installed_at,
            last_checked_at=installed_at,
        )
        fetch = AsyncMock()

        with (
            patch.object(plugin_routes, "fetch_plugin_registry", new=fetch),
            self.assertRaises(HTTPException) as failure,
        ):
            await plugin_routes.get_bound_market_plugin(plugin_id, **self.auth_kwargs())

        self.assertEqual(failure.exception.status_code, 409)
        fetch.assert_not_awaited()

    async def test_bound_market_detail_rejects_registry_coordinate_drift(self) -> None:
        plugin_id = "github.alice.weather"
        plugin_path = self.plugins_dir / "github_alice_weather"
        write_manifest(plugin_path, manifest_v2())
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)
        self.installation_store.sources[plugin_id] = InstallationSource(
            plugin_id=plugin_id,
            install_method="market",
            registry_url="https://old-registry.example/registry.json",
            repository_url="https://github.com/alice/riyabot-weather",
            source_ref="v1.2.3",
            source_commit="b" * 40,
            artifact_sha256=None,
            installed_version="1.2.3",
            installed_at=installed_at,
            updated_at=installed_at,
            last_checked_at=installed_at,
        )
        snapshot = PluginRegistrySnapshot(
            registry_url="https://old-registry.example/registry.json",
            registry=PluginRegistry.model_validate(registry_document()),
            fetched_at=datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.timezone.utc),
        )

        with (
            patch.object(plugin_routes, "fetch_plugin_registry", new=AsyncMock(return_value=snapshot)),
            self.assertRaises(HTTPException) as failure,
        ):
            await plugin_routes.get_bound_market_plugin(plugin_id, **self.auth_kwargs())

        self.assertEqual(failure.exception.status_code, 409)


class PluginMarketLifecycleRoutesTest(PluginRouteBase):
    registry_url = "https://plugins.riyabot.example/registry.json"

    @staticmethod
    def _snapshot(*, versions: list[dict] | None = None) -> PluginRegistrySnapshot:
        return PluginRegistrySnapshot(
            registry_url=PluginMarketLifecycleRoutesTest.registry_url,
            registry=PluginRegistry.model_validate(registry_document(versions=versions)),
            fetched_at=datetime.datetime(2026, 8, 9, 12, 0, tzinfo=datetime.timezone.utc),
        )

    async def test_market_install_uses_reviewed_tag_commit_and_persists_source(self) -> None:
        async def clone_reviewed_version(**kwargs):
            target_path = kwargs["target_path"]
            write_manifest(target_path, manifest_v2())
            (target_path / "plugin.py").write_text("PLUGIN = True\n", encoding="utf-8")
            (target_path / ".git").mkdir()
            (target_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
            return {"success": True, "path": str(target_path), "attempts": 1, "commit": "a" * 40}

        self.service.clone_repository = AsyncMock(side_effect=clone_reviewed_version)
        with (
            patch.object(plugin_routes, "fetch_plugin_registry", new=AsyncMock(return_value=self._snapshot())),
            patch.object(plugin_routes, "update_progress", new=AsyncMock()),
            patch.object(plugin_routes.global_config.webui, "plugin_registry_url", self.registry_url),
        ):
            result = await plugin_routes.install_market_plugin(
                plugin_routes.MarketPluginRequest(plugin_id="github.alice.weather", version="1.2.3"),
                **self.auth_kwargs(),
            )

        installed_path = self.plugins_dir / "github_alice_weather"
        self.assertTrue(result["success"])
        self.assertTrue((installed_path / "plugin.py").is_file())
        self.assertFalse((installed_path / ".git").exists())
        installed_manifest = json.loads((installed_path / "_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(installed_manifest["id"], "github.alice.weather")
        clone_kwargs = self.service.clone_repository.await_args.kwargs
        self.assertEqual(clone_kwargs["branch"], "v1.2.3")
        self.assertEqual(clone_kwargs["expected_commit"], "a" * 40)
        self.assertEqual(clone_kwargs["depth"], 1)
        source = self.installation_store.get("github.alice.weather")
        self.assertEqual(source.install_method, "market")
        self.assertEqual(source.source_ref, "v1.2.3")
        self.assertEqual(source.source_commit, "a" * 40)

    async def test_market_install_rejects_artifact_until_artifact_support_exists(self) -> None:
        versions = [registry_document()["plugins"]["github.alice.weather"]["versions"][0]]
        versions[0]["artifact_url"] = "https://github.com/alice/riyabot-weather/releases/download/v1.2.3/plugin.zip"
        versions[0]["sha256"] = "c" * 64
        self.service.clone_repository = AsyncMock()

        with (
            patch.object(
                plugin_routes,
                "fetch_plugin_registry",
                new=AsyncMock(return_value=self._snapshot(versions=versions)),
            ),
            patch.object(plugin_routes, "update_progress", new=AsyncMock()),
            patch.object(plugin_routes.global_config.webui, "plugin_registry_url", self.registry_url),
            self.assertRaises(HTTPException) as failure,
        ):
            await plugin_routes.install_market_plugin(
                plugin_routes.MarketPluginRequest(plugin_id="github.alice.weather", version="1.2.3"),
                **self.auth_kwargs(),
            )

        self.assertEqual(failure.exception.status_code, 501)
        self.service.clone_repository.assert_not_awaited()

    async def test_market_install_rejects_unreviewed_manifest_identity(self) -> None:
        async def clone_wrong_manifest(**kwargs):
            target_path = kwargs["target_path"]
            write_manifest(target_path, manifest_v2(plugin_id="github.mallory.weather"))
            (target_path / "plugin.py").write_text("PLUGIN = True\n", encoding="utf-8")
            return {"success": True, "path": str(target_path), "attempts": 1, "commit": "a" * 40}

        self.service.clone_repository = AsyncMock(side_effect=clone_wrong_manifest)
        with (
            patch.object(plugin_routes, "fetch_plugin_registry", new=AsyncMock(return_value=self._snapshot())),
            patch.object(plugin_routes, "update_progress", new=AsyncMock()),
            patch.object(plugin_routes.global_config.webui, "plugin_registry_url", self.registry_url),
            self.assertRaises(HTTPException) as failure,
        ):
            await plugin_routes.install_market_plugin(
                plugin_routes.MarketPluginRequest(plugin_id="github.alice.weather", version="1.2.3"),
                **self.auth_kwargs(),
            )

        self.assertEqual(failure.exception.status_code, 400)
        self.assertFalse((self.plugins_dir / "github_alice_weather").exists())
        self.assertIsNone(self.installation_store.get("github.alice.weather"))

    async def test_market_install_rejects_manifest_that_differs_from_reviewed_snapshot(self) -> None:
        async def clone_changed_manifest(**kwargs):
            target_path = kwargs["target_path"]
            changed = manifest_v2()
            changed["description"] = "审核后被修改"
            write_manifest(target_path, changed)
            (target_path / "plugin.py").write_text("PLUGIN = True\n", encoding="utf-8")
            return {"success": True, "path": str(target_path), "attempts": 1, "commit": "a" * 40}

        self.service.clone_repository = AsyncMock(side_effect=clone_changed_manifest)
        with (
            patch.object(plugin_routes, "fetch_plugin_registry", new=AsyncMock(return_value=self._snapshot())),
            patch.object(plugin_routes, "update_progress", new=AsyncMock()),
            patch.object(plugin_routes.global_config.webui, "plugin_registry_url", self.registry_url),
            self.assertRaises(HTTPException) as failure,
        ):
            await plugin_routes.install_market_plugin(
                plugin_routes.MarketPluginRequest(plugin_id="github.alice.weather", version="1.2.3"),
                **self.auth_kwargs(),
            )

        self.assertEqual(failure.exception.status_code, 400)
        self.assertFalse((self.plugins_dir / "github_alice_weather").exists())

    async def test_market_install_rejects_duplicate_manifest_fields(self) -> None:
        async def clone_ambiguous_manifest(**kwargs):
            target_path = kwargs["target_path"]
            target_path.mkdir(parents=True)
            raw = json.dumps(manifest_v2())
            raw = raw.replace('"version": "1.2.3"', '"version": "1.2.3", "version": "1.2.3"', 1)
            (target_path / "_manifest.json").write_text(raw, encoding="utf-8")
            (target_path / "plugin.py").write_text("PLUGIN = True\n", encoding="utf-8")
            return {"success": True, "path": str(target_path), "attempts": 1, "commit": "a" * 40}

        self.service.clone_repository = AsyncMock(side_effect=clone_ambiguous_manifest)
        with (
            patch.object(plugin_routes, "fetch_plugin_registry", new=AsyncMock(return_value=self._snapshot())),
            patch.object(plugin_routes, "update_progress", new=AsyncMock()),
            patch.object(plugin_routes.global_config.webui, "plugin_registry_url", self.registry_url),
            self.assertRaises(HTTPException) as failure,
        ):
            await plugin_routes.install_market_plugin(
                plugin_routes.MarketPluginRequest(plugin_id="github.alice.weather", version="1.2.3"),
                **self.auth_kwargs(),
            )

        self.assertEqual(failure.exception.status_code, 400)
        self.assertFalse((self.plugins_dir / "github_alice_weather").exists())

    async def test_market_install_rejects_symlinks_and_oversized_trees(self) -> None:
        async def clone_symlink(**kwargs):
            target_path = kwargs["target_path"]
            write_manifest(target_path, manifest_v2())
            (target_path / "plugin.py").write_text("PLUGIN = True\n", encoding="utf-8")
            outside = target_path.parent / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            (target_path / "linked.txt").symlink_to(outside)
            return {"success": True, "path": str(target_path), "attempts": 1, "commit": "a" * 40}

        async def clone_oversized(**kwargs):
            target_path = kwargs["target_path"]
            write_manifest(target_path, manifest_v2())
            (target_path / "plugin.py").write_text("PLUGIN = True\n", encoding="utf-8")
            return {"success": True, "path": str(target_path), "attempts": 1, "commit": "a" * 40}

        cases = [
            (clone_symlink, None, 400),
            (clone_oversized, 1, 413),
        ]
        for clone, byte_limit, expected_status in cases:
            with self.subTest(expected_status=expected_status):
                self.service.clone_repository = AsyncMock(side_effect=clone)
                effective_limit = byte_limit or plugin_routes.MAX_MARKET_PLUGIN_BYTES
                with (
                    patch.object(plugin_routes, "fetch_plugin_registry", new=AsyncMock(return_value=self._snapshot())),
                    patch.object(plugin_routes, "update_progress", new=AsyncMock()),
                    patch.object(plugin_routes.global_config.webui, "plugin_registry_url", self.registry_url),
                    patch.object(plugin_routes, "MAX_MARKET_PLUGIN_BYTES", effective_limit),
                    self.assertRaises(HTTPException) as failure,
                ):
                    await plugin_routes.install_market_plugin(
                        plugin_routes.MarketPluginRequest(plugin_id="github.alice.weather", version="1.2.3"),
                        **self.auth_kwargs(),
                    )

                self.assertEqual(failure.exception.status_code, expected_status)
                self.assertFalse((self.plugins_dir / "github_alice_weather").exists())

    async def test_market_install_rejects_incompatible_reviewed_manifest(self) -> None:
        incompatible = manifest_v2()
        incompatible["host_application"] = {"min_version": "9.0.0"}
        versions = [
            {
                "version": "1.2.3",
                "ref": "v1.2.3",
                "commit": "a" * 40,
                "status": "approved",
                "released_at": "2026-08-08T00:00:00Z",
                "manifest": incompatible,
            }
        ]

        async def clone_incompatible(**kwargs):
            target_path = kwargs["target_path"]
            write_manifest(target_path, incompatible)
            (target_path / "plugin.py").write_text("PLUGIN = True\n", encoding="utf-8")
            return {"success": True, "path": str(target_path), "attempts": 1, "commit": "a" * 40}

        self.service.clone_repository = AsyncMock(side_effect=clone_incompatible)
        with (
            patch.object(
                plugin_routes,
                "fetch_plugin_registry",
                new=AsyncMock(return_value=self._snapshot(versions=versions)),
            ),
            patch.object(plugin_routes, "update_progress", new=AsyncMock()) as progress,
            patch.object(plugin_routes.global_config.webui, "plugin_registry_url", self.registry_url),
            self.assertRaises(HTTPException) as failure,
        ):
            await plugin_routes.install_market_plugin(
                plugin_routes.MarketPluginRequest(plugin_id="github.alice.weather", version="1.2.3"),
                **self.auth_kwargs(),
            )

        self.assertEqual(failure.exception.status_code, 400)
        self.assertFalse((self.plugins_dir / "github_alice_weather").exists())
        self.service.clone_repository.assert_not_awaited()
        self.assertEqual(progress.await_args_list[-1].kwargs["stage"], "error")
        self.assertEqual(progress.await_args_list[-1].kwargs["operation"], "install")

    async def test_market_install_removes_plugin_when_source_persistence_fails(self) -> None:
        async def clone_reviewed_version(**kwargs):
            target_path = kwargs["target_path"]
            write_manifest(target_path, manifest_v2())
            (target_path / "plugin.py").write_text("PLUGIN = True\n", encoding="utf-8")
            return {"success": True, "path": str(target_path), "attempts": 1, "commit": "a" * 40}

        self.installation_store.fail_save = True
        self.service.clone_repository = AsyncMock(side_effect=clone_reviewed_version)
        with (
            patch.object(plugin_routes, "fetch_plugin_registry", new=AsyncMock(return_value=self._snapshot())),
            patch.object(plugin_routes, "update_progress", new=AsyncMock()),
            patch.object(plugin_routes.global_config.webui, "plugin_registry_url", self.registry_url),
            self.assertRaises(HTTPException) as failure,
        ):
            await plugin_routes.install_market_plugin(
                plugin_routes.MarketPluginRequest(plugin_id="github.alice.weather", version="1.2.3"),
                **self.auth_kwargs(),
            )

        self.assertEqual(failure.exception.status_code, 500)
        self.assertEqual(failure.exception.detail, "插件安装失败")
        self.assertFalse((self.plugins_dir / "github_alice_weather").exists())

    async def test_market_update_uses_bound_registry_and_restores_old_version_on_database_failure(self) -> None:
        plugin_id = "github.alice.weather"
        plugin_path = self.plugins_dir / "github_alice_weather"
        write_manifest(plugin_path, manifest_v2(version="1.2.3"))
        (plugin_path / "plugin.py").write_text("OLD = True\n", encoding="utf-8")
        (plugin_path / "old.txt").write_text("keep", encoding="utf-8")
        (plugin_path / "config.toml").write_text("token = 'keep'\n", encoding="utf-8")
        (plugin_path / "data").mkdir()
        (plugin_path / "data" / "state.db").write_text("database", encoding="utf-8")
        config_inode = (plugin_path / "config.toml").stat().st_ino
        database_inode = (plugin_path / "data" / "state.db").stat().st_ino
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)
        self.installation_store.sources[plugin_id] = InstallationSource(
            plugin_id=plugin_id,
            install_method="market",
            registry_url=self.registry_url,
            repository_url="https://github.com/alice/riyabot-weather",
            source_ref="v1.2.3",
            source_commit="a" * 40,
            artifact_sha256=None,
            installed_version="1.2.3",
            installed_at=installed_at,
            updated_at=installed_at,
            last_checked_at=installed_at,
        )
        versions = [
            registry_document()["plugins"][plugin_id]["versions"][0],
            {
                "version": "1.3.0",
                "ref": "v1.3.0",
                "commit": "b" * 40,
                "status": "approved",
                "released_at": "2026-08-09T00:00:00Z",
                "manifest": manifest_v2(version="1.3.0"),
            },
        ]

        async def clone_new_version(**kwargs):
            target_path = kwargs["target_path"]
            write_manifest(target_path, manifest_v2(version="1.3.0"))
            (target_path / "plugin.py").write_text("NEW = True\n", encoding="utf-8")
            (target_path / "new.txt").write_text("new", encoding="utf-8")
            return {"success": True, "path": str(target_path), "attempts": 1, "commit": "b" * 40}

        self.installation_store.fail_save = True
        self.service.clone_repository = AsyncMock(side_effect=clone_new_version)
        fetch = AsyncMock(return_value=self._snapshot(versions=versions))
        with (
            patch.object(plugin_routes, "fetch_plugin_registry", new=fetch),
            patch.object(plugin_routes, "update_progress", new=AsyncMock()) as progress,
            patch.object(
                plugin_routes.global_config.webui,
                "plugin_registry_url",
                "https://different.example/registry.json",
            ),
            self.assertRaises(HTTPException) as failure,
        ):
            await plugin_routes.update_market_plugin(
                plugin_routes.MarketPluginRequest(plugin_id=plugin_id, version="1.3.0"),
                **self.auth_kwargs(),
            )

        self.assertEqual(failure.exception.status_code, 500)
        self.assertEqual((plugin_path / "old.txt").read_text(encoding="utf-8"), "keep")
        self.assertEqual((plugin_path / "config.toml").read_text(encoding="utf-8"), "token = 'keep'\n")
        self.assertEqual((plugin_path / "data" / "state.db").read_text(encoding="utf-8"), "database")
        self.assertEqual((plugin_path / "config.toml").stat().st_ino, config_inode)
        self.assertEqual((plugin_path / "data" / "state.db").stat().st_ino, database_inode)
        self.assertFalse((plugin_path / "new.txt").exists())
        restored = json.loads((plugin_path / "_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(restored["version"], "1.2.3")
        fetch.assert_awaited_once_with(self.registry_url)
        clone_kwargs = self.service.clone_repository.await_args.kwargs
        self.assertEqual(clone_kwargs["branch"], "v1.3.0")
        self.assertEqual(clone_kwargs["expected_commit"], "b" * 40)
        self.assertEqual(progress.await_args_list[-1].kwargs["stage"], "error")
        self.assertEqual(progress.await_args_list[-1].kwargs["operation"], "update")

    async def test_market_update_preserves_only_controlled_runtime_state(self) -> None:
        plugin_id = "github.alice.weather"
        plugin_path = self.plugins_dir / "github_alice_weather"
        write_manifest(plugin_path, manifest_v2(version="1.2.3"))
        (plugin_path / "plugin.py").write_text("OLD = True\n", encoding="utf-8")
        (plugin_path / "old_code.py").write_text("OLD_CODE = True\n", encoding="utf-8")
        (plugin_path / "config.toml").write_text("token = 'keep'\n", encoding="utf-8")
        (plugin_path / "plugin_config.toml").write_text("enabled = true\n", encoding="utf-8")
        (plugin_path / "config.toml.backup.20260813").write_text("backup", encoding="utf-8")
        (plugin_path / "plugin_config.toml.backup_20260813").write_text("backup", encoding="utf-8")
        (plugin_path / "data").mkdir()
        (plugin_path / "data" / "state.db").write_text("database", encoding="utf-8")
        (plugin_path / "logs").mkdir()
        (plugin_path / "logs" / "plugin.log").write_text("log", encoding="utf-8")
        (plugin_path / "config_backup").mkdir()
        (plugin_path / "config_backup" / "config.toml.bak").write_text("backup", encoding="utf-8")
        config_inode = (plugin_path / "config.toml").stat().st_ino
        database_inode = (plugin_path / "data" / "state.db").stat().st_ino
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)
        self.installation_store.sources[plugin_id] = InstallationSource(
            plugin_id=plugin_id,
            install_method="market",
            registry_url=self.registry_url,
            repository_url="https://github.com/alice/riyabot-weather",
            source_ref="v1.2.3",
            source_commit="a" * 40,
            artifact_sha256=None,
            installed_version="1.2.3",
            installed_at=installed_at,
            updated_at=installed_at,
            last_checked_at=installed_at,
        )
        versions = [
            registry_document()["plugins"][plugin_id]["versions"][0],
            {
                "version": "1.3.0",
                "ref": "v1.3.0",
                "commit": "b" * 40,
                "status": "approved",
                "released_at": "2026-08-09T00:00:00Z",
                "manifest": manifest_v2(version="1.3.0"),
            },
        ]

        async def clone_new_version(**kwargs):
            target_path = kwargs["target_path"]
            write_manifest(target_path, manifest_v2(version="1.3.0"))
            (target_path / "plugin.py").write_text("NEW = True\n", encoding="utf-8")
            return {"success": True, "path": str(target_path), "attempts": 1, "commit": "b" * 40}

        self.service.clone_repository = AsyncMock(side_effect=clone_new_version)
        with (
            patch.object(
                plugin_routes, "fetch_plugin_registry", new=AsyncMock(return_value=self._snapshot(versions=versions))
            ),
            patch.object(plugin_routes, "update_progress", new=AsyncMock()),
        ):
            result = await plugin_routes.update_market_plugin(
                plugin_routes.MarketPluginRequest(plugin_id=plugin_id, version="1.3.0"),
                **self.auth_kwargs(),
            )

        self.assertTrue(result["success"])
        self.assertEqual((plugin_path / "plugin.py").read_text(encoding="utf-8"), "NEW = True\n")
        self.assertFalse((plugin_path / "old_code.py").exists())
        self.assertEqual((plugin_path / "config.toml").read_text(encoding="utf-8"), "token = 'keep'\n")
        self.assertEqual((plugin_path / "plugin_config.toml").read_text(encoding="utf-8"), "enabled = true\n")
        self.assertTrue((plugin_path / "config.toml.backup.20260813").is_file())
        self.assertTrue((plugin_path / "plugin_config.toml.backup_20260813").is_file())
        self.assertEqual((plugin_path / "data" / "state.db").read_text(encoding="utf-8"), "database")
        self.assertEqual((plugin_path / "logs" / "plugin.log").read_text(encoding="utf-8"), "log")
        self.assertTrue((plugin_path / "config_backup" / "config.toml.bak").is_file())
        self.assertEqual((plugin_path / "config.toml").stat().st_ino, config_inode)
        self.assertEqual((plugin_path / "data" / "state.db").stat().st_ino, database_inode)

    async def test_market_update_rejects_unsafe_or_conflicting_runtime_state(self) -> None:
        plugin_id = "github.alice.weather"
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)
        versions = [
            registry_document()["plugins"][plugin_id]["versions"][0],
            {
                "version": "1.3.0",
                "ref": "v1.3.0",
                "commit": "b" * 40,
                "status": "approved",
                "released_at": "2026-08-09T00:00:00Z",
                "manifest": manifest_v2(version="1.3.0"),
            },
        ]

        for case, expected_status in (("symlink", 400), ("conflict", 409)):
            with self.subTest(case=case):
                plugin_path = self.plugins_dir / "github_alice_weather"
                write_manifest(plugin_path, manifest_v2(version="1.2.3"))
                (plugin_path / "plugin.py").write_text("OLD = True\n", encoding="utf-8")
                (plugin_path / "config.toml").write_text("old = true\n", encoding="utf-8")
                if case == "symlink":
                    (plugin_path / "data").mkdir()
                    outside = Path(self.tmp.name) / "outside.db"
                    outside.write_text("outside", encoding="utf-8")
                    (plugin_path / "data" / "state.db").symlink_to(outside)

                self.installation_store.sources[plugin_id] = InstallationSource(
                    plugin_id=plugin_id,
                    install_method="market",
                    registry_url=self.registry_url,
                    repository_url="https://github.com/alice/riyabot-weather",
                    source_ref="v1.2.3",
                    source_commit="a" * 40,
                    artifact_sha256=None,
                    installed_version="1.2.3",
                    installed_at=installed_at,
                    updated_at=installed_at,
                    last_checked_at=installed_at,
                )

                async def clone_new_version(*, case=case, **kwargs):
                    target_path = kwargs["target_path"]
                    write_manifest(target_path, manifest_v2(version="1.3.0"))
                    (target_path / "plugin.py").write_text("NEW = True\n", encoding="utf-8")
                    if case == "conflict":
                        (target_path / "config.toml").write_text("new = true\n", encoding="utf-8")
                    return {"success": True, "path": str(target_path), "attempts": 1, "commit": "b" * 40}

                self.service.clone_repository = AsyncMock(side_effect=clone_new_version)
                with (
                    patch.object(
                        plugin_routes,
                        "fetch_plugin_registry",
                        new=AsyncMock(return_value=self._snapshot(versions=versions)),
                    ),
                    patch.object(plugin_routes, "update_progress", new=AsyncMock()),
                    self.assertRaises(HTTPException) as failure,
                ):
                    await plugin_routes.update_market_plugin(
                        plugin_routes.MarketPluginRequest(plugin_id=plugin_id, version="1.3.0"),
                        **self.auth_kwargs(),
                    )

                self.assertEqual(failure.exception.status_code, expected_status)
                self.assertEqual((plugin_path / "plugin.py").read_text(encoding="utf-8"), "OLD = True\n")
                self.assertEqual((plugin_path / "config.toml").read_text(encoding="utf-8"), "old = true\n")
                if case == "symlink":
                    self.assertEqual(outside.read_text(encoding="utf-8"), "outside")
                shutil.rmtree(plugin_path)

    async def test_market_update_rejects_local_manifest_source_drift_before_fetching(self) -> None:
        plugin_id = "github.alice.weather"
        plugin_path = self.plugins_dir / "github_alice_weather"
        write_manifest(plugin_path, manifest_v2(version="9.9.9"))
        (plugin_path / "plugin.py").write_text("LOCAL = True\n", encoding="utf-8")
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)
        self.installation_store.sources[plugin_id] = InstallationSource(
            plugin_id=plugin_id,
            install_method="market",
            registry_url=self.registry_url,
            repository_url="https://github.com/alice/riyabot-weather",
            source_ref="v1.2.3",
            source_commit="a" * 40,
            artifact_sha256=None,
            installed_version="1.2.3",
            installed_at=installed_at,
            updated_at=installed_at,
            last_checked_at=installed_at,
        )
        fetch = AsyncMock(return_value=self._snapshot())
        self.service.clone_repository = AsyncMock()

        with (
            patch.object(plugin_routes, "fetch_plugin_registry", new=fetch),
            patch.object(plugin_routes, "update_progress", new=AsyncMock()),
            self.assertRaises(HTTPException) as failure,
        ):
            await plugin_routes.update_market_plugin(
                plugin_routes.MarketPluginRequest(plugin_id=plugin_id, version="1.2.3"),
                **self.auth_kwargs(),
            )

        self.assertEqual(failure.exception.status_code, 409)
        fetch.assert_not_awaited()
        self.service.clone_repository.assert_not_awaited()
        self.assertEqual((plugin_path / "plugin.py").read_text(encoding="utf-8"), "LOCAL = True\n")

    async def test_market_update_rejects_registry_coordinate_drift_before_cloning(self) -> None:
        plugin_id = "github.alice.weather"
        plugin_path = self.plugins_dir / "github_alice_weather"
        write_manifest(plugin_path, manifest_v2(version="1.2.3"))
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)
        self.installation_store.sources[plugin_id] = InstallationSource(
            plugin_id=plugin_id,
            install_method="market",
            registry_url=self.registry_url,
            repository_url="https://github.com/alice/riyabot-weather",
            source_ref="v1.2.3",
            source_commit="b" * 40,
            artifact_sha256=None,
            installed_version="1.2.3",
            installed_at=installed_at,
            updated_at=installed_at,
            last_checked_at=installed_at,
        )
        versions = [
            registry_document()["plugins"][plugin_id]["versions"][0],
            {
                "version": "1.3.0",
                "ref": "v1.3.0",
                "commit": "c" * 40,
                "status": "approved",
                "released_at": "2026-08-10T00:00:00Z",
                "manifest": manifest_v2(version="1.3.0"),
            },
        ]
        self.service.clone_repository = AsyncMock()

        with (
            patch.object(
                plugin_routes,
                "fetch_plugin_registry",
                new=AsyncMock(return_value=self._snapshot(versions=versions)),
            ),
            patch.object(plugin_routes, "update_progress", new=AsyncMock()),
            self.assertRaises(HTTPException) as failure,
        ):
            await plugin_routes.update_market_plugin(
                plugin_routes.MarketPluginRequest(plugin_id=plugin_id, version="1.3.0"),
                **self.auth_kwargs(),
            )

        self.assertEqual(failure.exception.status_code, 409)
        self.service.clone_repository.assert_not_awaited()
        self.assertEqual(
            json.loads((plugin_path / "_manifest.json").read_text(encoding="utf-8"))["version"],
            "1.2.3",
        )

    async def test_market_update_rejects_the_current_installed_version_before_fetching(self) -> None:
        plugin_id = "github.alice.weather"
        plugin_path = self.plugins_dir / "github_alice_weather"
        write_manifest(plugin_path, manifest_v2(version="1.2.3"))
        (plugin_path / "plugin.py").write_text("CURRENT = True\n", encoding="utf-8")
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)
        self.installation_store.sources[plugin_id] = InstallationSource(
            plugin_id=plugin_id,
            install_method="market",
            registry_url=self.registry_url,
            repository_url="https://github.com/alice/riyabot-weather",
            source_ref="v1.2.3",
            source_commit="a" * 40,
            artifact_sha256=None,
            installed_version="1.2.3",
            installed_at=installed_at,
            updated_at=installed_at,
            last_checked_at=installed_at,
        )
        fetch = AsyncMock(return_value=self._snapshot())
        self.service.clone_repository = AsyncMock()

        with (
            patch.object(plugin_routes, "fetch_plugin_registry", new=fetch),
            patch.object(plugin_routes, "update_progress", new=AsyncMock()),
            self.assertRaises(HTTPException) as failure,
        ):
            await plugin_routes.update_market_plugin(
                plugin_routes.MarketPluginRequest(plugin_id=plugin_id, version="1.2.3"),
                **self.auth_kwargs(),
            )

        self.assertEqual(failure.exception.status_code, 409)
        self.assertEqual(failure.exception.detail, "当前已安装此版本")
        fetch.assert_not_awaited()
        self.service.clone_repository.assert_not_awaited()
        self.assertEqual((plugin_path / "plugin.py").read_text(encoding="utf-8"), "CURRENT = True\n")


class PluginLifecycleRoutesTest(PluginRouteBase):
    async def test_clone_repository_validates_target_path_and_delegates_to_mirror_service(self) -> None:
        async def clone_success(**kwargs):
            target_path = kwargs["target_path"]
            target_path.mkdir(parents=True, exist_ok=True)
            return {
                "success": True,
                "path": str(target_path),
                "attempts": 1,
                "mirror_used": "github",
                "url": "https://github.com/Author/Plugin",
            }

        self.service.clone_repository = AsyncMock(side_effect=clone_success)

        result = await plugin_routes.clone_repository(
            plugin_routes.CloneRepositoryRequest(
                owner="Author",
                repo="Plugin",
                target_path="Author_Plugin",
                branch="main",
                depth=1,
            ),
            **self.auth_kwargs(),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 1)
        clone_kwargs = self.service.clone_repository.await_args.kwargs
        self.assertEqual(clone_kwargs["owner"], "Author")
        self.assertEqual(clone_kwargs["repo"], "Plugin")
        self.assertEqual(clone_kwargs["target_path"], self.plugins_dir.resolve() / "Author_Plugin")

        with self.assertRaises(HTTPException) as unsafe_path:
            await plugin_routes.clone_repository(
                plugin_routes.CloneRepositoryRequest(owner="Author", repo="Plugin", target_path="../escape"),
                **self.auth_kwargs(),
            )
        self.assertEqual(unsafe_path.exception.status_code, 400)

    async def test_clone_repository_failure_does_not_expose_internal_details(self) -> None:
        secret = 'credential="super-secret" at /private/repository'
        self.service.clone_repository = AsyncMock(side_effect=RuntimeError(secret))

        with (
            patch.object(plugin_routes.logger, "error") as logged,
            self.assertRaises(HTTPException) as failure,
        ):
            await plugin_routes.clone_repository(
                plugin_routes.CloneRepositoryRequest(
                    owner="Author",
                    repo="Plugin",
                    target_path="Author_Plugin",
                ),
                **self.auth_kwargs(),
            )

        self.assertEqual(failure.exception.status_code, 500)
        self.assertEqual(failure.exception.detail, "克隆仓库失败")
        self.assertNotIn(secret, repr(logged.call_args))

    async def test_install_plugin_writes_manifest_id_and_rejects_existing_or_invalid_clones(self) -> None:
        async def clone_with_manifest(**kwargs):
            target_path = kwargs["target_path"]
            write_manifest(
                target_path,
                {
                    "manifest_version": 1,
                    "name": "Plugin",
                    "version": "1.0.0",
                    "author": "Author",
                },
            )
            return {"success": True, "path": str(target_path), "attempts": 1, "commit": "c" * 40}

        self.service.clone_repository = AsyncMock(side_effect=clone_with_manifest)

        with patch.object(plugin_routes, "update_progress", new=AsyncMock()) as progress:
            result = await plugin_routes.install_plugin(
                plugin_routes.InstallPluginRequest(
                    plugin_id="Author.Plugin",
                    repository_url="https://github.com/Author/Plugin.git",
                    branch="main",
                ),
                **self.auth_kwargs(),
            )

        installed_path = self.plugins_dir / "Author_Plugin"
        self.assertTrue(result["success"])
        self.assertEqual(result["plugin_id"], "Author.Plugin")
        self.assertEqual(result["path"], "plugins/Author_Plugin")
        manifest = json.loads((installed_path / "_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["id"], "Author.Plugin")
        self.assertEqual(self.service.clone_repository.await_args.kwargs["depth"], 1)
        self.assertNotEqual(self.service.clone_repository.await_args.kwargs["target_path"], installed_path)
        self.assertEqual(progress.await_args_list[-1].kwargs["stage"], "success")
        source = self.installation_store.get("Author.Plugin")
        self.assertEqual(source.install_method, "git")
        self.assertEqual(source.repository_url, "https://github.com/Author/Plugin")
        self.assertEqual(source.source_ref, "main")
        self.assertEqual(source.source_commit, "c" * 40)

        with patch.object(plugin_routes, "update_progress", new=AsyncMock()) as existing_progress:
            with self.assertRaises(HTTPException) as existing_error:
                await plugin_routes.install_plugin(
                    plugin_routes.InstallPluginRequest(
                        plugin_id="Author.Plugin",
                        repository_url="https://github.com/Author/Plugin",
                    ),
                    **self.auth_kwargs(),
                )
        self.assertEqual(existing_error.exception.status_code, 400)
        self.assertEqual(existing_progress.await_args_list[-1].kwargs["stage"], "error")

        async def clone_without_manifest(**kwargs):
            kwargs["target_path"].mkdir(parents=True, exist_ok=True)
            return {"success": True, "path": str(kwargs["target_path"]), "attempts": 1}

        self.service.clone_repository = AsyncMock(side_effect=clone_without_manifest)
        with patch.object(plugin_routes, "update_progress", new=AsyncMock()):
            with self.assertRaises(HTTPException) as invalid_clone:
                await plugin_routes.install_plugin(
                    plugin_routes.InstallPluginRequest(
                        plugin_id="Author.Invalid",
                        repository_url="https://github.com/Author/Invalid",
                    ),
                    **self.auth_kwargs(),
                )
        self.assertEqual(invalid_clone.exception.status_code, 400)
        self.assertFalse((self.plugins_dir / "Author_Invalid").exists())

    async def test_git_install_removes_plugin_when_source_persistence_fails(self) -> None:
        async def clone_with_manifest(**kwargs):
            target_path = kwargs["target_path"]
            write_manifest(
                target_path,
                {
                    "manifest_version": 1,
                    "name": "Plugin",
                    "version": "1.0.0",
                    "author": "Author",
                },
            )
            return {"success": True, "path": str(target_path), "attempts": 1, "commit": "c" * 40}

        self.installation_store.fail_save = True
        self.service.clone_repository = AsyncMock(side_effect=clone_with_manifest)
        with (
            patch.object(plugin_routes, "update_progress", new=AsyncMock()),
            self.assertRaises(HTTPException) as failure,
        ):
            await plugin_routes.install_plugin(
                plugin_routes.InstallPluginRequest(
                    plugin_id="Author.Plugin",
                    repository_url="https://github.com/Author/Plugin",
                    branch="main",
                ),
                **self.auth_kwargs(),
            )

        self.assertEqual(failure.exception.status_code, 500)
        self.assertFalse((self.plugins_dir / "Author_Plugin").exists())

    async def test_git_install_preserves_matching_v2_identity_and_rejects_mismatch(self) -> None:
        async def clone_with_v2_manifest(**kwargs):
            target_path = kwargs["target_path"]
            write_manifest(target_path, manifest_v2())
            return {"success": True, "path": str(target_path), "attempts": 1, "commit": "c" * 40}

        self.service.clone_repository = AsyncMock(side_effect=clone_with_v2_manifest)
        with patch.object(plugin_routes, "update_progress", new=AsyncMock()):
            result = await plugin_routes.install_plugin(
                plugin_routes.InstallPluginRequest(
                    plugin_id="github.alice.weather",
                    repository_url="https://github.com/alice/riyabot-weather",
                    branch="main",
                ),
                **self.auth_kwargs(),
            )

        installed_path = self.plugins_dir / "github_alice_weather"
        installed_manifest = json.loads((installed_path / "_manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(result["success"])
        self.assertEqual(installed_manifest, manifest_v2())

        self.service.clone_repository = AsyncMock(side_effect=clone_with_v2_manifest)
        with (
            patch.object(plugin_routes, "update_progress", new=AsyncMock()),
            self.assertRaises(HTTPException) as mismatch,
        ):
            await plugin_routes.install_plugin(
                plugin_routes.InstallPluginRequest(
                    plugin_id="github.alice.other",
                    repository_url="https://github.com/alice/riyabot-weather",
                    branch="main",
                ),
                **self.auth_kwargs(),
            )

        self.assertEqual(mismatch.exception.status_code, 400)
        self.assertFalse((self.plugins_dir / "github_alice_other").exists())

    async def test_git_install_rejects_a_conflicting_declared_v1_identity(self) -> None:
        async def clone_with_declared_id(**kwargs):
            target_path = kwargs["target_path"]
            write_manifest(
                target_path,
                {
                    "id": "Author.RealPlugin",
                    "manifest_version": 1,
                    "name": "Plugin",
                    "version": "1.0.0",
                    "description": "Plugin",
                    "author": "Author",
                },
            )
            return {"success": True, "path": str(target_path), "attempts": 1, "commit": "c" * 40}

        self.service.clone_repository = AsyncMock(side_effect=clone_with_declared_id)
        with (
            patch.object(plugin_routes, "update_progress", new=AsyncMock()),
            self.assertRaises(HTTPException) as mismatch,
        ):
            await plugin_routes.install_plugin(
                plugin_routes.InstallPluginRequest(
                    plugin_id="Author.RequestedPlugin",
                    repository_url="https://github.com/Author/Plugin",
                    branch="main",
                ),
                **self.auth_kwargs(),
            )

        self.assertEqual(mismatch.exception.status_code, 400)
        self.assertFalse((self.plugins_dir / "Author_RequestedPlugin").exists())

    async def test_install_rejects_unsafe_manifests_and_sanitizes_clone_failures(self) -> None:
        outside_manifest = Path(self.tmp.name) / "outside-manifest.json"
        outside_content = json.dumps(
            {
                "manifest_version": 1,
                "name": "Outside",
                "version": "1.0.0",
                "author": "Author",
            }
        )
        outside_manifest.write_text(outside_content, encoding="utf-8")

        async def clone_with_linked_manifest(**kwargs):
            target_path = kwargs["target_path"]
            target_path.mkdir(parents=True, exist_ok=True)
            (target_path / "_manifest.json").symlink_to(outside_manifest)
            return {"success": True, "path": str(target_path), "attempts": 1}

        self.service.clone_repository = AsyncMock(side_effect=clone_with_linked_manifest)
        with patch.object(plugin_routes, "update_progress", new=AsyncMock()):
            with self.assertRaises(HTTPException) as linked_manifest:
                await plugin_routes.install_plugin(
                    plugin_routes.InstallPluginRequest(
                        plugin_id="Author.Linked",
                        repository_url="https://github.com/Author/Linked",
                    ),
                    **self.auth_kwargs(),
                )

        self.assertEqual(linked_manifest.exception.status_code, 400)
        self.assertEqual(outside_manifest.read_text(encoding="utf-8"), outside_content)
        self.assertFalse((self.plugins_dir / "Author_Linked").exists())

        outside_plugin = Path(self.tmp.name) / "outside-plugin"
        write_manifest(
            outside_plugin,
            {
                "manifest_version": 1,
                "name": "Outside Directory",
                "version": "1.0.0",
                "author": "Author",
            },
        )
        outside_plugin_content = (outside_plugin / "_manifest.json").read_text(encoding="utf-8")

        async def clone_with_linked_directory(**kwargs):
            kwargs["target_path"].symlink_to(outside_plugin, target_is_directory=True)
            return {"success": True, "path": str(kwargs["target_path"]), "attempts": 1}

        self.service.clone_repository = AsyncMock(side_effect=clone_with_linked_directory)
        with patch.object(plugin_routes, "update_progress", new=AsyncMock()):
            with self.assertRaises(HTTPException) as linked_directory:
                await plugin_routes.install_plugin(
                    plugin_routes.InstallPluginRequest(
                        plugin_id="Author.LinkedDirectory",
                        repository_url="https://github.com/Author/LinkedDirectory",
                    ),
                    **self.auth_kwargs(),
                )

        self.assertEqual(linked_directory.exception.status_code, 400)
        self.assertEqual(
            (outside_plugin / "_manifest.json").read_text(encoding="utf-8"),
            outside_plugin_content,
        )
        self.assertFalse((self.plugins_dir / "Author_LinkedDirectory").exists())

        async def clone_with_oversized_manifest(**kwargs):
            write_manifest(
                kwargs["target_path"],
                {
                    "manifest_version": 1,
                    "name": "Oversized",
                    "version": "1.0.0",
                    "author": "Author",
                    "description": "x" * plugin_routes.MAX_PLUGIN_MANIFEST_BYTES,
                },
            )
            return {"success": True, "path": str(kwargs["target_path"]), "attempts": 1}

        self.service.clone_repository = AsyncMock(side_effect=clone_with_oversized_manifest)
        with patch.object(plugin_routes, "update_progress", new=AsyncMock()):
            with self.assertRaises(HTTPException) as oversized_manifest:
                await plugin_routes.install_plugin(
                    plugin_routes.InstallPluginRequest(
                        plugin_id="Author.Oversized",
                        repository_url="https://github.com/Author/Oversized",
                    ),
                    **self.auth_kwargs(),
                )

        self.assertEqual(oversized_manifest.exception.status_code, 413)
        self.assertFalse((self.plugins_dir / "Author_Oversized").exists())

        secret = "clone failed at /private/plugins with api_key=super-secret"
        self.service.clone_repository = AsyncMock(side_effect=RuntimeError(secret))
        with (
            patch.object(plugin_routes, "update_progress", new=AsyncMock()) as progress,
            patch.object(plugin_routes.logger, "error") as logged,
            self.assertRaises(HTTPException) as clone_error,
        ):
            await plugin_routes.install_plugin(
                plugin_routes.InstallPluginRequest(
                    plugin_id="Author.Failure",
                    repository_url="https://github.com/Author/Failure",
                ),
                **self.auth_kwargs(),
            )

        self.assertEqual(clone_error.exception.status_code, 500)
        self.assertEqual(clone_error.exception.detail, "插件安装失败")
        self.assertNotIn("super-secret", repr(progress.await_args_list))
        self.assertNotIn("super-secret", repr(logged.call_args))

    async def test_uninstall_plugin_removes_new_format_directory_and_reports_missing_plugin(self) -> None:
        plugin_dir = self.plugins_dir / "Author_Plugin"
        write_manifest(
            plugin_dir,
            {
                "id": "Author.Plugin",
                "manifest_version": 1,
                "name": "Plugin",
                "version": "1.0.0",
                "author": "Author",
            },
        )
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)
        self.installation_store.sources["Author.Plugin"] = InstallationSource(
            plugin_id="Author.Plugin",
            install_method="git",
            registry_url=None,
            repository_url="https://github.com/Author/Plugin",
            source_ref="main",
            source_commit="c" * 40,
            artifact_sha256=None,
            installed_version="1.0.0",
            installed_at=installed_at,
            updated_at=installed_at,
            last_checked_at=installed_at,
        )

        with patch.object(plugin_routes, "update_progress", new=AsyncMock()) as progress:
            result = await plugin_routes.uninstall_plugin(
                plugin_routes.UninstallPluginRequest(plugin_id="Author.Plugin"),
                **self.auth_kwargs(),
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["plugin_name"], "Plugin")
        self.assertFalse(plugin_dir.exists())
        self.assertIsNone(self.installation_store.get("Author.Plugin"))
        self.assertEqual(progress.await_args_list[-1].kwargs["stage"], "success")

        with patch.object(plugin_routes, "update_progress", new=AsyncMock()) as missing_progress:
            with self.assertRaises(HTTPException) as missing_error:
                await plugin_routes.uninstall_plugin(
                    plugin_routes.UninstallPluginRequest(plugin_id="Author.Plugin"),
                    **self.auth_kwargs(),
                )
        self.assertEqual(missing_error.exception.status_code, 404)
        self.assertEqual(missing_progress.await_args_list[-1].kwargs["stage"], "error")

    async def test_uninstall_restores_plugin_when_source_delete_fails(self) -> None:
        plugin_dir = self.plugins_dir / "Author_Plugin"
        write_manifest(
            plugin_dir,
            {
                "id": "Author.Plugin",
                "manifest_version": 1,
                "name": "Plugin",
                "version": "1.0.0",
                "author": "Author",
            },
        )
        (plugin_dir / "state.txt").write_text("keep", encoding="utf-8")
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)
        self.installation_store.sources["Author.Plugin"] = InstallationSource(
            plugin_id="Author.Plugin",
            install_method="git",
            registry_url=None,
            repository_url="https://github.com/Author/Plugin",
            source_ref="main",
            source_commit="c" * 40,
            artifact_sha256=None,
            installed_version="1.0.0",
            installed_at=installed_at,
            updated_at=installed_at,
            last_checked_at=installed_at,
        )
        self.installation_store.fail_delete = True

        with (
            patch.object(plugin_routes, "update_progress", new=AsyncMock()),
            self.assertRaises(HTTPException) as failure,
        ):
            await plugin_routes.uninstall_plugin(
                plugin_routes.UninstallPluginRequest(plugin_id="Author.Plugin"),
                **self.auth_kwargs(),
            )

        self.assertEqual(failure.exception.status_code, 500)
        self.assertEqual((plugin_dir / "state.txt").read_text(encoding="utf-8"), "keep")
        self.assertIsNotNone(self.installation_store.get("Author.Plugin"))

    async def test_uninstall_restores_plugin_and_source_when_file_cleanup_fails(self) -> None:
        plugin_dir = self.plugins_dir / "Author_Plugin"
        write_manifest(
            plugin_dir,
            {
                "id": "Author.Plugin",
                "manifest_version": 1,
                "name": "Plugin",
                "version": "1.0.0",
                "author": "Author",
            },
        )
        (plugin_dir / "state.txt").write_text("keep", encoding="utf-8")
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)
        source = InstallationSource(
            plugin_id="Author.Plugin",
            install_method="git",
            registry_url=None,
            repository_url="https://github.com/Author/Plugin",
            source_ref="main",
            source_commit="c" * 40,
            artifact_sha256=None,
            installed_version="1.0.0",
            installed_at=installed_at,
            updated_at=installed_at,
            last_checked_at=installed_at,
        )
        self.installation_store.sources[source.plugin_id] = source

        with (
            patch.object(plugin_routes, "_remove_plugin_tree", side_effect=PermissionError("denied")),
            patch.object(plugin_routes, "update_progress", new=AsyncMock()),
            self.assertRaises(HTTPException) as failure,
        ):
            await plugin_routes.uninstall_plugin(
                plugin_routes.UninstallPluginRequest(plugin_id="Author.Plugin"),
                **self.auth_kwargs(),
            )

        self.assertEqual(failure.exception.status_code, 500)
        self.assertEqual((plugin_dir / "state.txt").read_text(encoding="utf-8"), "keep")
        self.assertEqual(self.installation_store.get(source.plugin_id), source)

    async def test_uninstall_does_not_trust_a_folder_name_over_manifest_identity(self) -> None:
        for folder_name in ("Author_Plugin", "Author.Plugin"):
            with self.subTest(folder_name=folder_name):
                plugin_dir = self.plugins_dir / folder_name
                write_manifest(
                    plugin_dir,
                    {
                        "id": "Other.Plugin",
                        "manifest_version": 1,
                        "name": "Other Plugin",
                        "version": "1.0.0",
                        "description": "Other Plugin",
                        "author": "Other",
                    },
                )

                with (
                    patch.object(plugin_routes, "update_progress", new=AsyncMock()),
                    self.assertRaises(HTTPException) as missing,
                ):
                    await plugin_routes.uninstall_plugin(
                        plugin_routes.UninstallPluginRequest(plugin_id="Author.Plugin"),
                        **self.auth_kwargs(),
                    )

                self.assertEqual(missing.exception.status_code, 404)
                self.assertTrue(plugin_dir.exists())
                shutil.rmtree(plugin_dir)

    async def test_update_plugin_replaces_old_manifest_and_cleans_invalid_new_clone(self) -> None:
        plugin_dir = self.plugins_dir / "Author_Plugin"
        write_manifest(
            plugin_dir,
            {
                "id": "Author.Plugin",
                "manifest_version": 1,
                "name": "Plugin",
                "version": "1.0.0",
                "author": "Author",
            },
        )
        (plugin_dir / "old.txt").write_text("old", encoding="utf-8")
        (plugin_dir / "config.toml").write_text("token = 'keep'\n", encoding="utf-8")
        (plugin_dir / "data").mkdir()
        (plugin_dir / "data" / "state.db").write_text("database", encoding="utf-8")

        async def clone_new_version(**kwargs):
            target_path = kwargs["target_path"]
            write_manifest(
                target_path,
                {
                    "manifest_version": 1,
                    "name": "Plugin",
                    "version": "2.0.0",
                    "author": "Author",
                },
            )
            (target_path / "new.txt").write_text("new", encoding="utf-8")
            return {"success": True, "path": str(target_path), "attempts": 1, "commit": "d" * 40}

        self.service.clone_repository = AsyncMock(side_effect=clone_new_version)

        with patch.object(plugin_routes, "update_progress", new=AsyncMock()) as progress:
            result = await plugin_routes.update_plugin(
                plugin_routes.UpdatePluginRequest(
                    plugin_id="Author.Plugin",
                    repository_url="https://github.com/Author/Plugin",
                    branch="main",
                ),
                **self.auth_kwargs(),
            )

        self.assertTrue(result["success"])
        self.assertEqual((result["old_version"], result["new_version"]), ("1.0.0", "2.0.0"))
        self.assertFalse((plugin_dir / "old.txt").exists())
        self.assertTrue((plugin_dir / "new.txt").exists())
        self.assertEqual((plugin_dir / "config.toml").read_text(encoding="utf-8"), "token = 'keep'\n")
        self.assertEqual((plugin_dir / "data" / "state.db").read_text(encoding="utf-8"), "database")
        updated_manifest = json.loads((plugin_dir / "_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(updated_manifest["id"], "Author.Plugin")
        self.assertEqual(progress.await_args_list[-1].kwargs["stage"], "success")
        source = self.installation_store.get("Author.Plugin")
        self.assertEqual(source.install_method, "git")
        self.assertEqual(source.source_commit, "d" * 40)
        self.assertEqual(source.installed_version, "2.0.0")

        async def clone_invalid_new_version(**kwargs):
            kwargs["target_path"].mkdir(parents=True, exist_ok=True)
            return {"success": True, "path": str(kwargs["target_path"]), "attempts": 1}

        write_manifest(
            plugin_dir,
            {
                "id": "Author.Plugin",
                "manifest_version": 1,
                "name": "Plugin",
                "version": "2.0.0",
                "author": "Author",
            },
        )
        (plugin_dir / "preserve.txt").write_text("keep", encoding="utf-8")
        self.service.clone_repository = AsyncMock(side_effect=clone_invalid_new_version)

        with patch.object(plugin_routes, "update_progress", new=AsyncMock()):
            with self.assertRaises(HTTPException) as invalid_update:
                await plugin_routes.update_plugin(
                    plugin_routes.UpdatePluginRequest(
                        plugin_id="Author.Plugin",
                        repository_url="https://github.com/Author/Plugin",
                    ),
                    **self.auth_kwargs(),
                )
        self.assertEqual(invalid_update.exception.status_code, 400)
        self.assertTrue(plugin_dir.exists())
        self.assertTrue((plugin_dir / "preserve.txt").exists())
        preserved_manifest = json.loads((plugin_dir / "_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(preserved_manifest["version"], "2.0.0")

    async def test_free_repository_update_rejects_market_installation_before_cloning(self) -> None:
        plugin_id = "github.alice.weather"
        plugin_dir = self.plugins_dir / "github_alice_weather"
        write_manifest(plugin_dir, manifest_v2())
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)
        self.installation_store.sources[plugin_id] = InstallationSource(
            plugin_id=plugin_id,
            install_method="market",
            registry_url="https://plugins.riyabot.example/registry.json",
            repository_url="https://github.com/alice/riyabot-weather",
            source_ref="v1.2.3",
            source_commit="a" * 40,
            artifact_sha256=None,
            installed_version="1.2.3",
            installed_at=installed_at,
            updated_at=installed_at,
            last_checked_at=installed_at,
        )
        self.service.clone_repository = AsyncMock()

        with (
            patch.object(plugin_routes, "update_progress", new=AsyncMock()),
            self.assertRaises(HTTPException) as failure,
        ):
            await plugin_routes.update_plugin(
                plugin_routes.UpdatePluginRequest(
                    plugin_id=plugin_id,
                    repository_url="https://github.com/mallory/weather",
                    branch="main",
                ),
                **self.auth_kwargs(),
            )

        self.assertEqual(failure.exception.status_code, 409)
        self.service.clone_repository.assert_not_awaited()

    async def test_update_preserves_old_plugin_when_clone_fails(self) -> None:
        plugin_dir = self.plugins_dir / "Author_Plugin"
        write_manifest(
            plugin_dir,
            {
                "id": "Author.Plugin",
                "manifest_version": 1,
                "name": "Plugin",
                "version": "1.0.0",
                "author": "Author",
            },
        )
        (plugin_dir / "state.db").write_text("important", encoding="utf-8")
        self.service.clone_repository = AsyncMock(
            return_value={"success": False, "error": "remote leaked /private/path", "attempts": 1}
        )

        with patch.object(plugin_routes, "update_progress", new=AsyncMock()) as progress:
            with self.assertRaises(HTTPException) as update_error:
                await plugin_routes.update_plugin(
                    plugin_routes.UpdatePluginRequest(
                        plugin_id="Author.Plugin",
                        repository_url="https://github.com/Author/Plugin",
                    ),
                    **self.auth_kwargs(),
                )

        self.assertEqual(update_error.exception.status_code, 500)
        self.assertEqual(update_error.exception.detail, "插件更新失败")
        self.assertEqual((plugin_dir / "state.db").read_text(encoding="utf-8"), "important")
        self.assertNotIn("/private/path", repr(progress.await_args_list))


class InstalledPluginRoutesTest(PluginRouteBase):
    async def test_installed_plugins_hide_market_source_when_local_manifest_drifted(self) -> None:
        plugin_id = "github.alice.weather"
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)
        self.installation_store.sources[plugin_id] = InstallationSource(
            plugin_id=plugin_id,
            install_method="market",
            registry_url="https://plugins.riyabot.example/registry.json",
            repository_url="https://github.com/alice/riyabot-weather",
            source_ref="v1.2.3",
            source_commit="a" * 40,
            artifact_sha256=None,
            installed_version="1.2.3",
            installed_at=installed_at,
            updated_at=installed_at,
            last_checked_at=installed_at,
        )
        drifted_manifests = {
            "version": manifest_v2(version="9.9.9"),
            "repository": manifest_v2(repository="https://github.com/mallory/riyabot-weather"),
        }

        for drift_kind, manifest in drifted_manifests.items():
            with self.subTest(drift_kind=drift_kind):
                plugin_dir = self.plugins_dir / "github_alice_weather"
                write_manifest(plugin_dir, manifest)

                result = await plugin_routes.get_installed_plugins(**self.auth_kwargs())

                self.assertEqual(result["total"], 1)
                self.assertEqual(result["plugins"][0]["id"], plugin_id)
                self.assertIsNone(result["plugins"][0]["installation"])
                shutil.rmtree(plugin_dir)

    async def test_installed_plugins_include_persisted_source_metadata(self) -> None:
        plugin_id = "Author.Plugin"
        plugin_dir = self.plugins_dir / "Author_Plugin"
        write_manifest(
            plugin_dir,
            {
                "id": plugin_id,
                "manifest_version": 1,
                "name": "Plugin",
                "version": "1.0.0",
                "description": "Plugin",
                "author": "Author",
            },
        )
        installed_at = datetime.datetime(2026, 8, 9, 10, 0, tzinfo=datetime.timezone.utc)
        self.installation_store.sources[plugin_id] = InstallationSource(
            plugin_id=plugin_id,
            install_method="git",
            registry_url=None,
            repository_url="https://github.com/Author/Plugin",
            source_ref="main",
            source_commit="c" * 40,
            artifact_sha256=None,
            installed_version="1.0.0",
            installed_at=installed_at,
            updated_at=installed_at,
            last_checked_at=installed_at,
        )

        result = await plugin_routes.get_installed_plugins(**self.auth_kwargs())

        installation = result["plugins"][0]["installation"]
        self.assertEqual(installation["install_method"], "git")
        self.assertEqual(installation["repository_url"], "https://github.com/Author/Plugin")
        self.assertEqual(installation["source_ref"], "main")
        self.assertEqual(installation["source_commit"], "c" * 40)

    async def test_installed_plugins_scans_manifests_infers_ids_and_deduplicates(self) -> None:
        valid = self.plugins_dir / "Author_Plugin"
        inferred = self.plugins_dir / "LegacyFolder"
        duplicate = self.plugins_dir / "Duplicate"
        invalid = self.plugins_dir / "Invalid"
        no_manifest = self.plugins_dir / "NoManifest"
        hidden = self.plugins_dir / ".hidden"

        write_manifest(
            valid,
            {
                "id": "Author.Plugin",
                "name": "Plugin",
                "version": "1.0.0",
                "author": {"name": "Author"},
            },
        )
        write_manifest(
            inferred,
            {
                "name": "Legacy",
                "version": "2.0.0",
                "author": "LegacyAuthor",
                "repository_url": "https://github.com/LegacyAuthor/LegacyRepo.git",
            },
        )
        write_manifest(
            duplicate,
            {
                "id": "Author.Plugin",
                "name": "Duplicate",
                "version": "1.1.0",
                "author": "Author",
            },
        )
        write_manifest(invalid, {"name": "Invalid"})
        no_manifest.mkdir(parents=True)
        hidden.mkdir(parents=True)

        result = await plugin_routes.get_installed_plugins(**self.auth_kwargs())

        self.assertTrue(result["success"])
        self.assertEqual(result["total"], 2)
        plugin_ids = {plugin["id"] for plugin in result["plugins"]}
        self.assertEqual(plugin_ids, {"Author.Plugin", "LegacyAuthor.LegacyRepo"})

        inferred_manifest = json.loads((inferred / "_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(inferred_manifest["id"], "LegacyAuthor.LegacyRepo")

    async def test_installed_plugins_creates_missing_plugins_directory(self) -> None:
        result = await plugin_routes.get_installed_plugins(**self.auth_kwargs())

        self.assertEqual(result, {"success": True, "plugins": []})
        self.assertTrue(self.plugins_dir.exists())

    async def test_installed_plugins_skips_symlinked_or_oversized_manifests_and_hides_absolute_paths(self) -> None:
        valid = self.plugins_dir / "ValidPlugin"
        write_manifest(
            valid,
            {
                "id": "Author.Valid",
                "name": "Valid",
                "version": "1.0.0",
                "author": "Author",
            },
        )

        with tempfile.TemporaryDirectory() as outside_dir:
            outside_plugin = Path(outside_dir) / "OutsidePlugin"
            write_manifest(
                outside_plugin,
                {
                    "id": "Outside.Plugin",
                    "name": "Outside",
                    "version": "1.0.0",
                    "author": "Outside",
                },
            )
            (self.plugins_dir / "LinkedPlugin").symlink_to(outside_plugin, target_is_directory=True)

            linked_manifest_plugin = self.plugins_dir / "LinkedManifest"
            linked_manifest_plugin.mkdir(parents=True)
            manifest_target = linked_manifest_plugin / "manifest-target.json"
            manifest_target.write_text(
                json.dumps(
                    {
                        "id": "Author.LinkedManifest",
                        "name": "Linked",
                        "version": "1.0.0",
                    }
                ),
                encoding="utf-8",
            )
            (linked_manifest_plugin / "_manifest.json").symlink_to(manifest_target)

            result = await plugin_routes.get_installed_plugins(**self.auth_kwargs())

        self.assertEqual([plugin["id"] for plugin in result["plugins"]], ["Author.Valid"])
        self.assertEqual(result["plugins"][0]["path"], "plugins/ValidPlugin")
        self.assertFalse(Path(result["plugins"][0]["path"]).is_absolute())

        oversized = self.plugins_dir / "Oversized"
        write_manifest(
            oversized,
            {
                "id": "Author.Oversized",
                "name": "Oversized",
                "version": "1.0.0",
            },
        )
        with patch.object(plugin_routes, "MAX_PLUGIN_MANIFEST_BYTES", 8):
            limited_result = await plugin_routes.get_installed_plugins(**self.auth_kwargs())
        self.assertEqual(limited_result["plugins"], [])

    async def test_local_readme_returns_matching_file_or_structured_failure(self) -> None:
        plugin_dir = self.plugins_dir / "Author_Plugin"
        write_manifest(
            plugin_dir,
            {"id": "Author.Plugin", "name": "Plugin", "version": "1.0.0", "author": "Author"},
        )
        (plugin_dir / "readme.md").write_text("# Plugin\n\n说明", encoding="utf-8")

        success = await plugin_routes.get_local_plugin_readme("Author.Plugin", **self.auth_kwargs())
        missing_readme = await plugin_routes.get_local_plugin_readme("Missing.Plugin", **self.auth_kwargs())

        self.assertEqual(success, {"success": True, "data": "# Plugin\n\n说明"})
        self.assertEqual(missing_readme, {"success": False, "error": "插件未安装"})


class PluginConfigRoutesTest(PluginRouteBase):
    def setUp(self) -> None:
        super().setUp()
        self.plugin_dir = self.plugins_dir / "Author_Plugin"
        write_manifest(
            self.plugin_dir,
            {"id": "Author.Plugin", "name": "Plugin", "version": "1.0.0", "author": "Author"},
        )

    def write_config(self, content: str = '[plugin]\nenabled = true\ntags = ["old"]\n') -> None:
        (self.plugin_dir / "config.toml").write_text(content, encoding="utf-8")

    async def test_loaded_schema_is_returned_before_filesystem_fallback(self) -> None:
        loaded_plugin = SimpleNamespace(
            plugin_name="LoadedPlugin",
            get_manifest_info=lambda key, default=None: "Author.Plugin" if key == "id" else default,
            get_webui_config_schema=lambda: {"plugin_id": "Author.Plugin", "sections": {"plugin": {}}},
        )
        fake_manager = SimpleNamespace(
            list_loaded_plugins=lambda: ["LoadedPlugin"],
            get_plugin_instance=lambda _: loaded_plugin,
        )

        with patch("src.plugin_system.core.plugin_manager.plugin_manager", fake_manager):
            result = await plugin_routes.get_plugin_config_schema("Author.Plugin", **self.auth_kwargs())

        self.assertEqual(result["schema"]["plugin_id"], "Author.Plugin")

    async def test_filesystem_schema_raw_and_config_routes_read_current_plugin_files(self) -> None:
        self.write_config(
            """
[plugin]
enabled = true
threshold = 3
names = ["alpha", "beta"]

[[items]]
name = "first"
score = 1
""".strip()
        )
        fake_manager = SimpleNamespace(list_loaded_plugins=lambda: [], get_plugin_instance=lambda _: None)

        with patch("src.plugin_system.core.plugin_manager.plugin_manager", fake_manager):
            schema_result = await plugin_routes.get_plugin_config_schema("Author.Plugin", **self.auth_kwargs())
        raw_result = await plugin_routes.get_plugin_config_raw("Author.Plugin", **self.auth_kwargs())
        config_result = await plugin_routes.get_plugin_config("Author.Plugin", **self.auth_kwargs())

        self.assertTrue(schema_result["success"])
        self.assertEqual(schema_result["schema"]["sections"]["plugin"]["fields"]["enabled"]["ui_type"], "switch")
        self.assertEqual(schema_result["schema"]["sections"]["plugin"]["fields"]["names"]["item_type"], "string")
        self.assertIn("[[items]]", raw_result["config"])
        self.assertTrue(config_result["config"]["plugin"]["enabled"])
        self.assertEqual(config_result["config"]["plugin"]["threshold"], 3)

    async def test_raw_config_update_validates_toml_and_creates_backup(self) -> None:
        self.write_config("[plugin]\nenabled = true\n")

        result = await plugin_routes.update_plugin_config_raw(
            "Author.Plugin",
            plugin_routes.UpdatePluginConfigRequest(config="[plugin]\nenabled = false\n"),
            **self.auth_kwargs(),
        )

        self.assertTrue(result["success"])
        self.assertIn("enabled = false", (self.plugin_dir / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(len(list(self.plugin_dir.glob("config.toml.backup.*"))), 1)

        with self.assertRaises(HTTPException) as not_string:
            await plugin_routes.update_plugin_config_raw(
                "Author.Plugin",
                plugin_routes.UpdatePluginConfigRequest(config={"plugin": {"enabled": True}}),
                **self.auth_kwargs(),
            )
        self.assertEqual(not_string.exception.status_code, 400)

        with self.assertRaises(HTTPException) as bad_toml:
            await plugin_routes.update_plugin_config_raw(
                "Author.Plugin",
                plugin_routes.UpdatePluginConfigRequest(config="[plugin\n"),
                **self.auth_kwargs(),
            )
        self.assertEqual(bad_toml.exception.status_code, 400)

    async def test_config_routes_reject_symlink_escape_and_enforce_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as outside_dir:
            outside_plugin = Path(outside_dir) / "OutsidePlugin"
            write_manifest(
                outside_plugin,
                {"id": "Outside.Plugin", "name": "Outside", "version": "1.0.0", "author": "Outside"},
            )
            outside_config = outside_plugin / "config.toml"
            outside_config.write_text('secret = "outside"\n', encoding="utf-8")
            self.plugins_dir.mkdir(parents=True, exist_ok=True)
            (self.plugins_dir / "LinkedPlugin").symlink_to(outside_plugin, target_is_directory=True)

            with self.assertRaises(HTTPException) as read_escape:
                await plugin_routes.get_plugin_config_raw("Outside.Plugin", **self.auth_kwargs())
            self.assertIn(read_escape.exception.status_code, {400, 404})

            with self.assertRaises(HTTPException) as write_escape:
                await plugin_routes.update_plugin_config_raw(
                    "Outside.Plugin",
                    plugin_routes.UpdatePluginConfigRequest(config='secret = "changed"\n'),
                    **self.auth_kwargs(),
                )
            self.assertIn(write_escape.exception.status_code, {400, 404})
            self.assertEqual(outside_config.read_text(encoding="utf-8"), 'secret = "outside"\n')

        self.write_config("value = 12345\n")
        with patch.object(plugin_routes, "MAX_PLUGIN_CONFIG_BYTES", 4):
            with self.assertRaises(HTTPException) as oversized_read:
                await plugin_routes.get_plugin_config_raw("Author.Plugin", **self.auth_kwargs())
            self.assertEqual(oversized_read.exception.status_code, 413)

            with self.assertRaises(HTTPException) as oversized_write:
                await plugin_routes.update_plugin_config_raw(
                    "Author.Plugin",
                    plugin_routes.UpdatePluginConfigRequest(config="value = 12345\n"),
                    **self.auth_kwargs(),
                )
            self.assertEqual(oversized_write.exception.status_code, 413)

    async def test_raw_config_rejects_invalid_utf8_and_sanitizes_toml_errors(self) -> None:
        (self.plugin_dir / "config.toml").write_bytes(b"\xff")

        with self.assertRaises(HTTPException) as invalid_encoding:
            await plugin_routes.get_plugin_config_raw("Author.Plugin", **self.auth_kwargs())
        self.assertEqual(invalid_encoding.exception.status_code, 400)

        secret_content = 'api_key = "super-secret"\n[broken'
        with self.assertRaises(HTTPException) as invalid_toml:
            await plugin_routes.update_plugin_config_raw(
                "Author.Plugin",
                plugin_routes.UpdatePluginConfigRequest(config=secret_content),
                **self.auth_kwargs(),
            )
        self.assertEqual(invalid_toml.exception.status_code, 400)
        self.assertNotIn("super-secret", str(invalid_toml.exception.detail))

    async def test_structured_config_update_normalizes_webui_payload_and_coerces_schema_types(self) -> None:
        self.write_config('[plugin]\nenabled = true\ntags = ["old"]\n')
        plugin_instance = SimpleNamespace(
            config_schema={
                "plugin": {
                    "tags": ConfigField(type=list, default=[], description="标签"),
                }
            }
        )

        with patch.object(plugin_routes, "find_plugin_instance", return_value=plugin_instance):
            result = await plugin_routes.update_plugin_config(
                "Author.Plugin",
                plugin_routes.UpdatePluginConfigRequest(
                    config={"plugin.enabled": False, "plugin.tags": "alpha, beta", "extra.value": 7}
                ),
                **self.auth_kwargs(),
            )

        self.assertTrue(result["success"])
        saved = tomlkit.loads((self.plugin_dir / "config.toml").read_text(encoding="utf-8"))
        self.assertFalse(saved["plugin"]["enabled"])
        self.assertEqual(list(saved["plugin"]["tags"]), ["alpha", "beta"])
        self.assertEqual(saved["extra"]["value"], 7)
        self.assertEqual(len(list(self.plugin_dir.glob("config.toml.backup.*"))), 1)

    async def test_structured_config_routes_reject_symlinked_plugin_and_config_files(self) -> None:
        with tempfile.TemporaryDirectory() as outside_dir:
            outside_root = Path(outside_dir)
            outside_plugin = outside_root / "OutsidePlugin"
            write_manifest(
                outside_plugin,
                {
                    "id": "Outside.Plugin",
                    "name": "Outside",
                    "version": "1.0.0",
                    "author": "Outside",
                },
            )
            outside_config = outside_plugin / "config.toml"
            outside_config.write_text('[plugin]\nenabled = true\nsecret = "outside"\n', encoding="utf-8")
            (self.plugins_dir / "LinkedPlugin").symlink_to(outside_plugin, target_is_directory=True)

            calls = [
                lambda: plugin_routes.get_plugin_config("Outside.Plugin", **self.auth_kwargs()),
                lambda: plugin_routes.update_plugin_config(
                    "Outside.Plugin",
                    plugin_routes.UpdatePluginConfigRequest(config={"plugin": {"enabled": False}}),
                    **self.auth_kwargs(),
                ),
                lambda: plugin_routes.reset_plugin_config("Outside.Plugin", **self.auth_kwargs()),
                lambda: plugin_routes.toggle_plugin("Outside.Plugin", **self.auth_kwargs()),
            ]
            for call in calls:
                with self.subTest(call=call):
                    with self.assertRaises(HTTPException) as escaped:
                        await call()
                    self.assertIn(escaped.exception.status_code, {400, 404})

            self.assertEqual(
                outside_config.read_text(encoding="utf-8"),
                '[plugin]\nenabled = true\nsecret = "outside"\n',
            )

            linked_config_target = outside_root / "linked-config.toml"
            linked_config_target.write_text('[plugin]\nenabled = true\nsecret = "linked"\n', encoding="utf-8")
            (self.plugin_dir / "config.toml").symlink_to(linked_config_target)

            linked_calls = [
                lambda: plugin_routes.get_plugin_config("Author.Plugin", **self.auth_kwargs()),
                lambda: plugin_routes.update_plugin_config(
                    "Author.Plugin",
                    plugin_routes.UpdatePluginConfigRequest(config={"plugin": {"enabled": False}}),
                    **self.auth_kwargs(),
                ),
                lambda: plugin_routes.reset_plugin_config("Author.Plugin", **self.auth_kwargs()),
                lambda: plugin_routes.toggle_plugin("Author.Plugin", **self.auth_kwargs()),
            ]
            for call in linked_calls:
                with self.subTest(call=call):
                    with self.assertRaises(HTTPException) as linked:
                        await call()
                    self.assertEqual(linked.exception.status_code, 400)

            fake_manager = SimpleNamespace(list_loaded_plugins=lambda: [], get_plugin_instance=lambda _: None)
            with patch("src.plugin_system.core.plugin_manager.plugin_manager", fake_manager):
                with self.assertRaises(HTTPException) as linked_schema:
                    await plugin_routes.get_plugin_config_schema("Author.Plugin", **self.auth_kwargs())
            self.assertEqual(linked_schema.exception.status_code, 400)

            self.assertEqual(
                linked_config_target.read_text(encoding="utf-8"),
                '[plugin]\nenabled = true\nsecret = "linked"\n',
            )

    async def test_structured_config_routes_enforce_size_encoding_and_sanitized_toml_errors(self) -> None:
        self.write_config("value = 12345\n")
        with patch.object(plugin_routes, "MAX_PLUGIN_CONFIG_BYTES", 4):
            for call in [
                lambda: plugin_routes.get_plugin_config("Author.Plugin", **self.auth_kwargs()),
                lambda: plugin_routes.toggle_plugin("Author.Plugin", **self.auth_kwargs()),
            ]:
                with self.subTest(call=call):
                    with self.assertRaises(HTTPException) as oversized:
                        await call()
                    self.assertEqual(oversized.exception.status_code, 413)

        original_config = "value = 1\n"
        self.write_config(original_config)
        with patch.object(plugin_routes, "MAX_PLUGIN_CONFIG_BYTES", 32):
            with self.assertRaises(HTTPException) as oversized_output:
                await plugin_routes.update_plugin_config(
                    "Author.Plugin",
                    plugin_routes.UpdatePluginConfigRequest(config={"value": "x" * 64}),
                    **self.auth_kwargs(),
                )
        self.assertEqual(oversized_output.exception.status_code, 413)
        self.assertEqual((self.plugin_dir / "config.toml").read_text(encoding="utf-8"), original_config)

        (self.plugin_dir / "config.toml").write_bytes(b"\xff")
        with self.assertRaises(HTTPException) as invalid_encoding:
            await plugin_routes.get_plugin_config("Author.Plugin", **self.auth_kwargs())
        self.assertEqual(invalid_encoding.exception.status_code, 400)

        secret_config = 'api_key = "super-secret"\n[broken'
        self.write_config(secret_config)
        for call in [
            lambda: plugin_routes.get_plugin_config("Author.Plugin", **self.auth_kwargs()),
            lambda: plugin_routes.toggle_plugin("Author.Plugin", **self.auth_kwargs()),
            lambda: plugin_routes.update_plugin_config(
                "Author.Plugin",
                plugin_routes.UpdatePluginConfigRequest(config={"plugin": {"enabled": False}}),
                **self.auth_kwargs(),
            ),
        ]:
            with self.subTest(call=call):
                with self.assertRaises(HTTPException) as invalid_toml:
                    await call()
                self.assertEqual(invalid_toml.exception.status_code, 400)
                self.assertNotIn("super-secret", str(invalid_toml.exception.detail))

    async def test_config_backups_are_bounded_and_failed_atomic_replace_preserves_original(self) -> None:
        self.write_config("value = 0\n")
        with patch.object(plugin_routes, "MAX_PLUGIN_CONFIG_BACKUPS", 2):
            for value in range(1, 4):
                await plugin_routes.update_plugin_config_raw(
                    "Author.Plugin",
                    plugin_routes.UpdatePluginConfigRequest(config=f"value = {value}\n"),
                    **self.auth_kwargs(),
                )

        backups = sorted(self.plugin_dir.glob("config.toml.backup.*"))
        self.assertEqual(len(backups), 2)
        self.assertEqual(
            {backup.read_text(encoding="utf-8") for backup in backups},
            {"value = 1\n", "value = 2\n"},
        )

        original_config = (self.plugin_dir / "config.toml").read_text(encoding="utf-8")
        real_replace = os.replace

        def fail_config_replace(source: str | Path, destination: str | Path) -> None:
            if Path(destination).name == "config.toml":
                raise OSError("replace failed")
            real_replace(source, destination)

        with patch.object(plugin_routes.os, "replace", side_effect=fail_config_replace):
            with self.assertRaises(HTTPException) as failed_write:
                await plugin_routes.update_plugin_config_raw(
                    "Author.Plugin",
                    plugin_routes.UpdatePluginConfigRequest(config="value = 4\n"),
                    **self.auth_kwargs(),
                )
        self.assertEqual(failed_write.exception.status_code, 500)
        self.assertNotIn("replace failed", str(failed_write.exception.detail))
        self.assertEqual((self.plugin_dir / "config.toml").read_text(encoding="utf-8"), original_config)

    async def test_reset_backup_matches_the_config_that_was_atomically_removed(self) -> None:
        self.write_config("value = 1\n")
        original_read = plugin_routes._read_limited_bytes
        replaced_during_read = False

        def replace_after_read(path: Path, max_bytes: int, label: str) -> bytes:
            nonlocal replaced_during_read
            content = original_read(path, max_bytes, label)
            if path.name == "config.toml" and not replaced_during_read:
                path.write_text("value = 2\n", encoding="utf-8")
                replaced_during_read = True
            return content

        with patch.object(plugin_routes, "_read_limited_bytes", side_effect=replace_after_read):
            result = await plugin_routes.reset_plugin_config("Author.Plugin", **self.auth_kwargs())

        self.assertTrue(replaced_during_read)
        self.assertFalse((self.plugin_dir / "config.toml").exists())
        self.assertEqual((self.plugin_dir / result["backup"]).read_text(encoding="utf-8"), "value = 2\n")

    async def test_reset_and_toggle_plugin_config_preserve_structured_responses(self) -> None:
        self.write_config("[plugin]\nenabled = true\n")

        toggled = await plugin_routes.toggle_plugin("Author.Plugin", **self.auth_kwargs())
        self.assertEqual(toggled["enabled"], False)
        self.assertFalse(
            tomlkit.loads((self.plugin_dir / "config.toml").read_text(encoding="utf-8"))["plugin"]["enabled"]
        )

        reset = await plugin_routes.reset_plugin_config("Author.Plugin", **self.auth_kwargs())
        self.assertTrue(reset["success"])
        self.assertEqual(reset["backup"], Path(reset["backup"]).name)
        self.assertFalse((self.plugin_dir / "config.toml").exists())
        self.assertEqual(len(list(self.plugin_dir.glob("config.toml.reset.*"))), 1)

        second_reset = await plugin_routes.reset_plugin_config("Author.Plugin", **self.auth_kwargs())
        self.assertEqual(second_reset["message"], "配置文件不存在，无需重置")

        toggled_missing_config = await plugin_routes.toggle_plugin("Author.Plugin", **self.auth_kwargs())
        self.assertFalse(toggled_missing_config["enabled"])
        self.assertFalse(
            tomlkit.loads((self.plugin_dir / "config.toml").read_text(encoding="utf-8"))["plugin"]["enabled"]
        )

    async def test_config_routes_raise_404_for_missing_plugin(self) -> None:
        with self.assertRaises(HTTPException) as raw_error:
            await plugin_routes.get_plugin_config_raw("Missing.Plugin", **self.auth_kwargs())
        self.assertEqual(raw_error.exception.status_code, 404)

        with self.assertRaises(HTTPException) as update_error:
            await plugin_routes.update_plugin_config(
                "Missing.Plugin",
                plugin_routes.UpdatePluginConfigRequest(config={}),
                **self.auth_kwargs(),
            )
        self.assertEqual(update_error.exception.status_code, 404)
