import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from maim_message import GroupInfo, UserInfo

from src.chat.message_receive import chat_stream as chat_stream_module
from src.chat.message_receive.chat_stream import ChatManager, ChatMessageContext, ChatStream


def make_user(user_id: str = "user-1", nickname: str = "Alice") -> UserInfo:
    return UserInfo(platform="qq", user_id=user_id, user_nickname=nickname, user_cardname=f"{nickname}Card")


def make_group(group_id: str = "group-1", name: str = "Group") -> GroupInfo:
    return GroupInfo(platform="qq", group_id=group_id, group_name=name)


def make_stream(
    stream_id: str = "stream-1",
    *,
    user_info: UserInfo | None = None,
    group_info: GroupInfo | None = None,
    create_time: float = 1.0,
    last_active_time: float = 2.0,
) -> ChatStream:
    return ChatStream(
        stream_id=stream_id,
        platform="qq",
        user_info=user_info or make_user(),
        group_info=group_info,
        data={"create_time": create_time, "last_active_time": last_active_time},
    )


def make_manager() -> ChatManager:
    manager = object.__new__(ChatManager)
    manager.streams = {}
    manager.last_messages = {}
    return manager


async def run_to_thread(sync_func, *args, **kwargs):
    return sync_func(*args, **kwargs)


class ChatMessageContextEdgeTest(unittest.TestCase):
    def test_context_handles_default_template_empty_formats_and_missing_priority_info(self) -> None:
        message = SimpleNamespace(
            message_info=SimpleNamespace(
                template_info=SimpleNamespace(template_default=True, template_name="default"),
                format_info=SimpleNamespace(accept_format=""),
            ),
            priority_mode="normal",
        )
        context = ChatMessageContext(message)

        self.assertIs(context.get_last_message(), message)
        self.assertIsNone(context.get_template_name())
        self.assertFalse(context.check_types(["text"]))
        self.assertEqual(context.get_priority_mode(), "normal")
        self.assertIsNone(context.get_priority_info())

        message.message_info.template_info = None
        self.assertIsNone(context.get_template_name())

    def test_stream_set_context_wraps_latest_message(self) -> None:
        message = SimpleNamespace(message_id="msg-1")
        stream = make_stream()

        stream.set_context(message)

        self.assertIsInstance(stream.context, ChatMessageContext)
        self.assertIs(stream.context.get_last_message(), message)


class ChatManagerLookupTest(unittest.TestCase):
    def tearDown(self) -> None:
        ChatManager._instance = None
        ChatManager._initialized = False
        chat_stream_module.chat_manager = None

    def test_constructor_suppresses_database_initialization_errors(self) -> None:
        ChatManager._instance = None
        ChatManager._initialized = False

        with patch.object(chat_stream_module.db, "connect", side_effect=RuntimeError("db down")):
            manager = ChatManager()

        self.assertEqual(manager.streams, {})
        self.assertEqual(manager.last_messages, {})
        self.assertTrue(manager._initialized)

    def test_get_chat_manager_returns_process_singleton(self) -> None:
        ChatManager._instance = None
        ChatManager._initialized = False
        chat_stream_module.chat_manager = None

        with (
            patch.object(chat_stream_module.db, "connect"),
            patch.object(chat_stream_module.db, "create_tables"),
        ):
            first = chat_stream_module.get_chat_manager()
            second = chat_stream_module.get_chat_manager()

        self.assertIs(first, second)

    def test_register_message_and_stream_id_generation_cover_group_private_and_invalid_inputs(self) -> None:
        manager = make_manager()
        user_info = make_user()
        group_info = make_group()
        message = SimpleNamespace(
            message_info=SimpleNamespace(platform="qq", user_info=user_info, group_info=group_info)
        )

        manager.register_message(message)

        group_stream_id = ChatManager._generate_stream_id("qq", user_info, group_info)
        private_stream_id = ChatManager._generate_stream_id("qq", user_info, None)
        self.assertIs(manager.last_messages[group_stream_id], message)
        self.assertEqual(group_stream_id, manager.get_stream_id("qq", "group-1", is_group=True))
        self.assertEqual(private_stream_id, manager.get_stream_id("qq", "user-1", is_group=False))
        with self.assertRaisesRegex(ValueError, "必须提供"):
            ChatManager._generate_stream_id("qq", None, None)

    def test_stream_lookup_attaches_context_and_resolves_display_names(self) -> None:
        manager = make_manager()
        group_info = make_group(name="Named Group")
        group_user = make_user()
        group_stream_id = ChatManager._generate_stream_id("qq", group_user, group_info)
        group_stream = make_stream(group_stream_id, user_info=group_user, group_info=group_info)
        private_stream = make_stream("private-stream", user_info=make_user(nickname="Bob"), group_info=None)
        unnamed_stream = make_stream("unnamed-stream", user_info=make_user(nickname=""), group_info=None)
        last_message = SimpleNamespace(message_id="last")
        manager.streams = {
            group_stream.stream_id: group_stream,
            private_stream.stream_id: private_stream,
            unnamed_stream.stream_id: unnamed_stream,
        }
        manager.last_messages[group_stream.stream_id] = last_message

        self.assertIsNone(manager.get_stream("missing"))
        snapshot = manager.get_stream(group_stream.stream_id)
        self.assertIsNot(snapshot, group_stream)
        self.assertIs(snapshot.context.get_last_message(), last_message)
        self.assertIsNone(group_stream.context)
        self.assertEqual(
            manager.get_stream_by_info("qq", group_stream.user_info, group_stream.group_info).to_dict(),
            group_stream.to_dict(),
        )
        self.assertEqual(manager.get_stream_name(group_stream.stream_id), "Named Group")
        self.assertEqual(manager.get_stream_name(private_stream.stream_id), "Bob的私聊")
        self.assertIsNone(manager.get_stream_name(unnamed_stream.stream_id))
        self.assertIsNone(manager.get_stream_name("missing"))


class ChatManagerPersistenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_runs_load_all_streams_and_suppresses_load_errors(self) -> None:
        manager = make_manager()
        manager.load_all_streams = AsyncMock()

        await manager._initialize()
        manager.load_all_streams.assert_awaited_once()

        manager.load_all_streams = AsyncMock(side_effect=RuntimeError("db down"))
        await manager._initialize()
        manager.load_all_streams.assert_awaited_once()

    async def test_auto_save_task_saves_periodically_and_logs_save_errors(self) -> None:
        manager = make_manager()
        manager._save_all_streams = AsyncMock()

        with patch.object(
            chat_stream_module.asyncio, "sleep", new=AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with self.assertRaises(asyncio.CancelledError):
                await manager._auto_save_task()
        manager._save_all_streams.assert_awaited_once()

        manager._save_all_streams = AsyncMock(side_effect=RuntimeError("save down"))
        with patch.object(
            chat_stream_module.asyncio, "sleep", new=AsyncMock(side_effect=[None, asyncio.CancelledError])
        ):
            with self.assertRaises(asyncio.CancelledError):
                await manager._auto_save_task()
        manager._save_all_streams.assert_awaited_once()

    async def test_save_stream_skips_saved_streams_persists_fields_and_suppresses_database_errors(self) -> None:
        saved_stream = make_stream("saved-stream", group_info=make_group())
        saved_stream.saved = True

        with patch.object(chat_stream_module.ChatStreams, "replace") as replace:
            await ChatManager._save_stream(saved_stream)
        replace.assert_not_called()

        stream = make_stream("stream-1", user_info=make_user(), group_info=make_group())
        execute = Mock()
        replace_result = SimpleNamespace(execute=execute)
        with (
            patch.object(chat_stream_module.asyncio, "to_thread", new=run_to_thread),
            patch.object(chat_stream_module.ChatStreams, "replace", return_value=replace_result) as replace,
        ):
            await ChatManager._save_stream(stream)

        self.assertTrue(stream.saved)
        replace.assert_called_once_with(
            stream_id="stream-1",
            platform="qq",
            create_time=1.0,
            last_active_time=2.0,
            user_platform="qq",
            user_id="user-1",
            user_nickname="Alice",
            user_cardname="AliceCard",
            group_platform="qq",
            group_id="group-1",
            group_name="Group",
        )
        execute.assert_called_once_with()

        failing_stream = make_stream("failing-stream")
        with patch.object(chat_stream_module.asyncio, "to_thread", new=AsyncMock(side_effect=RuntimeError("db down"))):
            await ChatManager._save_stream(failing_stream)
        self.assertFalse(failing_stream.saved)

    async def test_save_all_streams_delegates_each_cached_stream(self) -> None:
        manager = make_manager()
        first = make_stream("first")
        second = make_stream("second")
        manager.streams = {first.stream_id: first, second.stream_id: second}
        manager._save_stream = AsyncMock()

        await manager._save_all_streams()

        self.assertEqual([call.args[0] for call in manager._save_stream.await_args_list], [first, second])

    async def test_load_all_streams_rebuilds_cache_marks_saved_and_attaches_existing_context(self) -> None:
        manager = make_manager()
        manager.last_messages = {"group-stream": SimpleNamespace(message_id="last")}
        group_model = SimpleNamespace(
            stream_id="group-stream",
            platform="qq",
            user_platform="qq",
            user_id="user-1",
            user_nickname="Alice",
            user_cardname=None,
            group_platform="qq",
            group_id="group-1",
            group_name="Group",
            create_time=1.0,
            last_active_time=2.0,
        )
        private_model = SimpleNamespace(
            stream_id="private-stream",
            platform="qq",
            user_platform="qq",
            user_id="user-2",
            user_nickname="Bob",
            user_cardname="Bobby",
            group_platform="",
            group_id="",
            group_name="",
            create_time=3.0,
            last_active_time=4.0,
        )

        with (
            patch.object(chat_stream_module.asyncio, "to_thread", new=run_to_thread),
            patch.object(chat_stream_module.ChatStreams, "select", return_value=[group_model, private_model]),
        ):
            await manager.load_all_streams()

        self.assertEqual(set(manager.streams), {"group-stream", "private-stream"})
        self.assertTrue(manager.streams["group-stream"].saved)
        self.assertEqual(manager.streams["group-stream"].group_info.group_name, "Group")
        self.assertIs(manager.streams["group-stream"].context.get_last_message(), manager.last_messages["group-stream"])
        self.assertIsNone(manager.streams["private-stream"].group_info)
        self.assertEqual(manager.streams["private-stream"].user_info.user_cardname, "Bobby")

        manager.streams = {"existing": make_stream("existing")}
        with patch.object(chat_stream_module.asyncio, "to_thread", new=AsyncMock(side_effect=RuntimeError("db down"))):
            await manager.load_all_streams()
        self.assertEqual(set(manager.streams), {"existing"})


class ChatManagerGetOrCreateTest(unittest.IsolatedAsyncioTestCase):
    async def test_get_or_create_stream_returns_updated_copy_for_cached_streams(self) -> None:
        manager = make_manager()
        original_user = make_user(nickname="Old")
        updated_user = make_user(nickname="New")
        original_group = make_group(name="Old Group")
        updated_group = make_group(name="New Group")
        stream_id = ChatManager._generate_stream_id("qq", original_user, original_group)
        cached_stream = make_stream(stream_id, user_info=original_user, group_info=original_group)
        cached_stream.saved = True
        manager.streams[stream_id] = cached_stream

        class FakeMessageRecv:
            pass

        last_message = FakeMessageRecv()
        manager.last_messages[stream_id] = last_message

        with (
            patch.object(chat_stream_module.time, "time", return_value=20.0),
            patch("src.chat.message_receive.message.MessageRecv", FakeMessageRecv),
        ):
            returned_stream = await manager.get_or_create_stream("qq", updated_user, updated_group)

        self.assertIsNot(returned_stream, cached_stream)
        self.assertEqual(returned_stream.user_info.user_nickname, "New")
        self.assertEqual(returned_stream.group_info.group_name, "New Group")
        self.assertIs(returned_stream.context.get_last_message(), last_message)
        self.assertEqual(cached_stream.last_active_time, 20.0)
        self.assertFalse(cached_stream.saved)
        self.assertEqual(cached_stream.user_info.user_nickname, "New")
        self.assertEqual(cached_stream.group_info.group_name, "New Group")

        cached_stream.context = None
        manager.last_messages[stream_id] = SimpleNamespace(message_id="not-a-message-recv")
        with patch.object(chat_stream_module.time, "time", return_value=21.0):
            returned_without_context = await manager.get_or_create_stream("qq", updated_user, updated_group)
        self.assertIsNone(returned_without_context.context)

    async def test_get_or_create_stream_restores_database_stream_or_creates_new_stream(self) -> None:
        manager = make_manager()
        user_info = make_user()
        group_info = make_group(name="Fresh Group")
        stream_id = ChatManager._generate_stream_id("qq", user_info, group_info)
        model = SimpleNamespace(
            stream_id=stream_id,
            platform="qq",
            user_platform="qq",
            user_id="stale-user",
            user_nickname="Stale",
            user_cardname=None,
            group_platform="qq",
            group_id="group-1",
            group_name="Stale Group",
            create_time=1.0,
            last_active_time=2.0,
        )
        manager._save_stream = AsyncMock()

        class FakeMessageRecv:
            pass

        last_message = FakeMessageRecv()
        manager.last_messages[stream_id] = last_message

        with (
            patch.object(chat_stream_module.asyncio, "to_thread", new=run_to_thread),
            patch.object(chat_stream_module.ChatStreams, "get_or_none", return_value=model),
            patch.object(chat_stream_module.time, "time", return_value=30.0),
            patch("src.chat.message_receive.message.MessageRecv", FakeMessageRecv),
        ):
            restored_stream = await manager.get_or_create_stream("qq", user_info, group_info)

        self.assertEqual(restored_stream.stream_id, stream_id)
        self.assertEqual(restored_stream.user_info.user_id, "user-1")
        self.assertEqual(restored_stream.group_info.group_name, "Fresh Group")
        self.assertIs(restored_stream.context.get_last_message(), last_message)
        self.assertEqual(restored_stream.last_active_time, 30.0)
        self.assertIsNot(manager.streams[stream_id], restored_stream)
        manager._save_stream.assert_awaited_once_with(manager.streams[stream_id])

        manager = make_manager()
        manager._save_stream = AsyncMock()
        with (
            patch.object(chat_stream_module.asyncio, "to_thread", new=run_to_thread),
            patch.object(chat_stream_module.ChatStreams, "get_or_none", return_value=None),
        ):
            new_stream = await manager.get_or_create_stream("qq", user_info, None)

        self.assertEqual(new_stream.stream_id, ChatManager._generate_stream_id("qq", user_info, None))
        self.assertIsNone(new_stream.group_info)
        self.assertIsNot(manager.streams[new_stream.stream_id], new_stream)
        manager._save_stream.assert_awaited_once_with(manager.streams[new_stream.stream_id])

    async def test_get_or_create_stream_reraises_database_lookup_errors(self) -> None:
        manager = make_manager()

        with patch.object(chat_stream_module.asyncio, "to_thread", new=AsyncMock(side_effect=RuntimeError("db down"))):
            with self.assertRaisesRegex(RuntimeError, "db down"):
                await manager.get_or_create_stream("qq", make_user(), None)


if __name__ == "__main__":
    unittest.main()
