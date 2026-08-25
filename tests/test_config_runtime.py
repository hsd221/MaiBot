import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tomlkit

from src.config import config as config_module
from src.config.api_ada_configs import APIProvider, ModelInfo, ModelTaskConfig, TaskConfig


def make_model_task_config(model_list: list[str] | None = None) -> ModelTaskConfig:
    task = TaskConfig(model_list=model_list or [])
    return ModelTaskConfig(
        utils=task,
        replyer=task,
        vlm=TaskConfig(),
        voice=TaskConfig(),
        tool_use=task,
        planner=task,
        embedding=TaskConfig(),
    )


class ConfigRuntimeHelpersTest(unittest.TestCase):
    def test_python_defaults_render_loadable_bot_and_model_configs(self) -> None:
        bot_text = config_module.generate_default_bot_config()
        model_text = config_module.generate_default_model_config()

        bot_document = tomlkit.parse(bot_text)
        model_document = tomlkit.parse(model_text)

        self.assertEqual(bot_document["inner"]["version"], config_module.BOT_CONFIG_VERSION)
        self.assertEqual(model_document["inner"]["version"], config_module.MODEL_CONFIG_VERSION)
        self.assertEqual(bot_document["bot"]["platform"], "qq")
        self.assertEqual(bot_document["bot"]["nickname"], "璃夜")
        self.assertEqual(bot_document["chat"]["max_context_size"], 30)
        self.assertEqual(bot_document["update"]["channel"], "stable")
        self.assertEqual(
            bot_document["webui"]["plugin_registry_url"],
            "https://raw.githubusercontent.com/hsd221/RiyaBot-Plugins-Registry/main/registry.json",
        )
        self.assertNotIn("dream", bot_document)
        self.assertEqual(model_document["api_providers"], [])
        self.assertEqual(model_document["models"], [])
        self.assertEqual(model_document["model_task_config"]["utils"]["model_list"], [])
        self.assertIn("# 平台", bot_text)
        self.assertIn("# 模型列表\nmodels = []", model_text)
        self.assertIn("# API提供商列表\napi_providers = []", model_text)

        loaded_bot = config_module.Config.from_dict(bot_document)
        loaded_model = config_module.APIAdapterConfig.from_dict(model_document)
        self.assertEqual(loaded_bot.bot.nickname, "璃夜")
        self.assertFalse(loaded_model.is_runtime_ready())

    def test_python_defaults_preserve_existing_template_values(self) -> None:
        bot_text = config_module.generate_default_bot_config()
        bot_document = tomlkit.parse(bot_text)
        model_document = tomlkit.parse(config_module.generate_default_model_config())

        expected_bot_values = {
            ("bot", "platforms"): ["wx:114514", "xx:1919810"],
            ("bot", "alias_names"): ["Riya", "小璃"],
            ("chat", "max_context_size"): 30,
            ("memory", "agent_timeout_seconds"): 180.0,
            ("memory", "qdrant_api_key"): "",
            ("tool", "enable_tool"): True,
            ("emoji", "emoji_chance"): 0.4,
            ("emoji", "max_reg_num"): 100,
            ("emoji", "check_interval"): 10,
            ("emoji", "vector_selection_enabled"): True,
            ("emoji", "usage_scene_enabled"): True,
            ("emoji", "usage_scene_context_messages"): 8,
            ("emoji", "usage_scene_max_scenes"): 8,
            ("emoji", "usage_scene_weight"): 0.6,
            ("emoji", "selection_candidate_count"): 8,
            ("expression", "expression_checked_only"): True,
            ("expression", "vector_selection_enabled"): True,
            ("expression", "expression_self_reflect"): True,
            ("expression", "expression_auto_check_interval"): 600,
            ("expression", "expression_auto_check_count"): 20,
            ("expression", "all_global_jargon"): True,
            ("expression", "jargon_mode"): "planner",
            ("lpmm_knowledge", "qa_relation_threshold"): 0.5,
            ("lpmm_knowledge", "qa_res_top_k"): 3,
            ("response_splitter", "max_length"): 512,
            ("response_splitter", "max_sentence_num"): 8,
            ("debug", "show_replyer_prompt"): False,
            ("debug", "show_replyer_reasoning"): False,
            ("webui", "anti_crawler_mode"): "loose",
            ("update", "channel"): "stable",
        }
        for path, expected in expected_bot_values.items():
            with self.subTest(path=path):
                self.assertEqual(bot_document[path[0]][path[1]], expected)

        self.assertEqual(len(bot_document["expression"]["learning_list"]), 3)
        self.assertEqual(len(bot_document["behavior"]["learning_list"]), 3)
        self.assertEqual(len(bot_document["keyword_reaction"]["keyword_rules"]), 2)
        self.assertEqual(len(bot_document["keyword_reaction"]["regex_rules"]), 2)
        self.assertEqual(bot_document["log"]["library_log_levels"], {"aiohttp": "WARNING"})
        self.assertIn("faiss", bot_document["log"]["suppress_libraries"])
        self.assertEqual(set(bot_document["personality"]), {"personality", "reply_style"})
        self.assertNotIn("private_plan_style", bot_document["experimental"])
        self.assertNotIn("dream", bot_document)
        self.assertNotIn("plan_style", bot_text)
        self.assertIn(
            "# 每个聊天流最大保存的Plan/Reply日志数量，超过此数量时会自动删除最老的日志\nplan_reply_log_max_per_chat",
            bot_text,
        )
        self.assertIn("# 关键词规则列表\nkeyword_rules = [", bot_text)

        expected_tasks = {
            "utils": (4096, 0.2, 15.0),
            "tool_use": (1024, 0.7, 10.0),
            "replyer": (2048, 0.3, 25.0),
            "planner": (800, 0.3, 12.0),
            "vlm": (256, 0.3, 15.0),
            "voice": (1024, 0.3, 12.0),
            "embedding": (1024, 0.3, 5.0),
            "memory_encoder": (800, 0.2, 20.0),
            "memory_weaver": (800, 0.2, 20.0),
        }
        for task_name, (max_tokens, temperature, slow_threshold) in expected_tasks.items():
            with self.subTest(task_name=task_name):
                task = model_document["model_task_config"][task_name]
                self.assertEqual(task["model_list"], [])
                self.assertEqual(task["max_tokens"], max_tokens)
                self.assertEqual(task["temperature"], temperature)
                self.assertEqual(task["slow_threshold"], slow_threshold)
                self.assertEqual(task["selection_strategy"], "random")

    def test_created_config_files_returns_copy_and_webui_setup_marker_handles_missing_or_invalid_files(self) -> None:
        original_created = list(config_module._CREATED_CONFIG_FILES)
        try:
            config_module._CREATED_CONFIG_FILES[:] = ["bot_config.toml"]
            returned = config_module.get_created_config_files()
            returned.append("model_config.toml")
            self.assertEqual(config_module.get_created_config_files(), ["bot_config.toml"])

            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                data_dir = root / "data"
                data_dir.mkdir()
                webui_path = data_dir / "webui.json"
                webui_path.write_text(
                    json.dumps({"first_setup_completed": True, "setup_completed_at": "old"}),
                    encoding="utf-8",
                )
                with patch.object(config_module, "PROJECT_ROOT", str(root)):
                    config_module._mark_webui_setup_required("created")
                marked = json.loads(webui_path.read_text(encoding="utf-8"))
                self.assertFalse(marked["first_setup_completed"])
                self.assertEqual(marked["setup_required_reason"], "created")
                self.assertNotIn("setup_completed_at", marked)
                self.assertEqual(webui_path.stat().st_mode & 0o777, 0o600)

                webui_path.write_text("{bad json", encoding="utf-8")
                with patch.object(config_module, "PROJECT_ROOT", str(root)):
                    config_module._mark_webui_setup_required("ignored")

                webui_path.unlink()
                with patch.object(config_module, "PROJECT_ROOT", str(root)):
                    config_module._mark_webui_setup_required("missing")

                if hasattr(os, "symlink"):
                    external_path = root / "external-webui.json"
                    original_external = json.dumps({"first_setup_completed": True})
                    external_path.write_text(original_external, encoding="utf-8")
                    webui_path.symlink_to(external_path)
                    with patch.object(config_module, "PROJECT_ROOT", str(root)):
                        config_module._mark_webui_setup_required("must-not-follow")
                    self.assertTrue(webui_path.is_symlink())
                    self.assertEqual(external_path.read_text(encoding="utf-8"), original_external)
        finally:
            config_module._CREATED_CONFIG_FILES[:] = original_created

    def test_toml_diff_path_and_update_helpers_preserve_versions_and_fallback_values(self) -> None:
        new = tomlkit.parse(
            """
[inner]
version = "2"
keep = 2 # new keep
added = true # added field

[nested]
value = 2
""".strip()
        )
        old = tomlkit.parse(
            """
[inner]
version = "1"
keep = 1 # old keep
removed = "x" # removed field

[nested]
value = 1
""".strip()
        )

        logs = config_module.compare_dicts(new, old)
        self.assertTrue(any("新增: inner.added" in line for line in logs))
        self.assertTrue(any("删减: inner.removed" in line for line in logs))
        self.assertEqual(config_module.get_value_by_path(old, ["nested", "value"]), 1)
        self.assertIsNone(config_module.get_value_by_path(old, ["missing"]))

        target = {}
        config_module.set_value_by_path(target, ["a", "b"], 1)
        self.assertEqual(target["a"]["b"], 1)
        with patch.object(config_module.tomlkit, "item", side_effect=TypeError("bad")):
            config_module.set_value_by_path(target, ["a", "fallback"], object())
        self.assertIn("fallback", target["a"])

        config_module._update_dict(old, {"inner": {"version": "skip", "keep": 3}, "nested": {"value": 4}})
        self.assertEqual(old["inner"]["version"], "1")
        self.assertEqual(old["inner"]["keep"], 3)
        self.assertEqual(old["nested"]["value"], 4)
        plain_target = {"inner": {"keep": "old"}}
        fallback_value = object()
        with patch.object(config_module.tomlkit, "item", side_effect=TypeError("bad")):
            config_module._update_dict(plain_target, {"inner": {"keep": fallback_value}})
        self.assertIs(plain_target["inner"]["keep"], fallback_value)

    def test_get_key_comment_reads_table_value_and_keytype_comments(self) -> None:
        doc = tomlkit.parse(
            """
[section] # table comment
value = 1 # value comment
""".strip()
        )

        self.assertEqual(config_module.get_key_comment(doc["section"], "ignored"), "# table comment")

        class FakeItem:
            trivia = type("Trivia", (), {"comment": "# value comment"})()

        class FakeValueContainer:
            value = {"value": FakeItem()}

        self.assertEqual(config_module.get_key_comment(FakeValueContainer(), "value"), "# value comment")

        class FakeKey:
            key = "target"
            trivia = type("Trivia", (), {"comment": "# key comment"})()

        class FakeKeysContainer:
            def keys(self):
                return [FakeKey()]

        with patch.object(config_module, "KeyType", FakeKey):
            self.assertEqual(config_module.get_key_comment(FakeKeysContainer(), "target"), "# key comment")
        self.assertIsNone(config_module.get_key_comment({}, "missing"))

    def test_version_helpers_read_files_documents_and_semver(self) -> None:
        self.assertEqual(config_module._version_tuple(None), (0,))
        self.assertEqual(config_module._version_tuple("v1.2.x-dev"), (1, 2, 0))
        self.assertEqual(
            config_module._get_version_from_document({"inner": {"version": "2.3.4"}}),
            "2.3.4",
        )
        self.assertIsNone(config_module._get_version_from_document({}))

    def test_update_config_generic_creates_missing_config_from_python_defaults_and_marks_webui(self) -> None:
        original_created = list(config_module._CREATED_CONFIG_FILES)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                config_dir = root / "config"

                with (
                    patch.object(config_module, "CONFIG_DIR", str(config_dir)),
                    patch.object(config_module, "PROJECT_ROOT", str(root)),
                    patch.object(config_module, "_mark_webui_setup_required") as mark_setup,
                ):
                    config_module._CREATED_CONFIG_FILES.clear()
                    config_module._update_config_generic("bot_config", config_module.generate_default_bot_config)

                config_path = config_dir / "bot_config.toml"
                self.assertTrue(config_path.exists())
                self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(config_dir.stat().st_mode & 0o022, 0)
                self.assertEqual(
                    tomlkit.parse(config_path.read_text(encoding="utf-8"))["inner"]["version"],
                    config_module.BOT_CONFIG_VERSION,
                )
                self.assertIn("bot_config.toml", config_module.get_created_config_files())
                mark_setup.assert_called_once_with("bot_config.toml 已根据 Python 默认配置创建")
        finally:
            config_module._CREATED_CONFIG_FILES[:] = original_created

    def test_update_config_generic_applies_explicit_default_migrations_and_keeps_custom_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "bot_config.toml").write_text(
                '[inner]\nversion = "1"\n\n[feature]\nfollow_default = 1\ncustom = 9\nold_key = false\n',
                encoding="utf-8",
            )

            def generated() -> str:
                return '[inner]\nversion = "2"\n\n[feature]\nfollow_default = 2\ncustom = 2\nnew_key = true\n'

            migrations = {
                "bot_config": (
                    config_module.DefaultValueMigration("2", ("feature", "follow_default"), 1, 2),
                    config_module.DefaultValueMigration("2", ("feature", "custom"), 1, 2),
                )
            }

            with (
                patch.object(config_module, "CONFIG_DIR", str(config_dir)),
                patch.object(config_module, "DEFAULT_VALUE_MIGRATIONS", migrations),
            ):
                config_module._update_config_generic("bot_config", generated)

            migrated = tomlkit.parse((config_dir / "bot_config.toml").read_text(encoding="utf-8"))
            self.assertEqual(migrated["inner"]["version"], "2")
            self.assertEqual(migrated["feature"]["follow_default"], 2)
            self.assertEqual(migrated["feature"]["custom"], 9)
            self.assertTrue(migrated["feature"]["new_key"])
            backups = list((config_dir / "old").glob("bot_config_*.toml"))
            self.assertTrue(backups)
            backed_up = tomlkit.parse(backups[0].read_text(encoding="utf-8"))
            self.assertEqual(backed_up["feature"]["follow_default"], 1)
            self.assertEqual((config_dir / "bot_config.toml").stat().st_mode & 0o777, 0o600)
            self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in backups))
            self.assertEqual((config_dir / "old").stat().st_mode & 0o022, 0)

    def test_default_value_migrations_run_in_version_order_and_only_in_upgrade_range(self) -> None:
        document = tomlkit.parse("[feature]\nvalue = 1\n")
        migrations = {
            "bot_config": (
                config_module.DefaultValueMigration("5.0.0", ("feature", "value"), 3, 5),
                config_module.DefaultValueMigration("3.0.0", ("feature", "value"), 2, 3),
                config_module.DefaultValueMigration("1.0.0", ("feature", "value"), 0, 1),
                config_module.DefaultValueMigration("2.0.0", ("feature", "value"), 1, 2),
            )
        }

        with patch.object(config_module, "DEFAULT_VALUE_MIGRATIONS", migrations):
            config_module._apply_default_value_migrations("bot_config", document, "1.0.0", "4.0.0")

        self.assertEqual(document["feature"]["value"], 3)

    def test_update_config_generic_keeps_original_and_backup_when_atomic_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            config_path = config_dir / "bot_config.toml"
            original = '[inner]\nversion = "1"\n\n[feature]\nvalue = "keep"\n'
            config_path.write_text(original, encoding="utf-8")

            with (
                patch.object(config_module, "CONFIG_DIR", str(config_dir)),
                patch.object(config_module, "_atomic_write_text", side_effect=OSError("write failed")),
            ):
                with self.assertRaisesRegex(OSError, "write failed"):
                    config_module._update_config_generic(
                        "bot_config",
                        lambda: '[inner]\nversion = "2"\n\n[feature]\nvalue = "new"\n',
                    )

            self.assertEqual(config_path.read_text(encoding="utf-8"), original)
            backups = list((config_dir / "old").glob("bot_config_*.toml"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), original)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(hasattr(os, "symlink"), "platform does not support symlinks")
    def test_update_config_generic_rejects_symbolic_link_config_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside_dir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            external_path = Path(outside_dir) / "external.toml"
            original_external = '[inner]\nversion = "1"\nsecret = "do-not-touch"\n'
            external_path.write_text(original_external, encoding="utf-8")
            linked_config = config_dir / "bot_config.toml"
            linked_config.symlink_to(external_path)

            with patch.object(config_module, "CONFIG_DIR", str(config_dir)):
                with self.assertRaises(RuntimeError):
                    config_module._update_config_generic("bot_config", lambda: '[inner]\nversion = "2"\n')

            self.assertTrue(linked_config.is_symlink())
            self.assertEqual(external_path.read_text(encoding="utf-8"), original_external)

    @unittest.skipUnless(hasattr(os, "link"), "platform does not support hard links")
    def test_update_config_generic_rejects_hard_link_config_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside_dir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            external_path = Path(outside_dir) / "external.toml"
            original_external = '[inner]\nversion = "1"\nsecret = "do-not-touch"\n'
            external_path.write_text(original_external, encoding="utf-8")
            linked_config = config_dir / "bot_config.toml"
            os.link(external_path, linked_config)

            with patch.object(config_module, "CONFIG_DIR", str(config_dir)):
                with self.assertRaises(RuntimeError):
                    config_module._update_config_generic("bot_config", lambda: '[inner]\nversion = "2"\n')

            self.assertEqual(external_path.read_text(encoding="utf-8"), original_external)
            self.assertEqual(linked_config.stat().st_nlink, 2)

    def test_update_config_generic_skips_same_version_without_rewriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            config_path = config_dir / "model_config.toml"
            original = 'models = [{name = "keep"}]\n[inner]\nversion = "1"\n'
            config_path.write_text(original, encoding="utf-8")

            with patch.object(config_module, "CONFIG_DIR", str(config_dir)):
                config_module._update_config_generic(
                    "model_config",
                    lambda: 'models = []\n[inner]\nversion = "1"\n',
                )

            self.assertEqual(config_path.read_text(encoding="utf-8"), original)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list((config_dir / "old").glob("*.toml")), [])

    def test_update_config_generic_does_not_downgrade_newer_configs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            config_path = config_dir / "bot_config.toml"
            original = '[inner]\nversion = "3"\n\n[future]\nvalue = "keep"\n'
            config_path.write_text(original, encoding="utf-8")

            with patch.object(config_module, "CONFIG_DIR", str(config_dir)):
                config_module._update_config_generic(
                    "bot_config",
                    lambda: '[inner]\nversion = "2"\n',
                )

            self.assertEqual(config_path.read_text(encoding="utf-8"), original)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list((config_dir / "old").glob("*.toml")), [])

    def test_update_config_generic_updates_configs_without_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            (config_dir / "bot_config.toml").write_text("value = 1\n", encoding="utf-8")

            with patch.object(config_module, "CONFIG_DIR", str(config_dir)):
                config_module._update_config_generic(
                    "bot_config",
                    lambda: 'value = 2\n[inner]\nversion = "2"\n',
                )

            migrated = tomlkit.parse((config_dir / "bot_config.toml").read_text(encoding="utf-8"))
            self.assertEqual(migrated["inner"]["version"], "2")
            self.assertEqual(migrated["value"], 1)
            self.assertEqual(len(list((config_dir / "old").glob("bot_config_*.toml"))), 1)

    def test_model_upgrade_preserves_existing_providers_models_and_task_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            config_path = config_dir / "model_config.toml"
            config_path.write_text(
                """
[[api_providers]]
name = "provider-a"
base_url = "https://api.example.test"
api_key = "secret"

[[models]]
model_identifier = "model-id"
name = "model-a"
api_provider = "provider-a"

[inner]
version = "1.11.0"

[model_task_config.utils]
model_list = ["model-a"]
[model_task_config.replyer]
model_list = ["model-a"]
[model_task_config.vlm]
model_list = []
[model_task_config.voice]
model_list = []
[model_task_config.tool_use]
model_list = ["model-a"]
[model_task_config.planner]
model_list = ["model-a"]
[model_task_config.embedding]
model_list = []
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with patch.object(config_module, "CONFIG_DIR", str(config_dir)):
                config_module._update_config_generic("model_config", config_module.generate_default_model_config)

            migrated = tomlkit.parse(config_path.read_text(encoding="utf-8"))
            loaded = config_module.APIAdapterConfig.from_dict(migrated)
            self.assertEqual(migrated["inner"]["version"], config_module.MODEL_CONFIG_VERSION)
            self.assertEqual([provider.name for provider in loaded.api_providers], ["provider-a"])
            self.assertEqual([model.name for model in loaded.models], ["model-a"])
            self.assertEqual(loaded.model_task_config.replyer.model_list, ["model-a"])

    def test_update_config_generic_logs_no_structural_changes_when_versions_differ_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()
            (config_dir / "bot_config.toml").write_text(
                '[inner]\nversion = "1"\nvalue = 1\n',
                encoding="utf-8",
            )

            with (
                patch.object(config_module, "CONFIG_DIR", str(config_dir)),
                patch.object(config_module.logger, "info") as logger_info,
            ):
                config_module._update_config_generic(
                    "bot_config",
                    lambda: '[inner]\nversion = "2"\nvalue = 1\n',
                )

            self.assertTrue(any(call.args == ("无新增或删减项",) for call in logger_info.call_args_list))

    def test_update_config_wrappers_delegate_to_generic_updater(self) -> None:
        with patch.object(config_module, "_update_config_generic") as updater:
            config_module.update_config()
            config_module.update_model_config()

        updater.assert_any_call("bot_config", config_module.generate_default_bot_config)
        updater.assert_any_call("model_config", config_module.generate_default_model_config)


class APIAdapterConfigRuntimeTest(unittest.TestCase):
    def make_config(self, *, model_names=None, task_models=None, providers=None) -> config_module.APIAdapterConfig:
        model_names = model_names if model_names is not None else ["model-a"]
        providers = (
            providers if providers is not None else [APIProvider("provider-a", "https://api.example.test", "secret")]
        )
        models = [
            ModelInfo(model_identifier=f"id-{name}", name=name, api_provider=providers[0].name) for name in model_names
        ]
        return config_module.APIAdapterConfig(
            models=models,
            model_task_config=make_model_task_config(task_models if task_models is not None else model_names),
            api_providers=providers,
        )

    def test_api_adapter_config_validates_duplicates_unknowns_readiness_and_lookup_errors(self) -> None:
        config = self.make_config()

        self.assertTrue(config.is_runtime_ready())
        self.assertIsNone(config.get_runtime_readiness_error())
        self.assertEqual(config.get_unknown_task_models(), {})
        self.assertEqual(config.get_missing_runtime_tasks(), [])
        self.assertEqual(config.get_model_info("model-a").name, "model-a")
        self.assertEqual(config.get_provider("provider-a").name, "provider-a")

        with self.assertRaisesRegex(ValueError, "模型列表不能为空"):
            config_module.APIAdapterConfig(
                models=[], model_task_config=make_model_task_config(), api_providers=[]
            ).validate_integrity(require_complete=True)
        with self.assertRaisesRegex(ValueError, "API提供商列表不能为空"):
            config_module.APIAdapterConfig(
                models=[ModelInfo("id-a", "model-a", "provider-a")],
                model_task_config=make_model_task_config(["model-a"]),
                api_providers=[],
            ).validate_integrity(require_complete=True)
        with self.assertRaisesRegex(ValueError, "API提供商名称存在重复"):
            self.make_config(providers=[APIProvider("p", "https://a", "k"), APIProvider("p", "https://b", "k")])
        with self.assertRaisesRegex(ValueError, "模型名称存在重复"):
            self.make_config(model_names=["dup", "dup"])

        unknown = self.make_config(task_models=["missing"])
        self.assertEqual(unknown.get_unknown_task_models()["utils"], ["missing"])
        self.assertFalse(unknown.is_runtime_ready())
        self.assertIn("引用了不存在的模型", unknown.get_runtime_readiness_error())

        missing_provider = self.make_config()
        missing_provider.models[0].api_provider = "missing-provider"
        with self.assertRaisesRegex(ValueError, "api_provider 'missing-provider' 不存在"):
            missing_provider.validate_integrity(require_complete=True)

        missing_identifier = self.make_config()
        missing_identifier.models[0].model_identifier = ""
        with self.assertRaisesRegex(ValueError, "model_identifier 不能为空"):
            missing_identifier.validate_integrity()

        lazy = self.make_config()
        delattr(lazy, "models_dict")
        self.assertEqual(lazy.get_unknown_task_models(), {})

        class NotATaskConfig:
            pass

        lazy.model_task_config.utils = NotATaskConfig()
        self.assertEqual(lazy.get_missing_runtime_tasks()[0], "utils")
        self.assertEqual(lazy.get_unknown_task_models(), {})

        missing_tasks = config_module.APIAdapterConfig(
            models=[ModelInfo("id-a", "model-a", "provider-a")],
            model_task_config=make_model_task_config([]),
            api_providers=[APIProvider("provider-a", "https://api.example.test", "secret")],
        )
        self.assertIn("utils", missing_tasks.get_missing_runtime_tasks())
        self.assertFalse(missing_tasks.is_runtime_ready())

        with self.assertRaisesRegex(ValueError, "模型名称不能为空"):
            config.get_model_info("")
        with self.assertRaisesRegex(KeyError, "不存在"):
            config.get_model_info("missing")
        with self.assertRaisesRegex(ValueError, "API提供商名称不能为空"):
            config.get_provider("")
        with self.assertRaisesRegex(KeyError, "不存在"):
            config.get_provider("missing")

    def test_load_config_helpers_return_converted_objects_or_reraise_parse_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bot_config = root / "bot_config.toml"
            bot_config.write_text('[bot]\nplatform = "qq"\n', encoding="utf-8")
            model_config = root / "model_config.toml"
            model_config.write_text(
                """
[[api_providers]]
name = "provider-a"
base_url = "https://api.example.test"
api_key = "secret"

[[models]]
model_identifier = "id-a"
name = "model-a"
api_provider = "provider-a"

[model_task_config.utils]
model_list = ["model-a"]

[model_task_config.replyer]
model_list = ["model-a"]

[model_task_config.vlm]
model_list = []

[model_task_config.voice]
model_list = []

[model_task_config.tool_use]
model_list = ["model-a"]

[model_task_config.planner]
model_list = ["model-a"]

[model_task_config.embedding]
model_list = []
""".strip(),
                encoding="utf-8",
            )

            self.assertIsInstance(config_module.api_ada_load_config(str(model_config)), config_module.APIAdapterConfig)
            invalid_model_config = root / "invalid_model_config.toml"
            invalid_model_config.write_text('[[models]]\nname = "missing required fields"\n', encoding="utf-8")
            with self.assertRaises(RuntimeError):
                config_module.api_ada_load_config(str(invalid_model_config))
            with self.assertRaisesRegex(RuntimeError, "Missing required field: 'qq_account'"):
                config_module.load_config(str(bot_config))


if __name__ == "__main__":
    unittest.main()
