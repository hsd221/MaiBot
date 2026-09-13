"""Regressions discovered in the independent audit-fix review."""

import tempfile
import threading
import contextvars
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from peewee import SqliteDatabase

from src.chat import chat_tool_registry
from src.chat.emoji_system import emoji_manager
from src.llm_models.payload_content import ToolCall
from src.plugin_system.core import tool_use
from src.common.database.database_model import Images, Messages
from src.chat.message_receive import storage, media_background
from src.chat.message_receive.storage import MessageStorage
from src.chat.message_receive.recall_registry import RecallRegistry
from tests.test_message_receive_utils import make_recv, make_stream
from tests.test_emoji_manager_utils import make_emoji, make_manager


class AuditChatReviewTest(unittest.IsolatedAsyncioTestCase):
    async def test_integrity_cleanup_releases_capacity_for_registration(self):
        manager = make_manager()
        with tempfile.TemporaryDirectory() as directory:
            surviving_path = Path(directory) / "surviving.png"
            surviving_path.write_bytes(b"fixture")
            removed = make_emoji("missing", full_path=str(Path(directory) / "missing.png"))
            removed.delete = AsyncMock(return_value=True)
            surviving = make_emoji("surviving", full_path=str(surviving_path))
            manager.emoji_objects = [removed, surviving]
            manager.emoji_num = manager.emoji_num_max = 2
            with patch.object(emoji_manager, "clean_unused_emojis", AsyncMock(return_value=1)):
                await manager.check_emoji_file_integrity()
        self.assertEqual(manager.emoji_objects, [surviving])
        self.assertEqual(manager.emoji_num, 1)
        self.assertLess(manager.emoji_num, manager.emoji_num_max)

    async def test_native_tool_receives_current_turn_snapshot(self):
        executor = object.__new__(tool_use.ToolExecutor)
        executor.chat_stream = SimpleNamespace(context="old")
        registry = object.__new__(chat_tool_registry.ChatToolRegistry)
        registry.chat_id = "fixture"
        registry.executor = executor
        received = []

        def get_tool(name, chat_stream):
            received.append(chat_stream)
            return SimpleNamespace(execute=AsyncMock(return_value={"content": chat_stream.context}))

        with (
            patch.object(tool_use, "get_tool_instance", side_effect=get_tool),
            patch.object(chat_tool_registry.global_announcement_manager, "get_disabled_chat_tools", return_value=[]),
        ):
            for context in ("first turn", "second turn"):
                registry.chat_stream = SimpleNamespace(context=context)
                result = await registry._execute_native_tool(ToolCall("call", "fixture", {}))
                self.assertTrue(result.success)
                self.assertEqual(result.content, context)
                self.assertIs(received[-1], registry.chat_stream)


class AuditStorageCancellationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = SqliteDatabase(f"{self.directory.name}/messages.db")
        self.models = (Messages, Images)
        self.original = {model: model._meta.database for model in self.models}
        self.database.bind(self.models, bind_refs=False, bind_backrefs=False)
        self.database.connect()
        self.database.create_tables(self.models)
        self.registry = RecallRegistry()
        self.registry_patch = patch.object(storage, "recall_registry", self.registry)
        self.registry_patch.start()

    def tearDown(self):
        self.registry_patch.stop()
        self.database.drop_tables(self.models)
        self.database.close()
        for model, original in self.original.items():
            model._meta.set_database(original)
        self.directory.cleanup()

    async def test_recall_reconciles_after_shutdown_and_repeated_cancellation(self):
        loop = asyncio.get_running_loop()
        insert_started, mark_started = asyncio.Event(), asyncio.Event()
        insert_release, mark_release = threading.Event(), threading.Event()
        original_store = MessageStorage._store_message_sync
        original_mark = MessageStorage.mark_message_recalled
        request_context = contextvars.ContextVar("audit_storage_context", default="missing")
        token = request_context.set("request")
        self.addCleanup(request_context.reset, token)

        def blocked_store(message, chat_stream):
            loop.call_soon_threadsafe(insert_started.set)
            if not insert_release.wait(5):
                raise TimeoutError("test did not release insert")
            self.assertEqual(request_context.get(), "request")
            with self.database.connection_context():
                return original_store(message, chat_stream)

        def blocked_mark(message_id):
            loop.call_soon_threadsafe(mark_started.set)
            if not mark_release.wait(5):
                raise TimeoutError("test did not release recall marker")
            with self.database.connection_context():
                return original_mark(message_id)

        baseline_tasks = asyncio.all_tasks()
        with (
            patch.object(MessageStorage, "_store_message_sync", side_effect=blocked_store),
            patch.object(MessageStorage, "mark_message_recalled", side_effect=blocked_mark),
            patch.object(media_background, "backfill_stored_message", AsyncMock()) as backfill,
        ):
            task = asyncio.create_task(
                MessageStorage.store_message(make_recv(message_id="cancel-recall"), make_stream())
            )
            try:
                await asyncio.wait_for(insert_started.wait(), 2)
                self.registry.register("cancel-recall")
                # Shutdown cancels every new task, including any internal I/O
                # task a storage implementation may have created.
                for pending in asyncio.all_tasks() - baseline_tasks:
                    pending.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                insert_release.set()
                await asyncio.wait_for(mark_started.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                mark_release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                insert_release.set()
                mark_release.set()
                await asyncio.gather(task, return_exceptions=True)
            backfill.assert_not_awaited()
        self.assertTrue(Messages.get(Messages.message_id == "cancel-recall").is_recalled)
        self.assertFalse(self.registry.has_pending("cancel-recall"))
        self.assertEqual(asyncio.all_tasks(), baseline_tasks)

    async def test_worker_exception_after_cancel_is_collected(self):
        loop = asyncio.get_running_loop()
        started, release = asyncio.Event(), threading.Event()

        def failing_store(*args):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise TimeoutError("test did not release worker")
            raise RuntimeError("fixture worker failure")

        with patch.object(MessageStorage, "_store_message_sync", side_effect=failing_store):
            task = asyncio.create_task(MessageStorage.store_message(make_recv(), make_stream()))
            try:
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)
                release.set()
                with self.assertRaisesRegex(RuntimeError, "fixture worker failure"):
                    await task
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_failed_recall_marker_keeps_pending_registration(self):
        with (
            patch.object(self.registry, "is_recalled", side_effect=[False, True]),
            patch.object(MessageStorage, "mark_message_recalled", return_value=False),
            patch.object(media_background, "backfill_stored_message", AsyncMock()),
        ):
            self.registry.register("marker-failure")
            await MessageStorage.store_message(make_recv(message_id="marker-failure"), make_stream())
        self.assertTrue(self.registry.has_pending("marker-failure"))

    async def test_media_backfill_preserves_concurrent_recall_update(self):
        self.assertTrue(MessageStorage._store_message_sync(make_recv(message_id="backfill-recall"), make_stream()))

        def recall_between_read_and_save(*args):
            self.assertTrue(MessageStorage.mark_message_recalled("backfill-recall"))
            return "[图片：cat]"

        with patch.object(
            media_background, "_replace_placeholder_occurrence", side_effect=recall_between_read_and_save
        ):
            await media_background._backfill_message_placeholder("image", "backfill-recall", "[图片：cat]", 0)
        record = Messages.get(Messages.message_id == "backfill-recall")
        self.assertTrue(record.is_recalled)
        self.assertEqual(record.processed_plain_text, "[图片：cat]")

    async def test_optional_backfill_cancellation_propagates_after_write(self):
        with patch.object(media_background, "backfill_stored_message", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await MessageStorage.store_message(make_recv(message_id="backfill-cancel"), make_stream())
        self.assertTrue(Messages.get_or_none(Messages.message_id == "backfill-cancel"))
