import datetime
import unittest
from dataclasses import replace

from peewee import SqliteDatabase

from src.common.database.database_model import BaseModel, PluginInstallation
from src.plugin_system.core.installation_store import (
    InstallationSource,
    PluginInstallationStore,
)


class PluginInstallationStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.database = SqliteDatabase(":memory:")
        self.original_databases = {model: model._meta.database for model in (BaseModel, PluginInstallation)}
        self.database.bind([PluginInstallation], bind_refs=False, bind_backrefs=False)
        self.database.connect()
        self.database.create_tables([PluginInstallation])
        self.store = PluginInstallationStore()

    def tearDown(self) -> None:
        self.database.drop_tables([PluginInstallation])
        self.database.close()
        for model, database in self.original_databases.items():
            model._meta.set_database(database)

    def test_save_market_source_persists_reviewed_registry_coordinates(self) -> None:
        installed_at = datetime.datetime(2026, 8, 9, 10, 30, tzinfo=datetime.timezone.utc)
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

        saved = self.store.save(source)
        loaded = self.store.get("github.alice.weather")

        self.assertEqual(saved, source)
        self.assertEqual(loaded, source)
        self.assertEqual(PluginInstallation.select().count(), 1)

    def test_save_rejects_incomplete_market_source(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        source = InstallationSource(
            plugin_id="github.alice.weather",
            install_method="market",
            registry_url=None,
            repository_url="https://github.com/alice/riyabot-weather",
            source_ref="v1.2.3",
            source_commit="a" * 40,
            artifact_sha256=None,
            installed_version="1.2.3",
            installed_at=now,
            updated_at=now,
            last_checked_at=None,
        )

        with self.assertRaisesRegex(ValueError, "市场安装来源不完整"):
            self.store.save(source)

        self.assertEqual(PluginInstallation.select().count(), 0)

    def test_save_rejects_invalid_market_source_coordinates(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        source = InstallationSource(
            plugin_id="github.alice.weather",
            install_method="market",
            registry_url="https://plugins.riyabot.example/registry.json",
            repository_url="https://github.com/alice/riyabot-weather",
            source_ref="v1.2.3",
            source_commit="a" * 40,
            artifact_sha256=None,
            installed_version="1.2.3",
            installed_at=now,
            updated_at=now,
            last_checked_at=now,
        )
        invalid_sources = {
            "registry": replace(source, registry_url="http://127.0.0.1/registry.json"),
            "repository": replace(source, repository_url="https://user:secret@github.com/alice/plugin"),
            "ref": replace(source, source_ref="../main"),
            "commit": replace(source, source_commit="A" * 40),
            "version": replace(source, installed_version="1.2"),
            "sha256": replace(source, artifact_sha256="f" * 63),
        }

        for field_name, invalid_source in invalid_sources.items():
            with self.subTest(field=field_name):
                with self.assertRaisesRegex(ValueError, "市场安装来源无效"):
                    self.store.save(invalid_source)

        self.assertEqual(PluginInstallation.select().count(), 0)

    def test_save_keeps_legacy_git_identity_and_version_compatible(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        source = InstallationSource(
            plugin_id="Author.Plugin",
            install_method="git",
            registry_url=None,
            repository_url="https://github.com/alice/legacy-plugin",
            source_ref="main",
            source_commit="a" * 40,
            artifact_sha256=None,
            installed_version="2026.08",
            installed_at=now,
            updated_at=now,
            last_checked_at=now,
        )

        self.assertEqual(self.store.save(source), source)

    def test_save_rejects_registry_binding_for_non_market_source(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        source = InstallationSource(
            plugin_id="legacy.plugin",
            install_method="git",
            registry_url="https://plugins.riyabot.example/registry.json",
            repository_url="https://github.com/alice/legacy-plugin",
            source_ref="main",
            source_commit=None,
            artifact_sha256=None,
            installed_version="0.1.0",
            installed_at=now,
            updated_at=now,
            last_checked_at=None,
        )

        with self.assertRaisesRegex(ValueError, "非市场安装不能绑定 Registry"):
            self.store.save(source)

    def test_save_update_preserves_original_install_time(self) -> None:
        installed_at = datetime.datetime(2026, 8, 9, 10, 30, tzinfo=datetime.timezone.utc)
        updated_at = datetime.datetime(2026, 8, 10, 8, 0, tzinfo=datetime.timezone.utc)
        original = InstallationSource(
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
        replacement = InstallationSource(
            plugin_id="github.alice.weather",
            install_method="market",
            registry_url="https://plugins.riyabot.example/registry.json",
            repository_url="https://github.com/alice/riyabot-weather",
            source_ref="v1.3.0",
            source_commit="b" * 40,
            artifact_sha256="c" * 64,
            installed_version="1.3.0",
            installed_at=updated_at,
            updated_at=updated_at,
            last_checked_at=updated_at,
        )

        self.store.save(original)
        saved = self.store.save(replacement)

        self.assertEqual(saved.installed_at, installed_at)
        self.assertEqual(saved.updated_at, updated_at)
        self.assertEqual(saved.installed_version, "1.3.0")
        self.assertEqual(PluginInstallation.select().count(), 1)

    def test_save_does_not_partially_update_a_record_with_an_invalid_install_time(self) -> None:
        aware_now = datetime.datetime.now(datetime.timezone.utc)
        PluginInstallation.create(
            plugin_id="local.corrupt-existing",
            install_method="local",
            registry_url=None,
            repository_url=None,
            source_ref=None,
            source_commit=None,
            artifact_sha256=None,
            installed_version="0.1.0",
            installed_at=datetime.datetime(2026, 8, 9, 10, 30),
            updated_at=aware_now,
            last_checked_at=None,
        )
        replacement = InstallationSource(
            plugin_id="local.corrupt-existing",
            install_method="local",
            registry_url=None,
            repository_url=None,
            source_ref=None,
            source_commit=None,
            artifact_sha256=None,
            installed_version="0.2.0",
            installed_at=aware_now,
            updated_at=aware_now,
            last_checked_at=None,
        )

        with self.assertRaisesRegex(ValueError, "插件安装时间无效"):
            self.store.save(replacement)

        record = PluginInstallation.get_by_id("local.corrupt-existing")
        self.assertEqual(record.installed_version, "0.1.0")

    def test_delete_is_idempotent(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        self.store.save(
            InstallationSource(
                plugin_id="local.plugin",
                install_method="local",
                registry_url=None,
                repository_url=None,
                source_ref=None,
                source_commit=None,
                artifact_sha256=None,
                installed_version="0.1.0",
                installed_at=now,
                updated_at=now,
                last_checked_at=None,
            )
        )

        self.assertTrue(self.store.delete("local.plugin"))
        self.assertFalse(self.store.delete("local.plugin"))
        self.assertIsNone(self.store.get("local.plugin"))

    def test_list_all_returns_sources_keyed_by_plugin_id(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        for plugin_id in ("local.alpha", "local.beta"):
            self.store.save(
                InstallationSource(
                    plugin_id=plugin_id,
                    install_method="local",
                    registry_url=None,
                    repository_url=None,
                    source_ref=None,
                    source_commit=None,
                    artifact_sha256=None,
                    installed_version="0.1.0",
                    installed_at=now,
                    updated_at=now,
                    last_checked_at=None,
                )
            )

        sources = self.store.list_all()

        self.assertEqual(set(sources), {"local.alpha", "local.beta"})
        self.assertEqual(sources["local.alpha"].install_method, "local")

    def test_get_returns_none_for_invalid_install_method(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        PluginInstallation.create(
            plugin_id="corrupt.method",
            install_method="archive",
            registry_url=None,
            repository_url=None,
            source_ref=None,
            source_commit=None,
            artifact_sha256=None,
            installed_version="0.1.0",
            installed_at=now,
            updated_at=now,
            last_checked_at=None,
        )

        self.assertIsNone(self.store.get("corrupt.method"))

    def test_get_returns_none_for_incomplete_market_source(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        PluginInstallation.create(
            plugin_id="github.alice.corrupt",
            install_method="market",
            registry_url="https://plugins.riyabot.example/registry.json",
            repository_url=None,
            source_ref="v1.0.0",
            source_commit="a" * 40,
            artifact_sha256=None,
            installed_version="1.0.0",
            installed_at=now,
            updated_at=now,
            last_checked_at=None,
        )

        self.assertIsNone(self.store.get("github.alice.corrupt"))

    def test_get_returns_none_for_malformed_timestamp(self) -> None:
        PluginInstallation.create(
            plugin_id="corrupt.timestamp",
            install_method="local",
            registry_url=None,
            repository_url=None,
            source_ref=None,
            source_commit=None,
            artifact_sha256=None,
            installed_version="0.1.0",
            installed_at="not-a-timestamp",
            updated_at="also-not-a-timestamp",
            last_checked_at=None,
        )

        self.assertIsNone(self.store.get("corrupt.timestamp"))

    def test_get_returns_none_for_non_datetime_or_naive_timestamps(self) -> None:
        aware_now = datetime.datetime.now(datetime.timezone.utc)
        invalid_timestamps = {
            "wrong-type": 123,
            "missing-timezone": datetime.datetime(2026, 8, 9, 10, 30),
        }

        for suffix, installed_at in invalid_timestamps.items():
            with self.subTest(timestamp=suffix):
                plugin_id = f"corrupt.timestamp.{suffix}"
                PluginInstallation.create(
                    plugin_id=plugin_id,
                    install_method="local",
                    registry_url=None,
                    repository_url=None,
                    source_ref=None,
                    source_commit=None,
                    artifact_sha256=None,
                    installed_version="0.1.0",
                    installed_at=installed_at,
                    updated_at=aware_now,
                    last_checked_at=None,
                )

                self.assertIsNone(self.store.get(plugin_id))

    def test_save_rejects_timestamps_without_datetime_type_and_timezone(self) -> None:
        aware_now = datetime.datetime.now(datetime.timezone.utc)
        source = InstallationSource(
            plugin_id="local.invalid-time",
            install_method="local",
            registry_url=None,
            repository_url=None,
            source_ref=None,
            source_commit=None,
            artifact_sha256=None,
            installed_version="0.1.0",
            installed_at=aware_now,
            updated_at=aware_now,
            last_checked_at=None,
        )

        invalid_sources = (
            replace(source, installed_at="2026-08-09T10:30:00+00:00"),
            replace(source, updated_at=datetime.datetime(2026, 8, 9, 10, 30)),
        )
        for invalid_source in invalid_sources:
            with self.subTest(source=invalid_source):
                with self.assertRaisesRegex(ValueError, "插件安装时间无效"):
                    self.store.save(invalid_source)

        self.assertEqual(PluginInstallation.select().count(), 0)

    def test_list_all_skips_invalid_records_without_hiding_valid_sources(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        self.store.save(
            InstallationSource(
                plugin_id="local.valid",
                install_method="local",
                registry_url=None,
                repository_url=None,
                source_ref=None,
                source_commit=None,
                artifact_sha256=None,
                installed_version="0.1.0",
                installed_at=now,
                updated_at=now,
                last_checked_at=None,
            )
        )
        PluginInstallation.create(
            plugin_id="corrupt.method",
            install_method="archive",
            registry_url=None,
            repository_url=None,
            source_ref=None,
            source_commit=None,
            artifact_sha256=None,
            installed_version="0.1.0",
            installed_at=now,
            updated_at=now,
            last_checked_at=None,
        )
        PluginInstallation.create(
            plugin_id="github.alice.corrupt",
            install_method="market",
            registry_url=None,
            repository_url="https://github.com/alice/riyabot-corrupt",
            source_ref="v1.0.0",
            source_commit="a" * 40,
            artifact_sha256=None,
            installed_version="1.0.0",
            installed_at=now,
            updated_at=now,
            last_checked_at=None,
        )
        PluginInstallation.create(
            plugin_id="corrupt.timestamp",
            install_method="local",
            registry_url=None,
            repository_url=None,
            source_ref=None,
            source_commit=None,
            artifact_sha256=None,
            installed_version="0.1.0",
            installed_at="not-a-timestamp",
            updated_at=now,
            last_checked_at=None,
        )

        sources = self.store.list_all()

        self.assertEqual(set(sources), {"local.valid"})


if __name__ == "__main__":
    unittest.main()
