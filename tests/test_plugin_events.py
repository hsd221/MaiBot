import asyncio
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from maim_message import Seg

from src.chat.message_receive.message import MessageRecv
from src.plugin_system.base.base_events_handler import BaseEventHandler
from src.plugin_system.base.component_types import (
    ComponentType,
    CustomEventHandlerResult,
    EventHandlerInfo,
    EventType,
    MaiMessages,
)
from src.plugin_system.core.events_manager import EventsManager, _normalize_additional_data
from src.plugin_system.core.global_announcement_manager import GlobalAnnouncementManager


def make_message(
    additional_config='{"trace": 1}',
    *,
    group: bool = True,
    message_segment: Seg | None = None,
) -> MessageRecv:
    message_info = {
        "platform": "qq",
        "message_id": "msg-1",
        "time": 1.0,
        "user_info": {
            "platform": "qq",
            "user_id": "user-1",
            "user_nickname": "Alice",
            "user_cardname": "Ali",
        },
        "additional_config": additional_config,
        "format_info": {"content_format": "", "accept_format": ""},
        "template_info": {"template_items": {}},
    }
    if group:
        message_info["group_info"] = {"platform": "qq", "group_id": "group-1", "group_name": "Group"}

    message = MessageRecv(
        {
            "message_info": message_info,
            "message_segment": (message_segment or Seg(type="text", data="hello")).to_dict(),
            "raw_message": "hello",
            "processed_plain_text": "hello",
        }
    )
    message.update_chat_stream(SimpleNamespace(stream_id="stream-1"))
    return message


class InterceptingHandler(BaseEventHandler):
    event_type: EventType | str = "custom_intercept"
    handler_name = "intercepting"
    handler_description = "Intercepting handler"
    weight = 10
    intercept_message = True
    seen_messages = []
    seen_configs = []
    result = (
        True,
        False,
        "stopped",
        CustomEventHandlerResult(message="stored"),
        MaiMessages(plain_text="modified"),
    )

    async def execute(self, message):
        self.__class__.seen_messages.append(message)
        self.__class__.seen_configs.append(self.plugin_config)
        return self.__class__.result


class LowerWeightHandler(InterceptingHandler):
    handler_name = "lower_weight"
    weight = 1


class BackgroundHandler(BaseEventHandler):
    event_type: EventType | str = "custom_background"
    handler_name = "background"
    intercept_message = False
    seen_messages = []

    async def execute(self, message):
        self.__class__.seen_messages.append(message)
        return True, True, "background done", CustomEventHandlerResult(message="background"), None


class InvalidResultHandler(BaseEventHandler):
    event_type: EventType | str = "custom_invalid"
    handler_name = "invalid_result"
    intercept_message = True

    async def execute(self, message):
        return True, False


class NonTupleResultHandler(InvalidResultHandler):
    handler_name = "non_tuple_result"

    async def execute(self, message):
        return "bad-result"


class FailingInterceptingHandler(InvalidResultHandler):
    handler_name = "failing_intercepting"

    async def execute(self, message):
        return False, True, "failed", None, None


class RaisingInterceptingHandler(InvalidResultHandler):
    handler_name = "raising_intercepting"

    async def execute(self, message):
        raise RuntimeError("handler failed")


class DotNameHandler(BaseEventHandler):
    event_type = EventType.ON_MESSAGE
    handler_name = "bad.name"

    async def execute(self, message):
        return True, True, None, None, None


class MissingEventHandler(BaseEventHandler):
    event_type: EventType | str = "custom_missing_event"
    handler_name = "missing_event"

    async def execute(self, message):
        return True, True, None, None, None


class UnknownEventHandler(BaseEventHandler):
    event_type = EventType.UNKNOWN
    handler_name = "unknown_event"

    async def execute(self, message):
        return True, True, None, None, None


class MismatchedEventHandler(BaseEventHandler):
    event_type: EventType | str = "custom_mismatched_class_event"
    handler_name = "mismatched_event"

    async def execute(self, message):
        return True, True, None, None, None


class NonCoroutineHandler(BaseEventHandler):
    event_type: EventType | str = "custom_background"
    handler_name = "non_coroutine"

    def execute(self, message):
        return None


class FakeDoneTask:
    def __init__(self, result_or_error, name: str = "fake-task"):
        self._result_or_error = result_or_error
        self._name = name

    def get_name(self):
        return self._name

    def result(self):
        if isinstance(self._result_or_error, BaseException):
            raise self._result_or_error
        return self._result_or_error


async def never_finishes():
    await asyncio.sleep(60)


class PluginEventsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        InterceptingHandler.seen_messages = []
        InterceptingHandler.seen_configs = []
        LowerWeightHandler.seen_messages = []
        LowerWeightHandler.seen_configs = []
        BackgroundHandler.seen_messages = []

    def test_normalize_additional_data_accepts_mapping_json_and_ignores_invalid_values(self) -> None:
        self.assertEqual(_normalize_additional_data({"x": 1}), {"x": 1})
        self.assertEqual(_normalize_additional_data('{"x": 1}'), {"x": 1})
        self.assertEqual(_normalize_additional_data("[1, 2]"), {})
        self.assertEqual(_normalize_additional_data("{bad json"), {})
        self.assertEqual(_normalize_additional_data(None), {})

    def test_base_event_handler_metadata_config_and_invalid_names(self) -> None:
        info = InterceptingHandler.get_handler_info()
        handler = InterceptingHandler()

        handler.set_plugin_name("plugin-a")
        handler.set_plugin_config({"nested": {"enabled": True}})

        self.assertEqual(info.name, "intercepting")
        self.assertEqual(info.description, "Intercepting handler")
        self.assertEqual(info.event_type, "custom_intercept")
        self.assertTrue(info.intercept_message)
        self.assertEqual(handler.plugin_name, "plugin-a")
        self.assertTrue(handler.get_config("nested.enabled"))
        self.assertEqual(handler.get_config("nested.missing", "fallback"), "fallback")

        with self.assertRaisesRegex(ValueError, "包含非法字符"):
            DotNameHandler.get_handler_info()

    def test_register_event_rejects_duplicates_and_history_errors_are_explicit(self) -> None:
        manager = EventsManager()

        manager.register_event("tracked", enable_history_result=True)

        with self.assertRaisesRegex(ValueError, "已存在"):
            manager.register_event("tracked")
        with self.assertRaisesRegex(ValueError, "未知事件类型"):
            asyncio.run(manager.get_event_result_history(EventType.UNKNOWN))
        with self.assertRaisesRegex(ValueError, "未注册"):
            asyncio.run(manager.get_event_result_history("missing"))
        with self.assertRaisesRegex(ValueError, "历史记录未启用"):
            asyncio.run(manager.get_event_result_history(EventType.ON_MESSAGE))

    async def test_clear_event_result_history_validates_inputs_and_clears_enabled_history(self) -> None:
        manager = EventsManager()
        manager.register_event("tracked", enable_history_result=True)
        stored = CustomEventHandlerResult(message="stored")
        manager._events_result_history["tracked"].append(stored)

        await manager.clear_event_result_history("tracked")
        self.assertEqual(await manager.get_event_result_history("tracked"), [])
        with self.assertRaisesRegex(ValueError, "未知事件类型"):
            await manager.clear_event_result_history(EventType.UNKNOWN)
        with self.assertRaisesRegex(ValueError, "未注册"):
            await manager.clear_event_result_history("missing")
        with self.assertRaisesRegex(ValueError, "历史记录未启用"):
            await manager.clear_event_result_history(EventType.ON_MESSAGE)

    async def test_register_subscriber_sorts_by_weight_and_rejects_invalid_or_duplicate_handlers(self) -> None:
        manager = EventsManager()
        manager.register_event("custom_intercept", enable_history_result=True)
        high_info = InterceptingHandler.get_handler_info()
        low_info = LowerWeightHandler.get_handler_info()
        invalid_info = EventHandlerInfo(
            name="bad",
            component_type=ComponentType.EVENT_HANDLER,
            event_type="custom_intercept",
        )

        self.assertTrue(manager.register_event_subscriber(low_info, LowerWeightHandler))
        self.assertTrue(manager.register_event_subscriber(high_info, InterceptingHandler))
        self.assertFalse(manager.register_event_subscriber(invalid_info, object))
        self.assertFalse(manager.register_event_subscriber(high_info, InterceptingHandler))
        self.assertEqual(
            [handler.handler_name for handler in manager._events_subscribers["custom_intercept"]],
            [
                "intercepting",
                "lower_weight",
            ],
        )

        self.assertTrue(await manager.unregister_event_subscriber("intercepting"))
        self.assertFalse(await manager.unregister_event_subscriber("missing"))

    async def test_register_subscriber_rejects_missing_unknown_and_mismatched_event_handlers(self) -> None:
        manager = EventsManager()

        self.assertFalse(manager.register_event_subscriber(MissingEventHandler.get_handler_info(), MissingEventHandler))
        self.assertFalse(manager.register_event_subscriber(UnknownEventHandler.get_handler_info(), UnknownEventHandler))
        self.assertNotIn("unknown_event", manager._handler_mapping)
        self.assertTrue(manager._insert_event_handler(MissingEventHandler, MissingEventHandler.get_handler_info()))
        self.assertEqual(manager._events_subscribers["custom_missing_event"][0].handler_name, "missing_event")
        self.assertFalse(
            manager._insert_event_handler(
                UnknownEventHandler,
                EventHandlerInfo(name="unknown_event", component_type=ComponentType.EVENT_HANDLER),
            )
        )
        manager.register_event("custom_mismatched_class_event")
        self.assertTrue(
            manager._insert_event_handler(MismatchedEventHandler, MismatchedEventHandler.get_handler_info())
        )
        manager._events_subscribers["custom_mismatched_class_event"].clear()
        manager._handler_mapping["mismatched_event"] = MismatchedEventHandler
        self.assertFalse(await manager.unregister_event_subscriber("mismatched_event"))
        self.assertFalse(manager._remove_event_handler_instance(UnknownEventHandler))

    async def test_handle_mai_events_runs_intercepting_handlers_with_plugin_config_and_history(self) -> None:
        manager = EventsManager()
        manager.register_event("custom_intercept", enable_history_result=True)
        info = InterceptingHandler.get_handler_info()
        info.plugin_name = "plugin-a"
        self.assertTrue(manager.register_event_subscriber(info, InterceptingHandler))
        core_pkg = importlib.import_module("src.plugin_system.core")

        with patch.object(core_pkg.component_registry, "get_plugin_config", return_value={"flag": True}):
            continue_flag, modified_message = await manager.handle_mai_events(
                "custom_intercept",
                message=make_message(),
                extra_data={"extra": "value"},
            )

        history = await manager.get_event_result_history("custom_intercept")
        self.assertFalse(continue_flag)
        self.assertEqual(modified_message.plain_text, "modified")
        self.assertEqual(history[0].message, "stored")
        self.assertEqual(InterceptingHandler.seen_configs, [{"flag": True}])
        transformed = InterceptingHandler.seen_messages[0]
        self.assertEqual(transformed.stream_id, "stream-1")
        self.assertEqual(transformed.plain_text, "hello")
        self.assertEqual(transformed.message_segments[0].type, "text")
        self.assertEqual(transformed.additional_data, {"trace": 1, "extra": "value"})
        self.assertTrue(transformed.is_group_message)
        self.assertFalse(transformed.is_private_message)

    async def test_handle_mai_events_returns_when_no_handlers_are_registered(self) -> None:
        manager = EventsManager()

        continue_flag, modified_message = await manager.handle_mai_events(
            EventType.ON_MESSAGE,
            message=make_message(),
        )

        self.assertTrue(continue_flag)
        self.assertIsNone(modified_message)

    async def test_handle_mai_events_skips_disabled_chat_handlers_and_runs_on_stop_without_message(self) -> None:
        manager = EventsManager()
        manager.register_event("custom_intercept", enable_history_result=True)
        info = InterceptingHandler.get_handler_info()
        info.plugin_name = "plugin-a"
        self.assertTrue(manager.register_event_subscriber(info, InterceptingHandler))
        global_module = importlib.import_module("src.plugin_system.core.events_manager")
        core_pkg = importlib.import_module("src.plugin_system.core")

        with (
            patch.object(
                global_module.global_announcement_manager,
                "get_disabled_chat_event_handlers",
                return_value=["intercepting"],
            ),
            patch.object(core_pkg.component_registry, "get_plugin_config", return_value={"flag": True}),
        ):
            continue_flag, modified_message = await manager.handle_mai_events(
                "custom_intercept",
                message=make_message(),
            )

        self.assertTrue(continue_flag)
        self.assertIsNone(modified_message)
        self.assertEqual(InterceptingHandler.seen_messages, [])

        class StopHandler(InterceptingHandler):
            event_type = EventType.ON_STOP
            handler_name = "stop_intercepting"
            seen_messages = []

        stop_manager = EventsManager()
        stop_info = StopHandler.get_handler_info()
        stop_info.plugin_name = "plugin-a"
        self.assertTrue(stop_manager.register_event_subscriber(stop_info, StopHandler))
        with patch.object(core_pkg.component_registry, "get_plugin_config", return_value={}):
            continue_flag, modified_message = await stop_manager.handle_mai_events(EventType.ON_STOP)

        self.assertFalse(continue_flag)
        self.assertEqual(modified_message.plain_text, "modified")
        self.assertEqual(StopHandler.seen_messages, [None])

    async def test_background_handlers_run_as_tasks_and_record_history(self) -> None:
        manager = EventsManager()
        manager.register_event("custom_background", enable_history_result=True)
        self.assertTrue(manager.register_event_subscriber(BackgroundHandler.get_handler_info(), BackgroundHandler))
        core_pkg = importlib.import_module("src.plugin_system.core")

        with patch.object(core_pkg.component_registry, "get_plugin_config", return_value={}):
            continue_flag, modified_message = await manager.handle_mai_events(
                "custom_background",
                message=make_message(),
            )

        await asyncio.sleep(0)
        await asyncio.sleep(0)
        history = await manager.get_event_result_history("custom_background")

        self.assertTrue(continue_flag)
        self.assertIsNone(modified_message)
        self.assertEqual(BackgroundHandler.seen_messages[0].plain_text, "hello")
        self.assertEqual(history[0].message, "background")
        self.assertEqual(manager._handler_tasks["background"], [])

    async def test_cancel_handler_tasks_clears_pending_done_timeout_and_unexpected_failures(self) -> None:
        manager = EventsManager()
        pending = asyncio.create_task(never_finishes())
        manager._handler_tasks["pending"] = [pending]

        await manager.cancel_handler_tasks("pending")

        self.assertTrue(pending.cancelled())
        self.assertNotIn("pending", manager._handler_tasks)

        done = asyncio.create_task(asyncio.sleep(0))
        await done
        manager._handler_tasks["done"] = [done]
        await manager.cancel_handler_tasks("done")
        self.assertNotIn("done", manager._handler_tasks)

        timed_out = asyncio.create_task(never_finishes())
        manager._handler_tasks["timeout"] = [timed_out]
        with patch.object(asyncio, "wait_for", side_effect=asyncio.TimeoutError):
            await manager.cancel_handler_tasks("timeout")
        self.assertNotIn("timeout", manager._handler_tasks)
        timed_out.cancel()
        await asyncio.gather(timed_out, return_exceptions=True)

        failed = asyncio.create_task(never_finishes())
        manager._handler_tasks["failure"] = [failed]
        with patch.object(asyncio, "wait_for", side_effect=RuntimeError("wait failed")):
            await manager.cancel_handler_tasks("failure")
        self.assertNotIn("failure", manager._handler_tasks)
        failed.cancel()
        await asyncio.gather(failed, return_exceptions=True)

    def test_transform_event_message_handles_private_seglist_and_llm_fields(self) -> None:
        manager = EventsManager()
        llm_response = SimpleNamespace(
            content="answer",
            reasoning="because",
            model="gpt-test",
            tool_calls=[{"name": "lookup"}],
        )
        message = make_message(
            {"dict_trace": 2},
            group=False,
            message_segment=Seg(type="seglist", data=[Seg(type="text", data="hello"), Seg(type="text", data="world")]),
        )

        transformed = manager._transform_event_message(
            message,
            llm_prompt="prompt",
            llm_response=llm_response,
            extra_data={"extra": True},
        )

        self.assertEqual(transformed.llm_prompt, "prompt")
        self.assertEqual(transformed.llm_response_content, "answer")
        self.assertEqual(transformed.llm_response_reasoning, "because")
        self.assertEqual(transformed.llm_response_model, "gpt-test")
        self.assertEqual(transformed.llm_response_tool_call, [{"name": "lookup"}])
        self.assertEqual([segment.data for segment in transformed.message_segments], ["hello", "world"])
        self.assertEqual(transformed.additional_data, {"dict_trace": 2, "extra": True})
        self.assertFalse(transformed.is_group_message)
        self.assertTrue(transformed.is_private_message)
        self.assertEqual(transformed.message_base_info["user_id"], "user-1")

    def test_prepare_message_builds_from_stream_and_without_message_or_rejects_missing_stream_id(self) -> None:
        manager = EventsManager()
        llm_response = SimpleNamespace(content="answer", reasoning="why", model="model-a", tool_calls=[])
        message = make_message()
        stream = SimpleNamespace(
            group_info=SimpleNamespace(group_id="group-1"),
            context=SimpleNamespace(get_last_message=lambda: message),
        )
        chat_manager = SimpleNamespace(get_stream=lambda stream_id: stream)
        events_module = importlib.import_module("src.plugin_system.core.events_manager")

        with patch.object(events_module, "get_chat_manager", return_value=chat_manager):
            built = manager._prepare_message(
                EventType.ON_MESSAGE,
                stream_id="stream-1",
                llm_prompt="prompt",
                llm_response=llm_response,
                extra_data={"from_stream": True},
            )
            without_message = manager._prepare_message(
                EventType.ON_SEND_AFTER_BUILD_MESSAGE,
                stream_id="stream-1",
                llm_response=llm_response,
                action_usage=["wave"],
                extra_data={"extra": 1},
            )

        self.assertEqual(built.plain_text, "hello")
        self.assertEqual(built.additional_data["from_stream"], True)
        self.assertEqual(without_message.stream_id, "stream-1")
        self.assertTrue(without_message.is_group_message)
        self.assertFalse(without_message.is_private_message)
        self.assertEqual(without_message.action_usage, ["wave"])
        self.assertEqual(without_message.additional_data, {"response_is_processed": True, "extra": 1})
        self.assertIsNone(manager._prepare_message(EventType.ON_START))
        self.assertIsNone(manager._prepare_message(EventType.ON_MESSAGE))

        missing_stream_manager = SimpleNamespace(get_stream=lambda stream_id: None)
        with patch.object(events_module, "get_chat_manager", return_value=missing_stream_manager):
            self.assertIsNone(manager._build_message_from_stream("missing"))
            self.assertIsNone(manager._transform_event_without_message("missing"))

    async def test_invalid_intercepting_result_is_ignored_without_blocking_processing(self) -> None:
        manager = EventsManager()
        manager.register_event("custom_invalid")
        handler = InvalidResultHandler()

        continue_flag, modified_message = await manager._dispatch_intercepting_handler_task(
            handler,
            "custom_invalid",
            MaiMessages(plain_text="hello"),
        )

        self.assertTrue(continue_flag)
        self.assertIsNone(modified_message)

    async def test_intercepting_handler_failure_exception_and_history_mismatch_do_not_block_processing(self) -> None:
        manager = EventsManager()
        manager.register_event("custom_invalid", enable_history_result=True)

        continue_flag, modified_message = await manager._dispatch_intercepting_handler_task(
            NonTupleResultHandler(),
            "custom_invalid",
            MaiMessages(plain_text="hello"),
        )
        self.assertTrue(continue_flag)
        self.assertIsNone(modified_message)

        continue_flag, modified_message = await manager._dispatch_intercepting_handler_task(
            FailingInterceptingHandler(),
            "custom_invalid",
            MaiMessages(plain_text="hello"),
        )
        self.assertTrue(continue_flag)
        self.assertIsNone(modified_message)

        manager._events_result_history.pop("custom_invalid")
        InterceptingHandler.result = (
            True,
            False,
            "stored",
            CustomEventHandlerResult(message="lost"),
            MaiMessages(plain_text="modified"),
        )
        continue_flag, modified_message = await manager._dispatch_intercepting_handler_task(
            InterceptingHandler(),
            "custom_invalid",
            MaiMessages(plain_text="hello"),
        )
        self.assertTrue(continue_flag)
        self.assertIsNone(modified_message)
        InterceptingHandler.result = (
            True,
            False,
            "stopped",
            CustomEventHandlerResult(message="stored"),
            MaiMessages(plain_text="modified"),
        )

        continue_flag, modified_message = await manager._dispatch_intercepting_handler_task(
            RaisingInterceptingHandler(),
            "custom_invalid",
            MaiMessages(plain_text="hello"),
        )
        self.assertTrue(continue_flag)
        self.assertIsNone(modified_message)

        with self.assertRaisesRegex(ValueError, "未知事件类型"):
            await manager._dispatch_intercepting_handler_task(
                InterceptingHandler(),
                EventType.UNKNOWN,
                MaiMessages(plain_text="hello"),
            )
        with self.assertRaisesRegex(ValueError, "未注册"):
            await manager._dispatch_intercepting_handler_task(
                InterceptingHandler(),
                "missing_event",
                MaiMessages(plain_text="hello"),
            )

    async def test_dispatch_handler_task_handles_unknown_event_task_creation_errors_and_callbacks(self) -> None:
        manager = EventsManager()
        manager.register_event("custom_background", enable_history_result=True)

        with self.assertRaisesRegex(ValueError, "未知事件类型"):
            manager._dispatch_handler_task(BackgroundHandler(), EventType.UNKNOWN, MaiMessages())

        manager._dispatch_handler_task(NonCoroutineHandler(), "custom_background", MaiMessages())
        self.assertNotIn("non_coroutine", manager._handler_tasks)

        manager._handler_tasks["callback"] = []
        success_task = FakeDoneTask(
            (True, True, "ok", CustomEventHandlerResult(message="stored"), None),
            name="callback-success",
        )
        manager._handler_tasks["callback"].append(success_task)
        manager._task_done_callback(success_task, "custom_background", "callback")
        self.assertEqual((await manager.get_event_result_history("custom_background"))[0].message, "stored")
        self.assertEqual(manager._handler_tasks["callback"], [])

        failure_task = FakeDoneTask((False, True, "bad", None, None), name="callback-failure")
        manager._handler_tasks["callback"] = [failure_task]
        manager._task_done_callback(failure_task, "custom_background", "callback")
        self.assertEqual(manager._handler_tasks["callback"], [])

        manager._handler_tasks["callback"] = []
        cancelled_task = FakeDoneTask(asyncio.CancelledError(), name="callback-cancelled")
        manager._handler_tasks["callback"].append(cancelled_task)
        manager._task_done_callback(cancelled_task, "custom_background", "callback")
        self.assertEqual(manager._handler_tasks["callback"], [])

        manager._events_result_history.pop("custom_background")
        key_error_task = FakeDoneTask(
            (True, True, "ok", CustomEventHandlerResult(message="lost"), None),
            name="callback-key-error",
        )
        manager._handler_tasks["callback"] = [key_error_task]
        manager._task_done_callback(key_error_task, "custom_background", "callback")
        self.assertEqual(manager._handler_tasks["callback"], [])

        exception_task = FakeDoneTask(RuntimeError("task failed"), name="callback-exception")
        manager._handler_tasks["callback"] = [exception_task]
        manager._task_done_callback(exception_task, "custom_background", "callback")
        self.assertEqual(manager._handler_tasks["callback"], [])

        with self.assertRaisesRegex(ValueError, "未知事件类型"):
            manager._task_done_callback(success_task, EventType.UNKNOWN, "callback")
        with self.assertRaisesRegex(ValueError, "未注册"):
            manager._task_done_callback(success_task, "missing_event", "callback")


class GlobalAnnouncementManagerTest(unittest.TestCase):
    def test_disable_enable_and_get_disabled_components_are_chat_scoped_and_copy_lists(self) -> None:
        manager = GlobalAnnouncementManager()

        self.assertTrue(manager.disable_specific_chat_action("chat-1", "wave"))
        self.assertFalse(manager.disable_specific_chat_action("chat-1", "wave"))
        self.assertTrue(manager.disable_specific_chat_command("chat-1", "help"))
        self.assertFalse(manager.disable_specific_chat_command("chat-1", "help"))
        self.assertTrue(manager.disable_specific_chat_tool("chat-1", "search"))
        self.assertFalse(manager.disable_specific_chat_tool("chat-1", "search"))
        self.assertTrue(manager.disable_specific_chat_event_handler("chat-1", "on_message"))
        self.assertFalse(manager.disable_specific_chat_event_handler("chat-1", "on_message"))
        self.assertTrue(manager.disable_specific_chat_action("chat-2", "other"))

        actions = manager.get_disabled_chat_actions("chat-1")
        actions.append("mutated")

        self.assertEqual(manager.get_disabled_chat_actions("chat-1"), ["wave"])
        self.assertEqual(manager.get_disabled_chat_commands("chat-1"), ["help"])
        self.assertEqual(manager.get_disabled_chat_tools("chat-1"), ["search"])
        self.assertEqual(manager.get_disabled_chat_event_handlers("chat-1"), ["on_message"])
        self.assertEqual(manager.get_disabled_chat_actions("chat-2"), ["other"])
        self.assertEqual(manager.get_disabled_chat_actions("missing"), [])

        self.assertTrue(manager.enable_specific_chat_action("chat-1", "wave"))
        self.assertFalse(manager.enable_specific_chat_action("chat-1", "wave"))
        self.assertFalse(manager.enable_specific_chat_action("missing", "wave"))
        self.assertFalse(manager.enable_specific_chat_command("chat-1", "missing"))
        self.assertFalse(manager.enable_specific_chat_command("missing", "help"))
        self.assertFalse(manager.enable_specific_chat_tool("chat-1", "missing"))
        self.assertFalse(manager.enable_specific_chat_tool("missing", "search"))
        self.assertFalse(manager.enable_specific_chat_event_handler("chat-1", "missing"))
        self.assertFalse(manager.enable_specific_chat_event_handler("missing", "on_message"))
        self.assertTrue(manager.enable_specific_chat_command("chat-1", "help"))
        self.assertTrue(manager.enable_specific_chat_tool("chat-1", "search"))
        self.assertTrue(manager.enable_specific_chat_event_handler("chat-1", "on_message"))

        self.assertEqual(manager.get_disabled_chat_actions("chat-1"), [])
        self.assertEqual(manager.get_disabled_chat_commands("chat-1"), [])
        self.assertEqual(manager.get_disabled_chat_tools("chat-1"), [])
        self.assertEqual(manager.get_disabled_chat_event_handlers("chat-1"), [])


if __name__ == "__main__":
    unittest.main()
