import json
import importlib
import unittest
from types import SimpleNamespace
from typing import get_args, get_type_hints
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from maim_message import GroupInfo, UserInfo
from peewee import CharField, IntegerField, Model, SqliteDatabase

from src.chat.message_receive.chat_stream import ChatStream
from src.common.logger import get_logger as common_get_logger
from src.common.data_models.message_data_model import ForwardNode, ReplyContent, ReplyContentType, ReplySetModel
from src.config.api_ada_configs import TaskConfig
from src.plugin_system.apis import (
    chat_api,
    component_manage_api,
    config_api,
    database_api,
    frequency_api,
    generator_api,
    logging_api,
    llm_api,
    message_api,
    person_api,
    plugin_manage_api,
    plugin_register_api,
    send_api,
    tool_api,
)
from src.plugin_system.base.base_plugin import BasePlugin
from src.plugin_system.base.component_types import ComponentType, ToolInfo


test_db = SqliteDatabase(":memory:")


class SampleRecord(Model):
    name = CharField(unique=True)
    count = IntegerField(default=0)

    class Meta:
        database = test_db


def make_api_stream(
    stream_id: str,
    *,
    platform: str = "qq",
    user_id: str = "user-1",
    group_id: str | None = "group-1",
) -> ChatStream:
    group_info = (
        GroupInfo(platform=platform, group_id=group_id, group_name=f"Group {group_id}")
        if group_id is not None
        else None
    )
    return ChatStream(
        stream_id=stream_id,
        platform=platform,
        user_info=UserInfo(
            platform=platform,
            user_id=user_id,
            user_nickname=f"User {user_id}",
            user_cardname=f"Card {user_id}",
        ),
        group_info=group_info,
        data={"create_time": 1.0, "last_active_time": 2.0},
    )


class SendApiTest(unittest.IsolatedAsyncioTestCase):
    def test_parse_content_to_seg_handles_text_media_hybrid_forward_and_custom_types(self) -> None:
        text_seg, text_typing = send_api._parse_content_to_seg(ReplyContent.construct_as_text("hello"))
        image_seg, image_typing = send_api._parse_content_to_seg(
            ReplyContent(content_type=ReplyContentType.IMAGE, content="image64")
        )
        emoji_seg, emoji_typing = send_api._parse_content_to_seg(
            ReplyContent(content_type=ReplyContentType.EMOJI, content="emoji64")
        )
        command_seg, command_typing = send_api._parse_content_to_seg(
            ReplyContent(content_type=ReplyContentType.COMMAND, content={"name": "ping"})
        )
        voice_seg, voice_typing = send_api._parse_content_to_seg(
            ReplyContent(content_type=ReplyContentType.VOICE, content="voice64")
        )
        hybrid_seg, hybrid_typing = send_api._parse_content_to_seg(
            ReplyContent(
                content_type=ReplyContentType.HYBRID,
                content=[
                    ReplyContent.construct_as_text("hello"),
                    ReplyContent(content_type=ReplyContentType.IMAGE, content="image64"),
                    ReplyContent(content_type=ReplyContentType.EMOJI, content="emoji64"),
                    ReplyContent(content_type=ReplyContentType.VOICE, content="ignored"),
                ],
            )
        )
        forward_seg, forward_typing = send_api._parse_content_to_seg(
            ReplyContent.construct_as_forward(
                [
                    ForwardNode.construct_as_id_reference("msg-1"),
                    ForwardNode.construct_as_created_node(
                        "user-1", "Alice", [ReplyContent.construct_as_text("nested")]
                    ),
                ]
            )
        )
        custom_seg, custom_typing = send_api._parse_content_to_seg(ReplyContent("custom_type", {"x": 1}))

        self.assertEqual((text_seg.type, text_seg.data, text_typing), ("text", "hello", True))
        self.assertEqual((image_seg.type, image_seg.data, image_typing), ("image", "image64", False))
        self.assertEqual((emoji_seg.type, emoji_seg.data, emoji_typing), ("emoji", "emoji64", False))
        self.assertEqual((command_seg.type, command_seg.data, command_typing), ("command", {"name": "ping"}, False))
        self.assertEqual((voice_seg.type, voice_seg.data, voice_typing), ("voice", "voice64", False))
        self.assertEqual(hybrid_seg.type, "seglist")
        self.assertEqual([seg.type for seg in hybrid_seg.data], ["text", "image", "emoji"])
        self.assertTrue(hybrid_typing)
        self.assertEqual(forward_seg.type, "forward")
        self.assertEqual(forward_seg.data[0]["message_segment"]["type"], "id")
        self.assertEqual(forward_seg.data[1]["message_segment"]["type"], "seglist")
        self.assertFalse(forward_typing)
        self.assertEqual((custom_seg.type, custom_seg.data, custom_typing), ("custom_type", {"x": 1}, True))

        with self.assertRaisesRegex(AssertionError, "混合类型内容必须是列表"):
            send_api._parse_content_to_seg(SimpleNamespace(content_type=ReplyContentType.HYBRID, content="bad"))
        with self.assertRaisesRegex(AssertionError, "转发类型内容必须是列表"):
            send_api._parse_content_to_seg(SimpleNamespace(content_type=ReplyContentType.FORWARD, content="bad"))
        with self.assertRaisesRegex(AssertionError, "转发节点内容必须是列表"):
            send_api._parse_content_to_seg(
                ReplyContent.construct_as_forward([ForwardNode(user_id="user-1", user_nickname="Alice", content="bad")])
            )

    async def test_custom_reply_set_to_stream_sends_each_item_and_reports_partial_failure(self) -> None:
        reply_set = ReplySetModel()
        reply_set.add_text_content("hello")
        reply_set.add_image_content("image64")

        with patch.object(send_api, "_send_to_target", new=AsyncMock(side_effect=[True, False])) as send:
            status = await send_api.custom_reply_set_to_stream(reply_set, "stream-1", typing=True, show_log=False)

        self.assertFalse(status)
        self.assertEqual(send.await_count, 2)
        first_call = send.await_args_list[0].kwargs
        second_call = send.await_args_list[1].kwargs
        self.assertEqual(first_call["message_segment"].type, "text")
        self.assertTrue(first_call["typing"])
        self.assertEqual(second_call["message_segment"].type, "image")
        self.assertFalse(second_call["typing"])
        self.assertFalse(second_call["show_log"])

    async def test_stream_wrapper_functions_delegate_to_send_service_or_internal_sender(self) -> None:
        reply_message = SimpleNamespace(message_id="msg-1")
        with patch.object(
            send_api.send_service, "message_to_stream", new=AsyncMock(return_value=True)
        ) as message_to_stream:
            self.assertTrue(
                await send_api._send_to_target(
                    message_segment=send_api.Seg(type="text", data="hello"),
                    stream_id="stream-1",
                    display_message="hello",
                    typing=True,
                    set_reply=True,
                    reply_message=reply_message,
                    storage_message=False,
                    show_log=False,
                    selected_expressions=[1],
                )
            )
        message_to_stream.assert_awaited_once()
        target_kwargs = message_to_stream.await_args.kwargs
        self.assertEqual(target_kwargs["stream_id"], "stream-1")
        self.assertTrue(target_kwargs["typing"])
        self.assertTrue(target_kwargs["set_reply"])
        self.assertIs(target_kwargs["reply_message"], reply_message)
        self.assertFalse(target_kwargs["storage_message"])
        self.assertFalse(target_kwargs["show_log"])
        self.assertEqual(target_kwargs["selected_expressions"], [1])

        db_message = SimpleNamespace(message_id="db-msg")
        with patch.object(send_api.send_service, "db_message_to_message_recv", return_value="recv") as rebuild:
            self.assertEqual(send_api.db_message_to_message_recv(db_message), "recv")
        rebuild.assert_called_once_with(db_message)

        with patch.object(send_api.send_service, "text_to_stream", new=AsyncMock(return_value=True)) as text_to_stream:
            self.assertTrue(
                await send_api.text_to_stream(
                    "hello",
                    "stream-1",
                    typing=True,
                    storage_message=False,
                    selected_expressions=[1],
                )
            )
        text_to_stream.assert_awaited_once_with(
            text="hello",
            stream_id="stream-1",
            typing=True,
            set_reply=False,
            reply_message=None,
            storage_message=False,
            selected_expressions=[1],
        )

        with (
            patch.object(send_api.send_service, "emoji_to_stream", new=AsyncMock(return_value=True)) as emoji_to_stream,
            patch.object(send_api.send_service, "image_to_stream", new=AsyncMock(return_value=True)) as image_to_stream,
        ):
            self.assertTrue(
                await send_api.emoji_to_stream(
                    "emoji64",
                    "stream-1",
                    storage_message=False,
                    set_reply=True,
                    reply_message=reply_message,
                )
            )
            self.assertTrue(
                await send_api.image_to_stream(
                    "image64",
                    "stream-1",
                    storage_message=False,
                    set_reply=True,
                    reply_message=reply_message,
                )
            )
        emoji_to_stream.assert_awaited_once_with(
            emoji_base64="emoji64",
            stream_id="stream-1",
            set_reply=True,
            reply_message=reply_message,
            storage_message=False,
        )
        image_to_stream.assert_awaited_once_with(
            image_base64="image64",
            stream_id="stream-1",
            set_reply=True,
            reply_message=reply_message,
            storage_message=False,
        )

        with patch.object(send_api, "_send_to_target", new=AsyncMock(return_value=True)) as send:
            self.assertTrue(await send_api.command_to_stream({"name": "ping"}, "stream-1", display_message="/ping"))
            self.assertTrue(
                await send_api.custom_to_stream(
                    "notice",
                    {"x": 1},
                    "stream-1",
                    typing=True,
                    set_reply=True,
                    storage_message=False,
                    show_log=False,
                )
            )

        command_call = send.await_args_list[0].kwargs
        custom_call = send.await_args_list[1].kwargs
        self.assertEqual(command_call["message_segment"].type, "command")
        self.assertEqual(command_call["message_segment"].data, {"name": "ping"})
        self.assertEqual(command_call["display_message"], "/ping")
        self.assertEqual(custom_call["message_segment"].type, "notice")
        self.assertEqual(custom_call["message_segment"].data, {"x": 1})
        self.assertTrue(custom_call["typing"])
        self.assertTrue(custom_call["set_reply"])
        self.assertFalse(custom_call["storage_message"])
        self.assertFalse(custom_call["show_log"])


class DatabaseApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        test_db.connect()
        test_db.create_tables([SampleRecord])

    def tearDown(self) -> None:
        test_db.drop_tables([SampleRecord])
        test_db.close()

    async def test_db_query_supports_create_get_count_update_delete_and_invalid_types(self) -> None:
        created = await database_api.db_query(SampleRecord, query_type="create", data={"name": "alpha", "count": 1})
        await database_api.db_query(SampleRecord, query_type="create", data={"name": "beta", "count": 2})

        latest = await database_api.db_query(SampleRecord, query_type="get", limit=1, order_by=["-count"])
        alphabetical = await database_api.db_query(SampleRecord, query_type="get", order_by=["name"])
        single = await database_api.db_query(
            SampleRecord,
            query_type="get",
            filters={"name": "alpha"},
            single_result=True,
        )
        count = await database_api.db_query(SampleRecord, query_type="count")
        updated = await database_api.db_query(
            SampleRecord,
            query_type="update",
            filters={"name": "alpha"},
            data={"count": 5},
        )
        deleted = await database_api.db_query(SampleRecord, query_type="delete", filters={"name": "beta"})
        invalid = await database_api.db_query(SampleRecord, query_type="bad")

        self.assertEqual(created["name"], "alpha")
        self.assertEqual(latest[0]["name"], "beta")
        self.assertEqual([record["name"] for record in alphabetical], ["alpha", "beta"])
        self.assertEqual(single["count"], 1)
        self.assertEqual(count, 2)
        self.assertEqual(updated, 1)
        self.assertEqual(deleted, 1)
        self.assertIsNone(invalid)
        self.assertEqual(await database_api.db_query(SampleRecord, query_type="count"), 1)
        self.assertEqual(SampleRecord.get(SampleRecord.name == "alpha").count, 5)

    async def test_db_query_returns_default_values_on_missing_data_and_database_errors(self) -> None:
        self.assertIsNone(await database_api.db_query(SampleRecord, query_type="create"))
        self.assertIsNone(await database_api.db_query(SampleRecord, query_type="update"))
        self.assertEqual(
            await database_api.db_query(SampleRecord, query_type="get", filters={"missing_field": "x"}),
            [],
        )
        self.assertIsNone(
            await database_api.db_query(
                SampleRecord,
                query_type="get",
                filters={"missing_field": "x"},
                single_result=True,
            )
        )
        self.assertIsNone(await database_api.db_query(SampleRecord, query_type="count", filters={"missing_field": "x"}))

        fake_missing = Mock()
        fake_missing.dicts.return_value.get.side_effect = database_api.DoesNotExist()
        fake_select = Mock()
        fake_select.where.return_value = fake_missing
        fake_model = SimpleNamespace(
            create=Mock(return_value=SimpleNamespace(id=1)),
            select=Mock(return_value=fake_select),
            id=object(),
        )
        self.assertEqual(await database_api.db_query(fake_model, query_type="create", data={"name": "x"}), [])

        class OneTimeAllowedQueryType:
            def __init__(self):
                self.allowed_once = True

            def __eq__(self, other):
                if self.allowed_once and other == "get":
                    self.allowed_once = False
                    return True
                return False

        self.assertIsNone(await database_api.db_query(SampleRecord, query_type=OneTimeAllowedQueryType()))

    async def test_db_save_creates_or_updates_by_key_and_db_get_orders_results(self) -> None:
        created = await database_api.db_save(SampleRecord, {"name": "alpha", "count": 1})
        updated = await database_api.db_save(
            SampleRecord,
            {"name": "alpha", "count": 7},
            key_field="name",
            key_value="alpha",
        )
        await database_api.db_save(SampleRecord, {"name": "beta", "count": 2})

        ordered = await database_api.db_get(SampleRecord, order_by="-count")
        ascending = await database_api.db_get(SampleRecord, order_by="name", limit=1)
        single = await database_api.db_get(SampleRecord, filters={"name": "missing"}, single_result=True)

        self.assertEqual(created["count"], 1)
        self.assertEqual(updated["count"], 7)
        self.assertEqual([record["name"] for record in ordered], ["alpha", "beta"])
        self.assertEqual([record["name"] for record in ascending], ["alpha"])
        self.assertIsNone(single)

        self.assertIsNone(await database_api.db_save(SampleRecord, {"bad_field": "x"}))
        self.assertEqual(await database_api.db_get(SampleRecord, filters={"missing_field": "x"}), [])
        self.assertIsNone(
            await database_api.db_get(
                SampleRecord,
                filters={"missing_field": "x"},
                single_result=True,
            )
        )

    async def test_store_action_info_builds_action_record_and_delegates_to_db_save(self) -> None:
        saved = {"action_id": "thinking-1", "action_name": "reply"}
        chat_stream = SimpleNamespace(stream_id="stream-1", platform="qq")

        with (
            patch.object(database_api, "db_save", new=AsyncMock(return_value=saved)) as db_save,
            patch.object(database_api.time, "time", return_value=123.456),
        ):
            result = await database_api.store_action_info(
                chat_stream=chat_stream,
                action_build_into_prompt=True,
                action_prompt_display="display",
                action_done=False,
                thinking_id="thinking-1",
                action_data={"text": "hello"},
                action_name="reply",
                action_reasoning="reason",
            )

        data = db_save.await_args.kwargs["data"]
        self.assertIs(result, saved)
        self.assertEqual(data["action_id"], "thinking-1")
        self.assertEqual(data["time"], 123.456)
        self.assertEqual(json.loads(data["action_data"]), {"text": "hello"})
        self.assertFalse(data["action_done"])
        self.assertEqual(data["chat_info_stream_id"], "stream-1")
        self.assertEqual(data["chat_info_platform"], "qq")

    async def test_store_action_info_uses_defaults_and_returns_none_on_save_or_import_errors(self) -> None:
        with (
            patch.object(database_api, "db_save", new=AsyncMock(return_value=None)) as db_save,
            patch.object(database_api.time, "time", return_value=123.456),
        ):
            self.assertIsNone(await database_api.store_action_info(action_name="idle"))

        data = db_save.await_args.kwargs["data"]
        self.assertEqual(data["action_id"], "123456000")
        self.assertEqual(json.loads(data["action_data"]), {})
        self.assertEqual(data["chat_id"], "")
        self.assertEqual(data["chat_info_stream_id"], "")
        self.assertEqual(data["chat_info_platform"], "")

        with patch.object(database_api.json, "dumps", side_effect=RuntimeError("json failed")):
            self.assertIsNone(await database_api.store_action_info(action_name="broken"))


class MessageApiTest(unittest.IsolatedAsyncioTestCase):
    def test_message_query_wrappers_validate_inputs_and_delegate_to_builders(self) -> None:
        sentinel = [object()]

        with patch.object(message_api, "get_raw_msg_by_timestamp", return_value=sentinel) as get_raw:
            self.assertIs(message_api.get_messages_by_time(1.0, 2.0, limit=3, limit_mode="earliest"), sentinel)
        get_raw.assert_called_once_with(1.0, 2.0, 3, "earliest")

        with patch.object(message_api, "get_raw_msg_by_timestamp_with_chat", return_value=sentinel) as get_chat:
            self.assertIs(
                message_api.get_messages_by_time_in_chat(
                    "chat-1",
                    1.0,
                    2.0,
                    limit=5,
                    filter_mai=True,
                    filter_command=True,
                    filter_intercept_message_level=2,
                ),
                sentinel,
            )
        get_chat.assert_called_once_with(
            chat_id="chat-1",
            timestamp_start=1.0,
            timestamp_end=2.0,
            limit=5,
            limit_mode="latest",
            filter_bot=True,
            filter_command=True,
            filter_intercept_message_level=2,
        )

        invalid_cases = [
            lambda: message_api.get_messages_by_time("bad", 2.0),
            lambda: message_api.get_messages_by_time(1.0, 2.0, limit=-1),
            lambda: message_api.get_messages_by_time_in_chat("", 1.0, 2.0),
            lambda: message_api.get_messages_by_time_in_chat(123, 1.0, 2.0),
        ]
        for call in invalid_cases:
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    call()

    def test_recent_count_format_and_filter_helpers_delegate_to_underlying_functions(self) -> None:
        messages = [
            SimpleNamespace(user_info=SimpleNamespace(platform="qq", user_id="bot")),
            SimpleNamespace(user_info=SimpleNamespace(platform="qq", user_id="user")),
        ]

        with (
            patch.object(message_api.time, "time", return_value=1000.0),
            patch.object(message_api, "get_raw_msg_by_timestamp_with_chat", return_value=["recent"]) as get_chat,
        ):
            self.assertEqual(message_api.get_recent_messages("chat-1", hours=1.5, limit=7), ["recent"])
        get_chat.assert_called_once_with("chat-1", 1000.0 - 1.5 * 3600, 1000.0, 7, "latest")

        with patch.object(message_api, "num_new_messages_since", return_value=4) as count:
            self.assertEqual(message_api.count_new_messages("chat-1", 10.0, 20.0), 4)
        count.assert_called_once_with("chat-1", 10.0, 20.0)

        with patch.object(message_api, "build_readable_messages", return_value="readable") as build:
            self.assertEqual(message_api.build_readable_messages_to_str(messages, truncate=True), "readable")
        build.assert_called_once_with(messages, True, "relative", 0.0, True, False)

        with patch.object(message_api, "is_bot_self", side_effect=[True, False]):
            self.assertEqual(message_api.filter_mai_messages(messages), [messages[1]])

        with patch.object(message_api.Images, "get_or_none", return_value=SimpleNamespace(description=" cat ")):
            self.assertEqual(message_api.translate_pid_to_description("img-1"), "cat")
        with patch.object(message_api.Images, "get_or_none", return_value=None):
            self.assertEqual(message_api.translate_pid_to_description("missing"), "[图片]")

    async def test_async_message_format_helpers_delegate(self) -> None:
        messages = [{"user_id": "u1"}]
        details = ("text", [(1.0, "Alice", "hello")])

        with patch.object(
            message_api, "build_readable_messages_with_list", new=AsyncMock(return_value=details)
        ) as build:
            self.assertIs(await message_api.build_readable_messages_with_details(messages), details)
        build.assert_awaited_once_with(messages, True, "relative", False)

        with patch.object(message_api, "get_person_id_list", new=AsyncMock(return_value=["u1"])) as get_ids:
            self.assertEqual(await message_api.get_person_ids_from_messages(messages), ["u1"])
        get_ids.assert_awaited_once_with(messages)


class ChatApiTest(unittest.TestCase):
    def test_stream_queries_filter_by_platform_type_and_validate_inputs(self) -> None:
        group_qq = make_api_stream("group-qq", user_id="u1", group_id="g1")
        private_qq = make_api_stream("private-qq", user_id="u2", group_id=None)
        private_wx = make_api_stream("private-wx", platform="wx", user_id="u3", group_id=None)
        manager = SimpleNamespace(streams={s.stream_id: s for s in [group_qq, private_qq, private_wx]})

        with patch.object(chat_api, "get_chat_manager", return_value=manager):
            self.assertEqual(chat_api.get_all_streams("qq"), [group_qq, private_qq])
            self.assertEqual(
                chat_api.get_all_streams(chat_api.SpecialTypes.ALL_PLATFORMS), [group_qq, private_qq, private_wx]
            )
            self.assertEqual(chat_api.get_group_streams("qq"), [group_qq])
            self.assertEqual(
                chat_api.get_private_streams(chat_api.SpecialTypes.ALL_PLATFORMS), [private_qq, private_wx]
            )
            self.assertIs(chat_api.get_stream_by_group_id("g1", "qq"), group_qq)
            self.assertIs(chat_api.get_stream_by_user_id("u3", "wx"), private_wx)
            self.assertEqual(
                chat_api.get_streams_summary(),
                {"total_streams": 3, "group_streams": 1, "private_streams": 2, "qq_streams": 2},
            )

        invalid_calls = [
            lambda: chat_api.get_all_streams(123),
            lambda: chat_api.get_group_streams(123),
            lambda: chat_api.get_private_streams(123),
            lambda: chat_api.get_stream_by_group_id("", "qq"),
            lambda: chat_api.get_stream_by_group_id(123, "qq"),
            lambda: chat_api.get_stream_by_group_id("g1", 123),
            lambda: chat_api.get_stream_by_user_id("", "qq"),
            lambda: chat_api.get_stream_by_user_id(123, "qq"),
            lambda: chat_api.get_stream_by_user_id("u1", 123),
        ]
        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises((TypeError, ValueError)):
                    call()

        with patch.object(chat_api, "get_chat_manager", return_value=manager):
            self.assertIsNone(chat_api.get_stream_by_group_id("missing", "qq"))
            self.assertIsNone(chat_api.get_stream_by_user_id("missing", "qq"))

    def test_stream_queries_return_empty_or_default_values_when_manager_raises(self) -> None:
        def raise_manager():
            raise RuntimeError("manager unavailable")

        with patch.object(chat_api, "get_chat_manager", side_effect=raise_manager):
            self.assertEqual(chat_api.get_all_streams("qq"), [])
            self.assertEqual(chat_api.get_group_streams("qq"), [])
            self.assertEqual(chat_api.get_private_streams("qq"), [])
            self.assertIsNone(chat_api.get_stream_by_group_id("g1", "qq"))
            self.assertIsNone(chat_api.get_stream_by_user_id("u1", "qq"))

        with patch.object(chat_api.ChatManager, "get_all_streams", side_effect=RuntimeError("boom")):
            self.assertEqual(
                chat_api.get_streams_summary(),
                {"total_streams": 0, "group_streams": 0, "private_streams": 0, "qq_streams": 0},
            )

    def test_stream_type_and_info_expose_group_private_and_user_fields(self) -> None:
        group = make_api_stream("group-qq", user_id="u1", group_id="g1")
        private = make_api_stream("private-qq", user_id="u2", group_id=None)

        self.assertEqual(chat_api.get_stream_type(group), "group")
        self.assertEqual(chat_api.get_stream_type(private), "private")
        unknown = make_api_stream("unknown", user_id="u9", group_id=None)
        delattr(unknown, "group_info")
        self.assertEqual(chat_api.get_stream_type(unknown), "unknown")
        self.assertEqual(
            chat_api.get_stream_info(group),
            {
                "stream_id": "group-qq",
                "platform": "qq",
                "type": "group",
                "group_id": "g1",
                "group_name": "Group g1",
                "user_id": "u1",
                "user_name": "User u1",
            },
        )
        self.assertEqual(
            chat_api.get_stream_info(private),
            {
                "stream_id": "private-qq",
                "platform": "qq",
                "type": "private",
                "user_id": "u2",
                "user_name": "User u2",
            },
        )
        with self.assertRaises(TypeError):
            chat_api.get_stream_type(object())
        with self.assertRaises(ValueError):
            chat_api.get_stream_info(None)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            chat_api.get_stream_info(object())  # type: ignore[arg-type]

        class FalsyChatStream(ChatStream):
            def __bool__(self):
                return False

        falsy = FalsyChatStream(
            stream_id="falsy",
            platform="qq",
            user_info=UserInfo(platform="qq", user_id="u4", user_nickname="Falsy", user_cardname="Falsy"),
            group_info=None,
            data={},
        )
        with self.assertRaises(ValueError):
            chat_api.get_stream_type(falsy)

        broken = make_api_stream("broken", user_id="u5", group_id=None)
        broken.user_info = SimpleNamespace(user_id="u5")
        self.assertEqual(chat_api.get_stream_info(broken), {})

    def test_module_level_chat_helpers_delegate_to_manager_methods(self) -> None:
        stream = make_api_stream("stream")

        with (
            patch.object(chat_api.ChatManager, "get_all_streams", return_value=[stream]) as all_streams,
            patch.object(chat_api.ChatManager, "get_group_streams", return_value=[stream]) as group_streams,
            patch.object(chat_api.ChatManager, "get_private_streams", return_value=[]) as private_streams,
            patch.object(chat_api.ChatManager, "get_group_stream_by_group_id", return_value=stream) as by_group,
            patch.object(chat_api.ChatManager, "get_private_stream_by_user_id", return_value=stream) as by_user,
            patch.object(chat_api.ChatManager, "get_stream_type", return_value="group") as stream_type,
            patch.object(chat_api.ChatManager, "get_stream_info", return_value={"stream_id": "stream"}) as stream_info,
            patch.object(chat_api.ChatManager, "get_streams_summary", return_value={"total_streams": 1}) as summary,
        ):
            self.assertEqual(chat_api.get_all_streams("qq"), [stream])
            self.assertEqual(chat_api.get_group_streams("qq"), [stream])
            self.assertEqual(chat_api.get_private_streams("qq"), [])
            self.assertIs(chat_api.get_stream_by_group_id("group-1", "qq"), stream)
            self.assertIs(chat_api.get_stream_by_user_id("user-1", "qq"), stream)
            self.assertEqual(chat_api.get_stream_type(stream), "group")
            self.assertEqual(chat_api.get_stream_info(stream), {"stream_id": "stream"})
            self.assertEqual(chat_api.get_streams_summary(), {"total_streams": 1})

        all_streams.assert_called_once_with("qq")
        group_streams.assert_called_once_with("qq")
        private_streams.assert_called_once_with("qq")
        by_group.assert_called_once_with("group-1", "qq")
        by_user.assert_called_once_with("user-1", "qq")
        stream_type.assert_called_once_with(stream)
        stream_info.assert_called_once_with(stream)
        summary.assert_called_once_with()


class ConfigAndFrequencyApiTest(unittest.TestCase):
    def test_config_api_reads_nested_global_plugin_values_and_returns_defaults(self) -> None:
        fake_global = SimpleNamespace(chat=SimpleNamespace(talk_value=0.6), feature=SimpleNamespace(enabled=True))
        plugin_config = {"section": {"nested": SimpleNamespace(value=3)}, "plain": "ok"}

        with patch.object(config_api, "global_config", fake_global):
            self.assertEqual(config_api.get_global_config("chat.talk_value"), 0.6)
            self.assertTrue(config_api.get_global_config("feature.enabled"))
            self.assertEqual(config_api.get_global_config("missing.key", default="fallback"), "fallback")

        self.assertEqual(config_api.get_plugin_config(plugin_config, "section.nested.value"), 3)
        self.assertEqual(config_api.get_plugin_config(plugin_config, "plain"), "ok")
        self.assertEqual(config_api.get_plugin_config(plugin_config, "section.missing", default="fallback"), "fallback")

    def test_frequency_api_delegates_to_frequency_control_and_chat_config(self) -> None:
        control = SimpleNamespace(
            get_talk_frequency_adjust=Mock(return_value=2.0),
            set_talk_frequency_adjust=Mock(),
        )
        manager = SimpleNamespace(get_or_create_frequency_control=Mock(return_value=control))
        fake_global = SimpleNamespace(chat=SimpleNamespace(get_talk_value=Mock(return_value=0.4)))

        with (
            patch.object(frequency_api, "frequency_control_manager", manager),
            patch.object(frequency_api, "global_config", fake_global),
        ):
            self.assertEqual(frequency_api.get_current_talk_value("stream-1"), 0.8)
            self.assertEqual(frequency_api.get_talk_frequency_adjust("stream-1"), 2.0)
            frequency_api.set_talk_frequency_adjust("stream-1", 1.5)

        self.assertEqual(manager.get_or_create_frequency_control.call_count, 3)
        fake_global.chat.get_talk_value.assert_called_once_with("stream-1")
        control.set_talk_frequency_adjust.assert_called_once_with(1.5)


class LLMApiTest(unittest.IsolatedAsyncioTestCase):
    def test_get_available_models_returns_only_task_configs(self) -> None:
        task = TaskConfig(model_list=["model-a"])
        fake_model_task_config = SimpleNamespace(replyer=task, plain_dict={}, failing_property="ignored")

        with patch.object(llm_api.model_config, "model_task_config", fake_model_task_config):
            self.assertEqual(llm_api.get_available_models(), {"replyer": task})

        class ModelContainer:
            replyer = task

            @property
            def broken(self):
                raise RuntimeError("broken attr")

        with patch.object(llm_api.model_config, "model_task_config", ModelContainer()):
            self.assertEqual(llm_api.get_available_models(), {"replyer": task})

        type(llm_api.model_config).model_task_config = PropertyMock(side_effect=RuntimeError("config unavailable"))
        try:
            self.assertEqual(llm_api.get_available_models(), {})
        finally:
            delattr(type(llm_api.model_config), "model_task_config")

    async def test_generate_wrappers_delegate_to_llm_request_and_unpack_success_values(self) -> None:
        class FakeLLMRequest:
            instances: list["FakeLLMRequest"] = []

            def __init__(self, model_set, request_type):
                self.model_set = model_set
                self.request_type = request_type
                self.calls = []
                FakeLLMRequest.instances.append(self)

            async def generate_response_async(self, prompt, **kwargs):
                self.calls.append(("prompt", prompt, kwargs))
                return "answer", ("reason", "model-a", ["tool-call"])

            async def generate_response_with_message_async(self, **kwargs):
                self.calls.append(("factory", kwargs))
                return "factory-answer", ("factory-reason", "model-b", ["factory-tool"])

        task = TaskConfig(model_list=["model-a"], max_tokens=256, temperature=0.5)
        factory = Mock(return_value=[])

        with patch.object(llm_api, "LLMRequest", FakeLLMRequest):
            self.assertEqual(
                await llm_api.generate_with_model(
                    "prompt", task, request_type="unit.generate", temperature=0.2, max_tokens=42
                ),
                (True, "answer", "reason", "model-a"),
            )
            self.assertEqual(
                await llm_api.generate_with_model_with_tools(
                    "prompt", task, tool_options=[{"name": "lookup"}], temperature=0.3, max_tokens=43
                ),
                (True, "answer", "reason", "model-a", ["tool-call"]),
            )
            self.assertEqual(
                await llm_api.generate_with_model_with_tools_by_message_factory(
                    factory, task, tool_options=[{"name": "lookup"}], temperature=0.4, max_tokens=44
                ),
                (True, "factory-answer", "factory-reason", "model-b", ["factory-tool"]),
            )

        self.assertEqual(FakeLLMRequest.instances[0].request_type, "unit.generate")
        self.assertEqual(
            FakeLLMRequest.instances[0].calls[0],
            ("prompt", "prompt", {"temperature": 0.2, "max_tokens": 42}),
        )
        self.assertEqual(
            FakeLLMRequest.instances[1].calls[0],
            ("prompt", "prompt", {"tools": [{"name": "lookup"}], "temperature": 0.3, "max_tokens": 43}),
        )
        self.assertEqual(
            FakeLLMRequest.instances[2].calls[0],
            (
                "factory",
                {"message_factory": factory, "tools": [{"name": "lookup"}], "temperature": 0.4, "max_tokens": 44},
            ),
        )

    async def test_generate_wrappers_return_error_tuple_when_request_fails(self) -> None:
        class FailingLLMRequest:
            def __init__(self, model_set, request_type):
                pass

            async def generate_response_async(self, prompt, **kwargs):
                raise RuntimeError("provider down")

            async def generate_response_with_message_async(self, **kwargs):
                raise RuntimeError("factory down")

        task = TaskConfig(model_list=["model-a"])
        with patch.object(llm_api, "LLMRequest", FailingLLMRequest):
            self.assertEqual(
                await llm_api.generate_with_model("prompt", task),
                (False, "生成内容时出错: provider down", "", ""),
            )
            self.assertEqual(
                await llm_api.generate_with_model_with_tools("prompt", task),
                (False, "生成内容时出错: provider down", "", "", None),
            )
            self.assertEqual(
                await llm_api.generate_with_model_with_tools_by_message_factory(Mock(), task),
                (False, "生成内容时出错: factory down", "", "", None),
            )


class GeneratorApiTest(unittest.IsolatedAsyncioTestCase):
    def test_get_replyer_delegates_to_manager_and_handles_invalid_or_failing_lookup(self) -> None:
        sentinel = object()
        with patch.object(generator_api.replyer_manager, "get_replyer", return_value=sentinel) as get_replyer:
            self.assertIs(generator_api.get_replyer(chat_id="chat-1", request_type="unit.replyer"), sentinel)
        get_replyer.assert_called_once_with(chat_stream=None, chat_id="chat-1", request_type="unit.replyer")

        with self.assertRaises(ValueError):
            generator_api.get_replyer()

        with patch.object(generator_api.replyer_manager, "get_replyer", side_effect=RuntimeError("missing stream")):
            self.assertIsNone(generator_api.get_replyer(chat_id="chat-1"))

    async def test_generate_reply_merges_action_data_processes_output_and_logs_final_reply(self) -> None:
        chat_stream = SimpleNamespace(stream_id="stream-1")
        llm_response = SimpleNamespace(
            content="raw response",
            prompt="prompt text",
            processed_output=None,
            reply_set=None,
            model="model-a",
            timing={"total": 1.2},
            reasoning="reasoning text",
        )
        replyer = SimpleNamespace(generate_reply_with_context=AsyncMock(return_value=(True, llm_response)))

        with (
            patch.object(generator_api, "get_replyer", return_value=replyer) as get_replyer,
            patch.object(generator_api, "process_llm_response", return_value=["hello", "world"]) as process_response,
            patch.object(generator_api.PlanReplyLogger, "log_reply") as log_reply,
        ):
            success, response = await generator_api.generate_reply(
                chat_stream=chat_stream,
                action_data={
                    "extra_info": "from action",
                    "reason": "because",
                    "unknown_words": [" yyds ", "", 3, "CPU"],
                },
                think_level=2,
                available_actions={"reply": object()},
                chosen_actions=["chosen"],
                enable_tool=True,
                enable_splitter=False,
                enable_chinese_typo=False,
                request_type="unit.generator",
                from_plugin=False,
                reply_time_point=123.0,
            )

        self.assertTrue(success)
        self.assertIs(response, llm_response)
        get_replyer.assert_called_once_with(chat_stream, None, request_type="unit.generator")
        replyer.generate_reply_with_context.assert_awaited_once()
        kwargs = replyer.generate_reply_with_context.await_args.kwargs
        self.assertEqual(kwargs["extra_info"], "from action")
        self.assertEqual(kwargs["reply_reason"], "because")
        self.assertEqual(kwargs["unknown_words"], ["yyds", "CPU"])
        self.assertTrue(kwargs["enable_tool"])
        self.assertFalse(kwargs["from_plugin"])
        self.assertEqual(kwargs["think_level"], 2)
        self.assertEqual(kwargs["stream_id"], "stream-1")
        self.assertEqual(kwargs["reply_time_point"], 123.0)
        self.assertFalse(kwargs["log_reply"])
        process_response.assert_called_once_with("raw response", False, False)
        self.assertEqual(llm_response.processed_output, ["hello", "world"])
        self.assertEqual([item.content for item in llm_response.reply_set.reply_data], ["hello", "world"])
        log_reply.assert_called_once_with(
            chat_id="stream-1",
            prompt="prompt text",
            output="raw response",
            processed_output=["hello", "world"],
            model="model-a",
            timing={"total": 1.2},
            reasoning="reasoning text",
            think_level=2,
            success=True,
        )

    async def test_generate_and_rewrite_reply_return_false_when_replyer_or_generation_fails(self) -> None:
        with patch.object(generator_api, "get_replyer", return_value=None):
            self.assertEqual(await generator_api.generate_reply(chat_id="chat-1"), (False, None))
            self.assertEqual(await generator_api.rewrite_reply(chat_id="chat-1"), (False, None))

        warning_replyer = SimpleNamespace(generate_reply_with_context=AsyncMock(side_effect=UserWarning("paused")))
        with patch.object(generator_api, "get_replyer", return_value=warning_replyer):
            self.assertEqual(await generator_api.generate_reply(chat_id="chat-1"), (False, None))

        failed_replyer = SimpleNamespace(generate_reply_with_context=AsyncMock(return_value=(False, None)))
        with patch.object(generator_api, "get_replyer", return_value=failed_replyer):
            self.assertEqual(await generator_api.generate_reply(chat_id="chat-1"), (False, None))

        exploding_replyer = SimpleNamespace(generate_reply_with_context=AsyncMock(side_effect=RuntimeError("llm down")))
        with patch.object(generator_api, "get_replyer", return_value=exploding_replyer):
            self.assertEqual(await generator_api.generate_reply(chat_id="chat-1"), (False, None))

        value_error_replyer = SimpleNamespace(
            generate_reply_with_context=AsyncMock(side_effect=ValueError("bad input"))
        )
        with patch.object(generator_api, "get_replyer", return_value=value_error_replyer):
            with self.assertRaisesRegex(ValueError, "bad input"):
                await generator_api.generate_reply(chat_id="chat-1")

    async def test_generate_reply_handles_empty_content_and_reply_log_failures(self) -> None:
        llm_response = SimpleNamespace(
            content="",
            prompt=None,
            processed_output=None,
            reply_set=None,
            model="model-a",
            timing={},
            reasoning=None,
        )
        replyer = SimpleNamespace(generate_reply_with_context=AsyncMock(return_value=(True, llm_response)))

        with (
            patch.object(generator_api, "get_replyer", return_value=replyer),
            patch.object(
                generator_api.PlanReplyLogger, "log_reply", side_effect=RuntimeError("disk full")
            ) as log_reply,
        ):
            success, response = await generator_api.generate_reply(chat_id="chat-1", reply_time_point=123.0)

        self.assertTrue(success)
        self.assertIs(response, llm_response)
        self.assertIsNone(llm_response.reply_set)
        log_reply.assert_called_once()

    async def test_rewrite_reply_fills_legacy_reply_data_and_processes_successful_content(self) -> None:
        llm_response = SimpleNamespace(content="rewritten", reply_set=None)
        replyer = SimpleNamespace(rewrite_reply_with_context=AsyncMock(return_value=(True, llm_response)))

        with (
            patch.object(generator_api, "get_replyer", return_value=replyer) as get_replyer,
            patch.object(generator_api, "process_llm_response", return_value=["rewritten part"]) as process_response,
        ):
            success, response = await generator_api.rewrite_reply(
                chat_id="chat-1",
                reply_data={"raw_reply": "raw text", "reason": "need polish", "reply_to": "Alice: hi"},
                enable_splitter=True,
                enable_chinese_typo=False,
                request_type="unit.rewrite",
            )

        self.assertTrue(success)
        self.assertIs(response, llm_response)
        get_replyer.assert_called_once_with(None, "chat-1", request_type="unit.rewrite")
        replyer.rewrite_reply_with_context.assert_awaited_once_with(
            raw_reply="raw text",
            reason="need polish",
            reply_to="Alice: hi",
        )
        process_response.assert_called_once_with("rewritten", True, False)
        self.assertEqual([item.content for item in llm_response.reply_set.reply_data], ["rewritten part"])

    async def test_rewrite_reply_handles_failure_exception_and_value_error_paths(self) -> None:
        failed_response = SimpleNamespace(content="", reply_set=None)
        failed_replyer = SimpleNamespace(rewrite_reply_with_context=AsyncMock(return_value=(False, failed_response)))
        with patch.object(generator_api, "get_replyer", return_value=failed_replyer):
            success, response = await generator_api.rewrite_reply(chat_id="chat-1")
        self.assertFalse(success)
        self.assertIs(response, failed_response)
        self.assertIsNone(failed_response.reply_set)

        none_replyer = SimpleNamespace(rewrite_reply_with_context=AsyncMock(return_value=(True, None)))
        with patch.object(generator_api, "get_replyer", return_value=none_replyer):
            self.assertEqual(await generator_api.rewrite_reply(chat_id="chat-1"), (False, None))

        exploding_replyer = SimpleNamespace(rewrite_reply_with_context=AsyncMock(side_effect=RuntimeError("llm down")))
        with patch.object(generator_api, "get_replyer", return_value=exploding_replyer):
            self.assertEqual(await generator_api.rewrite_reply(chat_id="chat-1"), (False, None))

        value_error_replyer = SimpleNamespace(
            rewrite_reply_with_context=AsyncMock(side_effect=ValueError("bad rewrite"))
        )
        with patch.object(generator_api, "get_replyer", return_value=value_error_replyer):
            with self.assertRaisesRegex(ValueError, "bad rewrite"):
                await generator_api.rewrite_reply(chat_id="chat-1")

    def test_process_human_text_validates_content_and_returns_none_when_processing_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "content 必须是字符串类型"):
            generator_api.process_human_text(123, True, True)

        with patch.object(generator_api, "process_llm_response", side_effect=RuntimeError("split failed")):
            self.assertIsNone(generator_api.process_human_text("hello", True, True))

    async def test_generate_response_custom_returns_content_only_when_replyer_succeeds(self) -> None:
        replyer = SimpleNamespace(llm_generate_content=AsyncMock(return_value=("custom text", "reason", "model", None)))

        with patch.object(generator_api, "get_replyer", return_value=replyer) as get_replyer:
            self.assertEqual(
                await generator_api.generate_response_custom(chat_id="chat-1", request_type="unit.custom", prompt="hi"),
                "custom text",
            )

        get_replyer.assert_called_once_with(None, "chat-1", request_type="unit.custom")
        replyer.llm_generate_content.assert_awaited_once_with("hi")

        empty_replyer = SimpleNamespace(llm_generate_content=AsyncMock(return_value=("", "reason", "model", None)))
        with patch.object(generator_api, "get_replyer", return_value=empty_replyer):
            self.assertIsNone(await generator_api.generate_response_custom(chat_id="chat-1"))

        with patch.object(generator_api, "get_replyer", return_value=None):
            self.assertIsNone(await generator_api.generate_response_custom(chat_id="chat-1"))

        failing_replyer = SimpleNamespace(llm_generate_content=AsyncMock(side_effect=RuntimeError("provider down")))
        with patch.object(generator_api, "get_replyer", return_value=failing_replyer):
            self.assertIsNone(await generator_api.generate_response_custom(chat_id="chat-1"))


class ComponentToolPluginPersonApiTest(unittest.IsolatedAsyncioTestCase):
    def test_logging_api_re_exports_common_get_logger_for_plugin_authors(self) -> None:
        api_package = importlib.import_module("src.plugin_system.apis")

        self.assertEqual(logging_api.__all__, ["get_logger"])
        self.assertIs(logging_api.get_logger, common_get_logger)
        self.assertIs(api_package.get_logger, common_get_logger)

    def test_plugin_register_decorator_records_base_plugin_classes_and_rejects_invalid_names(self) -> None:
        manager_module = importlib.import_module("src.plugin_system.core.plugin_manager")
        manager = SimpleNamespace(plugin_classes={}, plugin_paths={})

        class DecoratedPlugin(BasePlugin):
            plugin_name = "decorated_plugin"
            enable_plugin = True
            dependencies = []
            python_dependencies = []
            config_file_name = ""
            config_schema = {}

            def get_plugin_components(self):
                return []

        class PlainPlugin:
            plugin_name = "plain_plugin"

        class DottedPlugin(DecoratedPlugin):
            plugin_name = "bad.name"

        with patch.object(manager_module, "plugin_manager", manager):
            self.assertIs(plugin_register_api.register_plugin(DecoratedPlugin), DecoratedPlugin)
            self.assertIs(plugin_register_api.register_plugin(PlainPlugin), PlainPlugin)
            with self.assertRaisesRegex(ValueError, "包含非法字符"):
                plugin_register_api.register_plugin(DottedPlugin)

        self.assertIs(manager.plugin_classes["decorated_plugin"], DecoratedPlugin)
        self.assertTrue(manager.plugin_paths["decorated_plugin"].endswith("test_plugin_apis"))
        self.assertNotIn("plain_plugin", manager.plugin_classes)
        self.assertNotIn("bad.name", manager.plugin_classes)

        missing_root_manager = SimpleNamespace(plugin_classes={}, plugin_paths={})
        with (
            patch.object(manager_module, "plugin_manager", missing_root_manager),
            patch.object(plugin_register_api.Path, "exists", return_value=False),
        ):
            self.assertIs(plugin_register_api.register_plugin(DecoratedPlugin), DecoratedPlugin)
        self.assertEqual(missing_root_manager.plugin_classes, {})

    def test_plugin_register_decorator_rejects_same_internal_name_from_another_directory(self) -> None:
        manager_module = importlib.import_module("src.plugin_system.core.plugin_manager")
        manager = SimpleNamespace(plugin_classes={}, plugin_paths={})

        class FirstPlugin(BasePlugin):
            plugin_name = "shared_plugin"
            enable_plugin = True
            dependencies = []
            python_dependencies = []
            config_file_name = ""
            config_schema = {}

            def get_plugin_components(self):
                return []

        class SecondPlugin(FirstPlugin):
            pass

        with patch.object(manager_module, "plugin_manager", manager):
            plugin_register_api.register_plugin(FirstPlugin)
            manager.plugin_paths["shared_plugin"] = "/different/plugin/path"
            with self.assertRaisesRegex(ValueError, "已由其他插件目录注册"):
                plugin_register_api.register_plugin(SecondPlugin)

        self.assertIs(manager.plugin_classes["shared_plugin"], FirstPlugin)

    def test_tool_api_instantiates_tool_with_plugin_config_and_lists_definitions(self) -> None:
        class FakeTool:
            def __init__(self, plugin_config=None, chat_stream=None):
                self.plugin_config = plugin_config
                self.chat_stream = chat_stream

            @classmethod
            def get_tool_definition(cls):
                return {"name": "lookup"}

        core_module = importlib.import_module("src.plugin_system.core")
        registry = SimpleNamespace(
            get_component_info=Mock(return_value=SimpleNamespace(plugin_name="plugin-a")),
            get_plugin_config=Mock(return_value={"nested": {"value": 1}}),
            get_component_class=Mock(return_value=FakeTool),
            get_llm_available_tools=Mock(return_value={"lookup": FakeTool}),
        )
        chat_stream = make_api_stream("private-qq", group_id=None)

        with patch.object(core_module, "component_registry", registry):
            instance = tool_api.get_tool_instance("lookup", chat_stream)
            self.assertEqual(tool_api.get_llm_available_tool_definitions(), [("lookup", {"name": "lookup"})])

        self.assertIsInstance(instance, FakeTool)
        self.assertEqual(instance.plugin_config, {"nested": {"value": 1}})
        self.assertIs(instance.chat_stream, chat_stream)
        registry.get_component_info.assert_called_once_with("lookup", ComponentType.TOOL)
        registry.get_component_class.assert_called_once_with("lookup", ComponentType.TOOL)

        registry.get_component_info = Mock(return_value=None)
        registry.get_plugin_config = Mock(return_value={"unused": True})
        with patch.object(core_module, "component_registry", registry):
            instance = tool_api.get_tool_instance("lookup", chat_stream)
        self.assertIsInstance(instance, FakeTool)
        self.assertIsNone(instance.plugin_config)
        registry.get_plugin_config.assert_not_called()

    async def test_component_manage_api_delegates_global_queries_and_local_switches(self) -> None:
        registry_module = importlib.import_module("src.plugin_system.core.component_registry")
        announcement_module = importlib.import_module("src.plugin_system.core.global_announcement_manager")
        registry = SimpleNamespace(
            get_all_plugins=Mock(return_value={"plugin-a": "info"}),
            get_plugin_info=Mock(return_value="plugin-info"),
            get_component_info=Mock(return_value="component-info"),
            get_components_by_type=Mock(return_value={"act": "info"}),
            get_enabled_components_by_type=Mock(return_value={"act": "info"}),
            get_registered_action_info=Mock(return_value="action-info"),
            get_registered_command_info=Mock(return_value="command-info"),
            get_registered_tool_info=Mock(return_value="tool-info"),
            get_registered_event_handler_info=Mock(return_value="event-info"),
            enable_component=Mock(return_value=True),
            disable_component=AsyncMock(return_value=True),
        )
        announcement = SimpleNamespace(
            enable_specific_chat_action=Mock(return_value=True),
            enable_specific_chat_command=Mock(return_value=True),
            enable_specific_chat_tool=Mock(return_value=True),
            enable_specific_chat_event_handler=Mock(return_value=True),
            disable_specific_chat_action=Mock(return_value=True),
            disable_specific_chat_command=Mock(return_value=True),
            disable_specific_chat_tool=Mock(return_value=True),
            disable_specific_chat_event_handler=Mock(return_value=True),
            get_disabled_chat_actions=Mock(return_value=["act"]),
            get_disabled_chat_commands=Mock(return_value=["cmd"]),
            get_disabled_chat_tools=Mock(return_value=["tool"]),
            get_disabled_chat_event_handlers=Mock(return_value=["handler"]),
        )

        with (
            patch.object(registry_module, "component_registry", registry),
            patch.object(announcement_module, "global_announcement_manager", announcement),
        ):
            self.assertEqual(component_manage_api.get_all_plugin_info(), {"plugin-a": "info"})
            self.assertEqual(component_manage_api.get_plugin_info("plugin-a"), "plugin-info")
            self.assertEqual(component_manage_api.get_component_info("act", ComponentType.ACTION), "component-info")
            self.assertEqual(component_manage_api.get_components_info_by_type(ComponentType.ACTION), {"act": "info"})
            self.assertEqual(
                component_manage_api.get_enabled_components_info_by_type(ComponentType.ACTION), {"act": "info"}
            )
            self.assertEqual(component_manage_api.get_registered_action_info("act"), "action-info")
            self.assertEqual(component_manage_api.get_registered_command_info("cmd"), "command-info")
            self.assertEqual(component_manage_api.get_registered_tool_info("tool"), "tool-info")
            self.assertEqual(component_manage_api.get_registered_event_handler_info("handler"), "event-info")
            self.assertTrue(component_manage_api.globally_enable_component("act", ComponentType.ACTION))
            self.assertTrue(await component_manage_api.globally_disable_component("act", ComponentType.ACTION))
            self.assertTrue(component_manage_api.locally_enable_component("act", ComponentType.ACTION, "stream-1"))
            self.assertTrue(component_manage_api.locally_enable_component("cmd", ComponentType.COMMAND, "stream-1"))
            self.assertTrue(component_manage_api.locally_enable_component("tool", ComponentType.TOOL, "stream-1"))
            self.assertTrue(
                component_manage_api.locally_enable_component("handler", ComponentType.EVENT_HANDLER, "stream-1")
            )
            self.assertTrue(component_manage_api.locally_disable_component("act", ComponentType.ACTION, "stream-1"))
            self.assertTrue(component_manage_api.locally_disable_component("cmd", ComponentType.COMMAND, "stream-1"))
            self.assertTrue(component_manage_api.locally_disable_component("tool", ComponentType.TOOL, "stream-1"))
            self.assertTrue(
                component_manage_api.locally_disable_component("handler", ComponentType.EVENT_HANDLER, "stream-1")
            )
            self.assertEqual(
                component_manage_api.get_locally_disabled_components("stream-1", ComponentType.ACTION), ["act"]
            )
            self.assertEqual(
                component_manage_api.get_locally_disabled_components("stream-1", ComponentType.COMMAND), ["cmd"]
            )
            self.assertEqual(
                component_manage_api.get_locally_disabled_components("stream-1", ComponentType.TOOL), ["tool"]
            )
            self.assertEqual(
                component_manage_api.get_locally_disabled_components("stream-1", ComponentType.EVENT_HANDLER),
                ["handler"],
            )
            with self.assertRaises(ValueError):
                component_manage_api.locally_enable_component("x", "bad", "stream-1")  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                component_manage_api.locally_disable_component("x", "bad", "stream-1")  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                component_manage_api.get_locally_disabled_components("stream-1", "bad")  # type: ignore[arg-type]

    def test_component_manage_api_annotations_include_tool_info(self) -> None:
        def contains_type(annotation, expected_type) -> bool:
            return annotation is expected_type or any(
                contains_type(argument, expected_type) for argument in get_args(annotation)
            )

        for function in (
            component_manage_api.get_component_info,
            component_manage_api.get_components_info_by_type,
            component_manage_api.get_enabled_components_info_by_type,
        ):
            return_annotation = get_type_hints(function)["return"]
            self.assertTrue(contains_type(return_annotation, ToolInfo), function.__name__)

    async def test_plugin_manage_api_delegates_to_plugin_manager_and_reports_missing_path(self) -> None:
        manager_module = importlib.import_module("src.plugin_system.core.plugin_manager")
        manager = SimpleNamespace(
            list_loaded_plugins=Mock(return_value=["loaded"]),
            list_registered_plugins=Mock(return_value=["registered"]),
            get_plugin_path=Mock(side_effect=lambda name: "/plugins/loaded" if name == "loaded" else ""),
            remove_registered_plugin=AsyncMock(return_value=True),
            reload_registered_plugin=AsyncMock(return_value=True),
            load_registered_plugin_classes=Mock(return_value=(True, 1)),
            add_plugin_directory=Mock(return_value=True),
            rescan_plugin_directory=Mock(return_value=(2, 0)),
        )

        with patch.object(manager_module, "plugin_manager", manager):
            self.assertEqual(plugin_manage_api.list_loaded_plugins(), ["loaded"])
            self.assertEqual(plugin_manage_api.list_registered_plugins(), ["registered"])
            self.assertEqual(plugin_manage_api.get_plugin_path("loaded"), "/plugins/loaded")
            with self.assertRaisesRegex(ValueError, "插件 'missing' 不存在"):
                plugin_manage_api.get_plugin_path("missing")
            self.assertTrue(await plugin_manage_api.remove_plugin("loaded"))
            self.assertTrue(await plugin_manage_api.reload_plugin("loaded"))
            self.assertEqual(plugin_manage_api.load_plugin("loaded"), (True, 1))
            self.assertTrue(plugin_manage_api.add_plugin_directory("/plugins"))
            self.assertEqual(plugin_manage_api.rescan_plugin_directory(), (2, 0))

    async def test_person_api_uses_person_model_and_falls_back_on_errors(self) -> None:
        class FakePerson:
            def __init__(self, platform=None, user_id=None, person_id=None, person_name=None):
                if user_id == "bad" or person_id == "bad" or person_name == "bad":
                    raise RuntimeError("person unavailable")
                self.person_id = person_id or f"{platform}:{user_id or person_name}"
                self.nickname = "Alice" if person_id != "empty" else None

        with patch.object(person_api, "Person", FakePerson):
            self.assertEqual(person_api.get_person_id("qq", 123), "qq:123")
            self.assertEqual(await person_api.get_person_value("person-1", "nickname", default="unknown"), "Alice")
            self.assertEqual(await person_api.get_person_value("empty", "nickname", default="unknown"), "unknown")
            self.assertEqual(person_api.get_person_id_by_name("Alice"), "None:Alice")
            self.assertEqual(person_api.get_person_id("qq", "bad"), "")
            self.assertEqual(await person_api.get_person_value("bad", "nickname", default="unknown"), "unknown")
            self.assertEqual(person_api.get_person_id_by_name("bad"), "")


if __name__ == "__main__":
    unittest.main()
