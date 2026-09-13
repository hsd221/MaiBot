import importlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.llm_models.payload_content.tool_option import ToolCall
from src.plugin_system.base.component_types import ComponentInfo, ComponentType, EventType
from src.plugin_system.core.component_registry import ComponentRegistry
from src.plugin_system.core.events_manager import EventsManager
from src.plugin_system.core.plugin_manager import PluginManager
from src.plugin_system.core.tool_use import ToolExecutor

module = importlib.import_module("src.plugin_system.core.plugin_manager")
events = importlib.import_module("src.plugin_system.core.events_manager")


class PluginAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_call_arguments_remain_unchanged(self):
        executor = object.__new__(ToolExecutor)
        call = ToolCall("call-1", "fixture", {"value": "input"})
        tool = SimpleNamespace(execute=AsyncMock(return_value={"content": "ok"}))
        await executor.execute_tool_call(call, tool)
        self.assertEqual(call.args, {"value": "input"})
        self.assertTrue(tool.execute.call_args.args[0]["llm_called"])

    async def test_missing_event_stream_and_context_are_skipped(self):
        manager = EventsManager()
        for stream in (None, SimpleNamespace(context=None)):
            with (
                self.subTest(stream=stream),
                patch.object(
                    events, "get_chat_manager", return_value=SimpleNamespace(get_stream=lambda _, stream=stream: stream)
                ),
            ):
                self.assertEqual(await manager.handle_mai_events(EventType.ON_PLAN, stream_id="missing"), (True, None))
        self.assertEqual(await manager.handle_mai_events(EventType.ON_PLAN), (True, None))

    def test_invalid_component_types_do_not_leave_registry_entries(self):
        registry = ComponentRegistry()
        info = ComponentInfo(
            name="invalid", component_type=ComponentType.TOOL, description="fixture", plugin_name="fixture"
        )
        self.assertFalse(registry.register_component(info, object))
        self.assertIsNone(registry.get_component_info("invalid", ComponentType.TOOL))

    async def test_reload_reads_modified_plugin_source(self):
        manager = object.__new__(PluginManager)
        manager.plugin_classes = {"fixture": type("OldPlugin", (), {"__module__": "plugins.fixture", "value": 1})}
        manager.plugin_paths = {}
        manager.loaded_plugins = {}
        manager.failed_plugins = {}
        manager.remove_registered_plugin = AsyncMock(return_value=True)
        manager.load_registered_plugin_classes = Mock(return_value=(True, 1))
        source = (
            "from src.plugin_system.core.plugin_manager import plugin_manager\n"
            "class ChangedPlugin:\n    value = 2\n"
            "plugin_manager.plugin_classes['fixture'] = ChangedPlugin\n"
        )
        with tempfile.TemporaryDirectory() as temp:
            manager.plugin_paths["fixture"] = temp
            Path(temp, "plugin.py").write_text(source)
            with patch.object(manager, "_preflight_plugin_module"), patch.object(module, "plugin_manager", manager):
                self.assertTrue(await manager.reload_registered_plugin("fixture"))
        self.assertEqual(manager.plugin_classes["fixture"].value, 2)
