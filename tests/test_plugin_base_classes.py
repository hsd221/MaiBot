import importlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import toml

from src.plugin_system.base import base_action, base_command
from src.plugin_system.base.base_action import BaseAction
from src.plugin_system.base.base_command import BaseCommand
from src.plugin_system.base.base_plugin import BasePlugin
from src.plugin_system.base.base_tool import BaseTool
from src.plugin_system.base.component_types import (
    ActionActivationType,
    ActionInfo,
    ComponentType,
    ToolInfo,
    ToolParamType,
)
from src.plugin_system.base.config_types import ConfigField, ConfigLayout, ConfigSection, ConfigTab
from src.plugin_system.base.plugin_base import PluginBase


def make_chat_stream(stream_id: str = "stream-1", platform: str = "qq"):
    return SimpleNamespace(stream_id=stream_id, platform=platform)


def make_action_message(*, group: bool = True):
    group_info = SimpleNamespace(group_id="group-1", group_name="Group") if group else None
    return SimpleNamespace(
        chat_info=SimpleNamespace(group_info=group_info),
        user_info=SimpleNamespace(user_id="user-1", user_nickname="Alice"),
    )


class ConcreteAction(BaseAction):
    action_name = "reply"
    action_description = "Reply action"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["hello"]
    action_parameters = {"text": "str"}
    action_require = ["text"]
    associated_types = ["text"]
    random_activation_probability = 0.2

    async def execute(self):
        return True, "done"


class ConcreteCommand(BaseCommand):
    command_name = "greet"
    command_description = "Greet command"
    command_pattern = r"^/greet (?P<name>\w+)$"

    async def execute(self):
        return True, "ok", 1


class ConcreteTool(BaseTool):
    name = "lookup"
    description = "Lookup data"
    parameters = [
        ("query", ToolParamType.STRING, "Query", True, None),
        ("limit", ToolParamType.INTEGER, "Limit", False, None),
    ]
    available_for_llm = True

    async def execute(self, function_args):
        return {"called_with": function_args}


class ConcreteConfigPlugin(PluginBase):
    config_section_descriptions = {
        "plugin": ConfigSection("插件设置", description="插件启用状态", icon="settings", collapsed=True, order=1),
        "settings": "行为设置",
    }
    config_layout = ConfigLayout(
        type="tabs", tabs=[ConfigTab(id="basic", title="基础", sections=["plugin", "settings"])]
    )

    def __init__(self, plugin_dir: str, dependencies: list[str] | None = None):
        self._enable_plugin = True
        self._dependencies = dependencies or []
        super().__init__(plugin_dir)

    @property
    def plugin_name(self) -> str:
        return "sample_plugin"

    @property
    def enable_plugin(self) -> bool:
        return self._enable_plugin

    @enable_plugin.setter
    def enable_plugin(self, value: bool) -> None:
        self._enable_plugin = value

    @property
    def dependencies(self):
        return self._dependencies

    @property
    def python_dependencies(self):
        return []

    @property
    def config_file_name(self) -> str:
        return "config.toml"

    @property
    def config_schema(self):
        return {
            "plugin": {
                "config_version": ConfigField(type=str, default="2.0.0", description="配置版本"),
                "enabled": ConfigField(type=bool, default=True, description="是否启用"),
            },
            "settings": {
                "threshold": ConfigField(type=float, default=0.5, description="阈值", min=0, max=1),
                "mode": ConfigField(type=str, default="auto", description="模式", choices=["auto", "manual"]),
                "nested": ConfigField(type=dict, default={"x": 1}, description="嵌套值"),
            },
        }

    def register_plugin(self) -> bool:
        return True


class NoConfigPlugin(PluginBase):
    config_layout = None

    def __init__(self, plugin_dir: str, config_file_name: str = "config.toml"):
        self._enable_plugin = True
        self._config_file_name = config_file_name
        super().__init__(plugin_dir)

    @property
    def plugin_name(self) -> str:
        return "no_config_plugin"

    @property
    def enable_plugin(self) -> bool:
        return self._enable_plugin

    @enable_plugin.setter
    def enable_plugin(self, value: bool) -> None:
        self._enable_plugin = value

    @property
    def dependencies(self):
        return []

    @property
    def python_dependencies(self):
        return []

    @property
    def config_file_name(self) -> str:
        return self._config_file_name

    @property
    def config_schema(self):
        return {}

    def register_plugin(self) -> bool:
        return True


class SchemaEdgePlugin(ConcreteConfigPlugin):
    config_layout = None
    config_section_descriptions = {
        "plugin": {"title": "插件字典元数据", "collapsed": True, "order": 9},
    }

    @property
    def config_schema(self):
        return {
            "plugin": {
                "config_version": ConfigField(
                    type=str,
                    default="3.0.0",
                    description="配置版本",
                    required=True,
                    example="3.0.0",
                ),
                "mode": ConfigField(
                    type=str,
                    default="auto",
                    description="模式",
                    choices=["auto", "manual"],
                ),
            },
            "ignored": "not-a-field-section",
        }


class JsonConfigPlugin(ConcreteConfigPlugin):
    @property
    def config_file_name(self) -> str:
        return "config.json"


class ConcreteCompositePlugin(BasePlugin):
    def __init__(self, plugin_dir: str, components=None, dependencies: list[str] | None = None):
        self._enable_plugin = True
        self._components = components or []
        self._dependencies = dependencies or []
        super().__init__(plugin_dir)

    @property
    def plugin_name(self) -> str:
        return "composite_plugin"

    @property
    def enable_plugin(self) -> bool:
        return self._enable_plugin

    @enable_plugin.setter
    def enable_plugin(self, value: bool) -> None:
        self._enable_plugin = value

    @property
    def dependencies(self):
        return self._dependencies

    @property
    def python_dependencies(self):
        return []

    @property
    def config_file_name(self) -> str:
        return ""

    @property
    def config_schema(self):
        return {}

    def get_plugin_components(self):
        return self._components


def write_manifest(plugin_dir: Path, **overrides) -> dict:
    manifest = {
        "manifest_version": 1,
        "name": "Sample Plugin",
        "version": "1.2.3",
        "description": "Sample plugin for unit tests",
        "author": {"name": "Mai", "url": "https://example.com"},
        "license": "MIT",
        "keywords": ["sample"],
        "categories": ["utility"],
        "homepage_url": "https://example.com/plugin",
        "repository_url": "https://example.com/repo",
        "host_application": {"min_version": "0.1.0"},
    }
    manifest.update(overrides)
    (plugin_dir / "_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


class PluginBaseActionTest(unittest.IsolatedAsyncioTestCase):
    def make_action(self, *, group: bool = True) -> ConcreteAction:
        return ConcreteAction(
            action_data={"loop_start_time": 100.0, "payload": "value"},
            action_reasoning="because",
            cycle_timers={},
            thinking_id="thinking-1",
            chat_stream=make_chat_stream(),
            plugin_config={"section": {"value": 3}},
            action_message=make_action_message(group=group),
        )

    def test_action_context_config_and_info_generation(self) -> None:
        action = self.make_action(group=True)
        private_action = self.make_action(group=False)
        info = ConcreteAction.get_action_info()

        self.assertTrue(action.is_group)
        self.assertEqual(action.target_id, "group-1")
        self.assertEqual(action.log_prefix, "[Group]")
        self.assertFalse(private_action.is_group)
        self.assertEqual(private_action.target_id, "user-1")
        self.assertEqual(private_action.get_config("section.value"), 3)
        self.assertEqual(private_action.get_config("missing", default="fallback"), "fallback")
        self.assertEqual(info.name, "reply")
        self.assertEqual(info.component_type, ComponentType.ACTION)
        self.assertEqual(info.activation_type, ActionActivationType.KEYWORD)
        self.assertEqual(info.activation_keywords, ["hello"])
        self.assertEqual(info.action_parameters, {"text": "str"})
        self.assertEqual(info.action_require, ["text"])
        self.assertEqual(info.associated_types, ["text"])

        class BadAction(ConcreteAction):
            action_name = "bad.name"

        with self.assertRaisesRegex(ValueError, "包含非法字符"):
            BadAction.get_action_info()

    async def test_action_send_wrappers_delegate_and_missing_chat_id_short_circuits(self) -> None:
        action = self.make_action()

        with (
            patch.object(base_action.send_api, "text_to_stream", new=AsyncMock(return_value=True)) as text_to_stream,
            patch.object(
                base_action.send_api, "command_to_stream", new=AsyncMock(return_value=True)
            ) as command_to_stream,
            patch.object(
                base_action.send_api, "custom_to_stream", new=AsyncMock(return_value=True)
            ) as custom_to_stream,
            patch.object(
                base_action.send_api, "custom_reply_set_to_stream", new=AsyncMock(return_value=True)
            ) as reply_set_to_stream,
            patch.object(base_action.database_api, "store_action_info", new=AsyncMock()) as store_action_info,
            patch.object(base_action.message_api, "count_new_messages", return_value=1) as count_new_messages,
            patch.object(base_action.time, "time", return_value=101.0),
        ):
            self.assertTrue(await action.send_text("hello", typing=True, storage_message=False))
            self.assertTrue(await action.send_command("ping", args={"x": 1}, display_message="/ping"))
            self.assertTrue(
                await action.send_custom(
                    "notice",
                    {"payload": 1},
                    typing=True,
                    set_reply=True,
                    storage_message=False,
                )
            )
            self.assertTrue(await action.send_hybrid([(base_action.ReplyContentType.TEXT, "hello")], typing=True))
            self.assertFalse(await action.send_voice(""))
            self.assertTrue(await action.send_voice("voice64"))
            await action.store_action_info(
                action_build_into_prompt=True, action_prompt_display="display", action_done=False
            )
            self.assertEqual(await action.wait_for_new_message(timeout=1), (True, ""))

        text_to_stream.assert_awaited_once_with(
            text="hello",
            stream_id="stream-1",
            set_reply=False,
            reply_message=None,
            typing=True,
            storage_message=False,
        )
        command_to_stream.assert_awaited_once_with(
            command={"name": "ping", "args": {"x": 1}},
            stream_id="stream-1",
            storage_message=True,
            display_message="/ping",
        )
        custom_to_stream.assert_awaited_once()
        self.assertEqual(reply_set_to_stream.await_count, 2)
        store_action_info.assert_awaited_once()
        count_new_messages.assert_called_once_with(chat_id="stream-1", start_time=100.0, end_time=101.0)

        action.chat_id = ""
        with patch.object(base_action.send_api, "text_to_stream", new=AsyncMock(return_value=True)) as text_to_stream:
            self.assertFalse(await action.send_text("hello"))
        text_to_stream.assert_not_awaited()


class PluginBaseCommandTest(unittest.IsolatedAsyncioTestCase):
    def make_command(self, stream_id: str | None = "stream-1") -> ConcreteCommand:
        chat_stream = make_chat_stream(stream_id) if stream_id else None
        message = SimpleNamespace(chat_stream=chat_stream)
        return ConcreteCommand(message=message, plugin_config={"section": {"value": "ok"}})

    def test_command_groups_config_and_info_generation(self) -> None:
        command = self.make_command()
        command.set_matched_groups({"name": "Alice"})
        info = ConcreteCommand.get_command_info()

        self.assertEqual(command.matched_groups, {"name": "Alice"})
        self.assertEqual(command.get_config("section.value"), "ok")
        self.assertEqual(command.get_config("missing", default="fallback"), "fallback")
        self.assertEqual(info.name, "greet")
        self.assertEqual(info.component_type, ComponentType.COMMAND)
        self.assertEqual(info.command_pattern, r"^/greet (?P<name>\w+)$")

        class BadCommand(ConcreteCommand):
            command_name = "bad.name"

        with self.assertRaisesRegex(ValueError, "包含非法字符"):
            BadCommand.get_command_info()

    async def test_command_send_wrappers_delegate_and_missing_stream_returns_false(self) -> None:
        command = self.make_command()

        with (
            patch.object(base_command.send_api, "text_to_stream", new=AsyncMock(return_value=True)) as text_to_stream,
            patch.object(base_command.send_api, "image_to_stream", new=AsyncMock(return_value=True)) as image_to_stream,
            patch.object(
                base_command.send_api, "custom_to_stream", new=AsyncMock(return_value=True)
            ) as custom_to_stream,
            patch.object(
                base_command.send_api, "command_to_stream", new=AsyncMock(return_value=True)
            ) as command_to_stream,
            patch.object(
                base_command.send_api, "custom_reply_set_to_stream", new=AsyncMock(return_value=True)
            ) as reply_set_to_stream,
        ):
            self.assertTrue(await command.send_text("hello", storage_message=False))
            self.assertTrue(await command.send_image("image64", set_reply=True))
            self.assertTrue(await command.send_voice("voice64"))
            self.assertTrue(await command.send_custom("notice", {"payload": 1}, display_message="notice"))
            self.assertTrue(await command.send_command("ping", args={"x": 1}, display_message="/ping"))
            self.assertTrue(await command.send_hybrid([(base_command.ReplyContentType.TEXT, "hello")]))

        text_to_stream.assert_awaited_once_with(
            text="hello",
            stream_id="stream-1",
            set_reply=False,
            reply_message=None,
            storage_message=False,
        )
        image_to_stream.assert_awaited_once_with(
            "image64",
            "stream-1",
            set_reply=True,
            reply_message=None,
            storage_message=True,
        )
        self.assertEqual(custom_to_stream.await_count, 2)
        command_to_stream.assert_awaited_once()
        reply_set_to_stream.assert_awaited_once()

        missing_stream_command = self.make_command(stream_id=None)
        self.assertFalse(await missing_stream_command.send_text("hello"))
        self.assertFalse(await missing_stream_command.send_command("ping"))


class PluginBaseToolTest(unittest.IsolatedAsyncioTestCase):
    async def test_tool_definition_info_config_context_and_direct_execute_validation(self) -> None:
        chat_stream = make_chat_stream()
        tool = ConcreteTool(plugin_config={"section": {"value": 3}}, chat_stream=chat_stream)

        self.assertEqual(
            ConcreteTool.get_tool_definition(),
            {"name": "lookup", "description": "Lookup data", "parameters": ConcreteTool.parameters},
        )
        info = ConcreteTool.get_tool_info()
        self.assertEqual(info.name, "lookup")
        self.assertEqual(info.component_type, ComponentType.TOOL)
        self.assertTrue(info.enabled)
        self.assertEqual(info.tool_parameters, ConcreteTool.parameters)
        self.assertEqual(tool.chat_id, "stream-1")
        self.assertEqual(tool.platform, "qq")
        self.assertEqual(tool.get_config("section.value"), 3)
        self.assertEqual(tool.get_config("missing", default="fallback"), "fallback")

        empty_config_tool = ConcreteTool()
        self.assertIsNone(empty_config_tool.chat_id)
        self.assertIsNone(empty_config_tool.platform)
        self.assertEqual(empty_config_tool.get_config("missing", default="fallback"), "fallback")

        with self.assertRaisesRegex(ValueError, "缺少必要参数"):
            await tool.direct_execute(limit=2)
        self.assertEqual(
            await tool.direct_execute(query="hello", limit=2), {"called_with": {"query": "hello", "limit": 2}}
        )
        with self.assertRaises(NotImplementedError):
            await BaseTool.execute(tool, {})

        class BadTool(BaseTool):
            async def execute(self, function_args):
                return {}

        with self.assertRaises(NotImplementedError):
            BadTool.get_tool_definition()
        with self.assertRaises(NotImplementedError):
            BadTool.get_tool_info()


class PluginBaseConfigTest(unittest.TestCase):
    def test_abstract_property_and_register_method_default_bodies_are_explicit(self) -> None:
        self.assertEqual(PluginBase.plugin_name.fget(object()), "")
        self.assertTrue(PluginBase.enable_plugin.fget(object()))
        self.assertEqual(PluginBase.dependencies.fget(object()), [])
        self.assertEqual(PluginBase.python_dependencies.fget(object()), [])
        self.assertEqual(PluginBase.config_file_name.fget(object()), "")
        self.assertEqual(PluginBase.config_schema.fget(object()), {})

        with self.assertRaises(NotImplementedError):
            PluginBase.register_plugin(object())

    def test_plugin_base_loads_manifest_generates_default_config_and_webui_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            manifest = write_manifest(plugin_dir)

            plugin = ConcreteConfigPlugin(str(plugin_dir))

            config_path = plugin_dir / "config.toml"
            config_data = toml.load(config_path)
            webui_schema = plugin.get_webui_config_schema()
            current_values = plugin.get_current_config_values()

        self.assertEqual(plugin.display_name, manifest["name"])
        self.assertEqual(plugin.plugin_version, "1.2.3")
        self.assertEqual(plugin.plugin_author, "Mai")
        self.assertEqual(plugin.plugin_info.license, "MIT")
        self.assertEqual(plugin.get_manifest_info("author.name"), "Mai")
        self.assertEqual(plugin.get_manifest_info("missing", default="fallback"), "fallback")
        self.assertEqual(config_data["plugin"]["config_version"], "2.0.0")
        self.assertTrue(config_data["plugin"]["enabled"])
        self.assertEqual(config_data["settings"]["threshold"], 0.5)
        self.assertEqual(config_data["settings"]["nested"], {"x": 1})
        self.assertEqual(plugin.get_config("settings.mode"), "auto")
        self.assertEqual(plugin.get_config("settings.missing", default="fallback"), "fallback")
        self.assertIsNot(current_values, plugin.config)
        self.assertEqual(webui_schema["plugin_id"], "sample_plugin")
        self.assertEqual(webui_schema["plugin_info"]["name"], "Sample Plugin")
        self.assertEqual(webui_schema["sections"]["plugin"]["title"], "插件设置")
        self.assertEqual(webui_schema["sections"]["plugin"]["icon"], "settings")
        self.assertEqual(webui_schema["sections"]["settings"]["title"], "行为设置")
        self.assertEqual(webui_schema["sections"]["settings"]["fields"]["mode"]["ui_type"], "select")
        self.assertEqual(webui_schema["layout"]["type"], "tabs")

    def test_plugin_base_exposes_manifest_v2_urls_in_runtime_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            manifest = {
                "manifest_version": 2,
                "id": "github.alice.sample-plugin",
                "name": "Sample Plugin",
                "version": "1.2.3",
                "description": "Sample plugin for unit tests",
                "author": {"name": "Alice", "url": "https://example.com/alice"},
                "license": "MIT",
                "urls": {
                    "repository": "https://example.com/alice/sample-plugin",
                    "homepage": "https://example.com/sample-plugin",
                },
                "host_application": {"min_version": "0.14.0"},
                "sdk": {"min_version": "1.0.0", "max_version": "1.99.99"},
                "entrypoint": "plugin.py",
                "capabilities": [],
                "i18n": {"default_locale": "zh-CN", "supported_locales": ["zh-CN"]},
            }
            (plugin_dir / "_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            plugin = ConcreteConfigPlugin(str(plugin_dir))

        self.assertEqual(plugin.plugin_info.homepage_url, "https://example.com/sample-plugin")
        self.assertEqual(
            plugin.plugin_info.repository_url,
            "https://example.com/alice/sample-plugin",
        )

    def test_plugin_base_handles_no_schema_unsupported_config_and_schema_edges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            manifest = write_manifest(
                plugin_dir,
                name="No Config",
                description="No config plugin",
                author="String Author",
                keywords=[],
                categories=[],
            )
            no_config_plugin = NoConfigPlugin(str(plugin_dir), config_file_name="config.toml")
            missing_config_path = plugin_dir / "config.toml"

        self.assertEqual(no_config_plugin.plugin_author, "String Author")
        self.assertEqual(no_config_plugin.get_manifest_info("missing", default="fallback"), "fallback")
        self.assertEqual(no_config_plugin.get_manifest_info("description"), manifest["description"])
        self.assertEqual(no_config_plugin.config, {})
        self.assertFalse(missing_config_path.exists())
        self.assertEqual(no_config_plugin._generate_config_from_schema(), {})
        self.assertEqual(no_config_plugin._get_expected_config_version(), "1.0.0")

        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            write_manifest(plugin_dir)
            (plugin_dir / "config.json").write_text("{}", encoding="utf-8")
            json_plugin = JsonConfigPlugin(str(plugin_dir))

        self.assertEqual(json_plugin.config, {})

        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            write_manifest(plugin_dir)
            schema_plugin = SchemaEdgePlugin(str(plugin_dir))
            schema = schema_plugin.get_webui_config_schema()

        self.assertEqual(schema["sections"]["plugin"]["title"], "插件字典元数据")
        self.assertTrue(schema["sections"]["plugin"]["collapsed"])
        self.assertNotIn("ignored", schema["sections"])
        self.assertEqual(schema["layout"], {"type": "auto", "tabs": []})

    def test_plugin_base_formats_toml_and_logs_save_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            write_manifest(plugin_dir)
            plugin = SchemaEdgePlugin(str(plugin_dir))
            config_path = plugin_dir / "generated.toml"
            no_schema_path = plugin_dir / "no-schema.toml"

            self.assertEqual(plugin._format_toml_value("中文"), '"中文"')
            self.assertEqual(plugin._format_toml_value(True), "true")
            self.assertEqual(plugin._format_toml_value(3), "3")
            self.assertEqual(plugin._format_toml_value(["a", 2]), '["a", 2]')
            self.assertEqual(plugin._format_toml_value({"key": ["x"]}), '{ key = ["x"] }')
            self.assertEqual(plugin._format_toml_value(None), "null")

            plugin._generate_and_save_default_config(str(config_path))
            generated = config_path.read_text(encoding="utf-8")
            self.assertIn("(必需)", generated)
            self.assertIn("# 示例: 3.0.0", generated)
            self.assertIn("# 可选值: auto, manual", generated)

            no_config_plugin = NoConfigPlugin(str(plugin_dir), config_file_name="")
            no_config_plugin._generate_and_save_default_config(str(no_schema_path))
            no_config_plugin._save_config_to_file({}, str(no_schema_path))

            with patch("builtins.open", side_effect=OSError("boom")):
                plugin._generate_and_save_default_config(str(config_path))
                plugin._save_config_to_file(plugin.config, str(config_path))

    def test_plugin_base_migration_helpers_cover_structure_changes_and_backup_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            write_manifest(plugin_dir)
            plugin = ConcreteConfigPlugin(str(plugin_dir))
            current_path = plugin_dir / "config.toml"

            migrated = plugin._migrate_config_values(
                {
                    "plugin": {"config_version": "1.0.0", "enabled": False, "removed": True},
                    "settings": {"nested": {"x": 9, "removed": "old"}, "old_key": "ignored"},
                    "obsolete": {"value": 1},
                },
                {
                    "plugin": {"config_version": "2.0.0", "enabled": True},
                    "settings": {"nested": {"x": 1, "y": 2}},
                },
            )

            self.assertEqual(migrated["plugin"]["config_version"], "2.0.0")
            self.assertFalse(migrated["plugin"]["enabled"])
            self.assertEqual(migrated["settings"], {"nested": {"x": 9, "y": 2}})
            self.assertNotIn("obsolete", migrated)
            self.assertEqual(plugin._get_current_config_version({}), "0.0.0")

            structure_changed = plugin._migrate_config_values(
                {"settings": "old-shape"},
                {"settings": {"nested": {"x": 1}}},
            )
            self.assertEqual(structure_changed["settings"], {"nested": {"x": 1}})

            backup_path = plugin._backup_config_file(str(current_path))
            self.assertTrue(Path(backup_path).exists())
            with patch("src.plugin_system.base.plugin_base.shutil.copy2", side_effect=OSError("boom")):
                self.assertEqual(plugin._backup_config_file(str(current_path)), "")

    def test_plugin_base_load_config_error_and_fallback_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            write_manifest(plugin_dir)
            (plugin_dir / "config.toml").write_text(
                """
[plugin]
enabled = false

[settings]
threshold = 0.7
""".strip(),
                encoding="utf-8",
            )
            plugin = ConcreteConfigPlugin(str(plugin_dir))

        self.assertEqual(plugin.config["settings"]["threshold"], 0.7)
        self.assertFalse(plugin.enable_plugin)

        bare_plugin = object.__new__(NoConfigPlugin)
        bare_plugin.manifest_data = {}
        bare_plugin.log_prefix = "[Plugin:bare]"
        self.assertEqual(bare_plugin.get_manifest_info("name", default="fallback"), "fallback")
        with self.assertRaises(ValueError):
            bare_plugin._validate_manifest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            write_manifest(plugin_dir)
            with patch.object(NoConfigPlugin, "_generate_and_save_default_config"):
                plugin = NoConfigPlugin(str(plugin_dir), config_file_name="missing.toml")

        self.assertEqual(plugin.config, {})

        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            write_manifest(plugin_dir)
            plugin = NoConfigPlugin(str(plugin_dir), config_file_name="")
            plugin.plugin_dir = ""
            with patch(
                "src.plugin_system.base.plugin_base.inspect.getfile", return_value=str(plugin_dir / "plugin.py")
            ):
                plugin._config_file_name = "config.toml"
                plugin._load_plugin_config()

        self.assertEqual(plugin.config, {})

        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            write_manifest(plugin_dir)
            (plugin_dir / "config.toml").write_text(
                """
[plugin]
enabled = true
""".strip(),
                encoding="utf-8",
            )
            plugin = NoConfigPlugin(str(plugin_dir), config_file_name="")
            plugin.plugin_dir = ""
            with (
                patch("src.plugin_system.base.plugin_base.inspect.getfile", side_effect=TypeError),
                patch(
                    "src.plugin_system.base.plugin_base.inspect.getmodule",
                    return_value=SimpleNamespace(__file__=str(plugin_dir / "module.py")),
                ),
            ):
                plugin._config_file_name = "config.toml"
                plugin._load_plugin_config()

        self.assertEqual(plugin.config["plugin"]["enabled"], True)

        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            write_manifest(plugin_dir)
            plugin = NoConfigPlugin(str(plugin_dir), config_file_name="")
            plugin.plugin_dir = ""
            with (
                patch("src.plugin_system.base.plugin_base.inspect.getfile", side_effect=TypeError),
                patch("src.plugin_system.base.plugin_base.inspect.getmodule", return_value=SimpleNamespace()),
            ):
                plugin._config_file_name = "config.toml"
                plugin._load_plugin_config()

        self.assertEqual(plugin.config, {})

    def test_plugin_base_manifest_validation_and_read_error_paths(self) -> None:
        class EmptyNamePlugin(ConcreteConfigPlugin):
            @property
            def plugin_name(self) -> str:
                return ""

        with tempfile.TemporaryDirectory() as tmp_dir:
            write_manifest(Path(tmp_dir))
            with self.assertRaisesRegex(ValueError, "必须定义 plugin_name"):
                EmptyNamePlugin(tmp_dir)

        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            write_manifest(plugin_dir, name="")
            with (
                patch.object(ConcreteConfigPlugin, "_validate_manifest", return_value=None),
                self.assertRaisesRegex(ValueError, "缺少name字段"),
            ):
                ConcreteConfigPlugin(str(plugin_dir))

        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            write_manifest(plugin_dir, description="")
            with (
                patch.object(ConcreteConfigPlugin, "_validate_manifest", return_value=None),
                self.assertRaisesRegex(ValueError, "缺少description字段"),
            ):
                ConcreteConfigPlugin(str(plugin_dir))

        with self.assertRaisesRegex(ValueError, "没有插件目录路径"):
            ConcreteConfigPlugin("")

        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            write_manifest(plugin_dir)
            with (
                patch("builtins.open", side_effect=OSError("boom")),
                self.assertRaisesRegex(OSError, "读取manifest文件失败"),
            ):
                ConcreteConfigPlugin(str(plugin_dir))

    def test_plugin_base_migrates_config_versions_and_preserves_supported_old_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            write_manifest(plugin_dir)
            original_config = """
[plugin]
config_version = "1.0.0"
enabled = false
removed = "old"

[settings]
threshold = 0.9
old_only = "ignored"
""".strip()
            (plugin_dir / "config.toml").write_text(original_config, encoding="utf-8")

            plugin = ConcreteConfigPlugin(str(plugin_dir))
            saved = toml.load(plugin_dir / "config.toml")
            backup_files = list(plugin_dir.glob("config.toml.backup_*"))
            backup_content = backup_files[0].read_text(encoding="utf-8") if backup_files else ""

        self.assertFalse(plugin.enable_plugin)
        self.assertEqual(plugin.config["plugin"]["config_version"], "2.0.0")
        self.assertFalse(plugin.config["plugin"]["enabled"])
        self.assertEqual(plugin.config["settings"]["threshold"], 0.9)
        self.assertEqual(plugin.config["settings"]["mode"], "auto")
        self.assertEqual(plugin.config["settings"]["nested"], {"x": 1})
        self.assertNotIn("removed", plugin.config["plugin"])
        self.assertNotIn("old_only", plugin.config["settings"])
        self.assertEqual(saved["plugin"]["config_version"], "2.0.0")
        self.assertFalse(saved["plugin"]["enabled"])
        self.assertEqual(saved["settings"]["threshold"], 0.9)
        self.assertEqual(len(backup_files), 1)
        self.assertEqual(backup_content, original_config)

    def test_plugin_base_keeps_original_config_when_migration_backup_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            write_manifest(plugin_dir)
            config_path = plugin_dir / "config.toml"
            original_config = """
[plugin]
config_version = "1.0.0"
enabled = false
removed = "keep"
""".strip()
            config_path.write_text(original_config, encoding="utf-8")

            with patch.object(ConcreteConfigPlugin, "_backup_config_file", return_value=""):
                plugin = ConcreteConfigPlugin(str(plugin_dir))

            self.assertEqual(config_path.read_text(encoding="utf-8"), original_config)
            self.assertEqual(plugin.config["plugin"]["config_version"], "1.0.0")
            self.assertEqual(plugin.config["plugin"]["removed"], "keep")

    def test_plugin_base_dependency_checks_and_manifest_error_paths_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            write_manifest(plugin_dir)
            plugin = ConcreteConfigPlugin(str(plugin_dir), dependencies=["required_plugin"])

            registry_module = __import__("src.plugin_system.core.component_registry", fromlist=["component_registry"])
            with patch.object(registry_module.component_registry, "get_plugin_info", return_value=object()):
                self.assertTrue(plugin._check_dependencies())
            with patch.object(registry_module.component_registry, "get_plugin_info", return_value=None):
                self.assertFalse(plugin._check_dependencies())

        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaises(FileNotFoundError):
                ConcreteConfigPlugin(tmp_dir)

        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            (plugin_dir / "_manifest.json").write_text("{bad json", encoding="utf-8")
            with self.assertRaises(ValueError):
                ConcreteConfigPlugin(str(plugin_dir))

        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin_dir = Path(tmp_dir)
            write_manifest(plugin_dir, name="")
            with self.assertRaises(ValueError):
                ConcreteConfigPlugin(str(plugin_dir))


class BasePluginRegistrationTest(unittest.TestCase):
    def test_abstract_component_method_body_raises_when_called_directly(self) -> None:
        with self.assertRaises(NotImplementedError):
            BasePlugin.get_plugin_components(object())

    def test_register_plugin_stamps_component_plugin_name_and_keeps_successful_components(self) -> None:
        action_info = ActionInfo(name="composite_action", component_type=ComponentType.ACTION)
        tool_info = ToolInfo(name="composite_tool", component_type=ComponentType.TOOL)
        registry = SimpleNamespace(
            register_component=Mock(side_effect=[True, False]),
            register_plugin=Mock(return_value=True),
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            write_manifest(Path(tmp_dir), name="Composite Plugin", description="Composite plugin")
            plugin = ConcreteCompositePlugin(
                tmp_dir,
                components=[(action_info, ConcreteAction), (tool_info, ConcreteTool)],
            )
            registry_module = importlib.import_module("src.plugin_system.core.component_registry")
            with patch.object(registry_module, "component_registry", registry):
                self.assertTrue(plugin.register_plugin())

        self.assertEqual(action_info.plugin_name, "composite_plugin")
        self.assertEqual(tool_info.plugin_name, "composite_plugin")
        self.assertEqual(plugin.plugin_info.components, [action_info])
        registry.register_component.assert_any_call(action_info, ConcreteAction)
        registry.register_component.assert_any_call(tool_info, ConcreteTool)
        registry.register_plugin.assert_called_once_with(plugin.plugin_info)

    def test_register_plugin_stops_before_component_registration_when_dependency_is_missing(self) -> None:
        registry = SimpleNamespace(
            get_plugin_info=Mock(return_value=None),
            register_component=Mock(),
            register_plugin=Mock(),
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            write_manifest(Path(tmp_dir), name="Composite Plugin", description="Composite plugin")
            plugin = ConcreteCompositePlugin(
                tmp_dir,
                components=[(ActionInfo(name="composite_action", component_type=ComponentType.ACTION), ConcreteAction)],
                dependencies=["required_plugin"],
            )
            registry_module = importlib.import_module("src.plugin_system.core.component_registry")
            with patch.object(registry_module, "component_registry", registry):
                self.assertFalse(plugin.register_plugin())

        registry.get_plugin_info.assert_called_once_with("required_plugin")
        registry.register_component.assert_not_called()
        registry.register_plugin.assert_not_called()

    def test_register_plugin_returns_false_when_plugin_registry_rejects_plugin_info(self) -> None:
        action_info = ActionInfo(name="composite_action", component_type=ComponentType.ACTION)
        registry = SimpleNamespace(
            register_component=Mock(return_value=True),
            register_plugin=Mock(return_value=False),
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            write_manifest(Path(tmp_dir), name="Composite Plugin", description="Composite plugin")
            plugin = ConcreteCompositePlugin(
                tmp_dir,
                components=[(action_info, ConcreteAction)],
            )
            registry_module = importlib.import_module("src.plugin_system.core.component_registry")
            with patch.object(registry_module, "component_registry", registry):
                self.assertFalse(plugin.register_plugin())

        self.assertEqual(plugin.plugin_info.components, [action_info])
        registry.register_component.assert_called_once_with(action_info, ConcreteAction)
        registry.register_plugin.assert_called_once_with(plugin.plugin_info)


if __name__ == "__main__":
    unittest.main()
