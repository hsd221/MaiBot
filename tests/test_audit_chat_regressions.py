"""Regression cases reproduced from the September code audit."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.chat.emoji_system import emoji_manager
from src.chat.message_receive import media_background
from src.chat.planner_actions import action_modifier, planner
from src.chat.replyer import group_generator
from tests.test_chat_stream_manager import make_manager as make_chat_manager, make_stream
from tests.test_emoji_manager_utils import make_emoji, make_manager as make_emoji_manager
from tests.test_heartflow_core import make_hfc, make_db_message
from tests.test_replyer_utils import make_group_replyer


class AuditChatRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def test_empty_history_still_builds_reply_prompt_with_current_timestamp(self):
        replyer = make_group_replyer()
        replyer.build_expression_habits = AsyncMock(return_value=("", []))
        for name in (
            "build_behavior_reference",
            "build_tool_info",
            "get_prompt_info",
            "build_actions_prompt",
            "build_personality_prompt",
            "_build_jargon_explanation",
            "build_keywords_reaction_prompt",
        ):
            setattr(replyer, name, AsyncMock(return_value=""))
        replyer.get_chat_prompt_for_chat = Mock(return_value="")
        with (
            patch.object(group_generator, "get_raw_msg_before_timestamp_with_chat", return_value=[]) as history,
            patch.object(group_generator, "build_memory_retrieval_prompt", AsyncMock(return_value=("", []))),
            patch.object(group_generator.time, "time", return_value=12345.0),
        ):
            prompt, *_ = await replyer.build_prompt_reply_context()
        self.assertTrue(prompt)
        self.assertEqual(history.call_args.kwargs["timestamp"], 12345.0)

    async def test_skipped_consumed_group_batch_is_archived(self):
        chat = make_hfc()
        message = make_db_message()
        chat._get_pending_group_messages = Mock(return_value=[message])
        chat._archive_recent_messages = AsyncMock()
        chat.message_archiver = object()
        chat._wait_for_group_message_or_timeout = AsyncMock()
        chat.turn_scheduler = SimpleNamespace(
            decide_group_turn=Mock(
                return_value=SimpleNamespace(
                    should_update_last_read_time=True,
                    should_observe=False,
                    sleep_seconds=1,
                )
            )
        )
        await chat._loopbody()
        chat._archive_recent_messages.assert_awaited_once_with([message])

    async def test_cycle_history_is_bounded(self):
        chat = make_hfc()
        for _ in range(1100):
            chat.start_cycle()
            chat.end_cycle({"loop_plan_info": {}, "loop_action_info": {}}, {})
        self.assertLessEqual(len(chat.history_loop), 100)
        self.assertEqual(chat.history_loop[-1].cycle_id, 1100)

    async def test_stream_lookup_does_not_mutate_cached_metadata(self):
        manager = make_chat_manager()
        manager.streams["stream-1"] = make_stream()
        returned = manager.get_stream("stream-1")
        returned.user_info.user_nickname = "changed"
        self.assertEqual(manager.streams["stream-1"].user_info.user_nickname, "Alice")

    async def test_action_types_without_context_are_unavailable(self):
        modifier = action_modifier.ActionModifier.__new__(action_modifier.ActionModifier)
        modifier.log_prefix = "test"
        removals = modifier._check_action_associated_types(
            {"image": SimpleNamespace(associated_types=["image"]), "text": SimpleNamespace(associated_types=[])}, None
        )
        self.assertEqual([item[0] for item in removals], ["image"])

    async def test_planner_error_reason_omits_upstream_exception(self):
        instance = planner.ActionPlanner.__new__(planner.ActionPlanner)
        instance.log_prefix = "test"
        instance.tool_registry = SimpleNamespace(
            set_available_actions=Mock(), get_tool_definitions=Mock(return_value=[])
        )
        instance.planner_llm = SimpleNamespace(
            generate_response_async=AsyncMock(side_effect=RuntimeError("secret-path"))
        )
        result = await instance._execute_main_planner("prompt", [], {}, {}, 0)
        self.assertNotIn("secret-path", result[0])

    async def test_prompt_build_error_is_not_returned_as_prompt(self):
        instance = planner.ActionPlanner.__new__(planner.ActionPlanner)
        instance.chat_id = "test"
        instance.plan_log = []
        with patch.object(planner.prompt_manager, "format_prompt", side_effect=RuntimeError("bad template")):
            prompt, _ = await instance.build_planner_prompt([])
        self.assertFalse(prompt)

    async def test_registration_failure_restores_source_file(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            source.write_bytes(b"new image")
            registered = Path(directory) / "registered"
            registered.mkdir()
            emoji = make_emoji("new", full_path=str(source))
            with (
                patch.object(emoji_manager, "EMOJI_REGISTERED_DIR", str(registered)),
                patch.object(emoji_manager.Emoji, "create", side_effect=RuntimeError("db failure")),
            ):
                self.assertFalse(await emoji.register_to_db())
            self.assertEqual(source.read_bytes(), b"new image")
            self.assertEqual(emoji.full_path, str(source))
            self.assertEqual(list(registered.iterdir()), [])

    async def test_registration_does_not_overwrite_existing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            source_dir = Path(directory) / "source"
            source_dir.mkdir()
            registered = Path(directory) / "registered"
            registered.mkdir()
            source = source_dir / "same.png"
            destination = registered / "same.png"
            source.write_bytes(b"new image")
            destination.write_bytes(b"old image")
            emoji = make_emoji("new", full_path=str(source))
            with patch.object(emoji_manager, "EMOJI_REGISTERED_DIR", str(registered)):
                self.assertFalse(await emoji.register_to_db())
            self.assertEqual(destination.read_bytes(), b"old image")
            self.assertEqual(source.read_bytes(), b"new image")

    async def test_replacement_registration_failure_preserves_old_emoji(self):
        manager = make_emoji_manager()
        old = make_emoji("old")
        new = make_emoji("new")
        new.register_to_db = AsyncMock(return_value=False)
        manager.emoji_objects = [old]
        manager.emoji_num = 1
        manager.delete_emoji = AsyncMock(return_value=True)
        manager.llm_emotion_judge.generate_response_async = AsyncMock(return_value=("删除编号1", None))
        with patch.object(manager, "_ensure_db"):
            self.assertFalse(await manager.replace_a_emoji(new))
        manager.delete_emoji.assert_not_awaited()
        self.assertEqual(manager.emoji_objects, [old])

    async def test_cleanup_preserves_recent_upload_and_live_db_record(self):
        with tempfile.TemporaryDirectory() as directory:
            recent = Path(directory) / "upload.png"
            tracked = Path(directory) / "tracked.png"
            orphan = Path(directory) / "orphan.png"
            for item in (recent, tracked, orphan):
                item.write_bytes(b"image")
            os.utime(tracked, (1, 1))
            os.utime(orphan, (1, 1))
            with patch.object(emoji_manager.Emoji, "select", return_value=[SimpleNamespace(full_path=str(tracked))]):
                removed = await emoji_manager.clean_unused_emojis(directory, [], 0)
            self.assertEqual(removed, 1)
            self.assertTrue(recent.exists())
            self.assertTrue(tracked.exists())
            self.assertFalse(orphan.exists())

    async def test_periodic_scanner_survives_filesystem_error(self):
        manager = make_emoji_manager()
        manager.get_all_emoji_from_db = AsyncMock()
        manager.check_emoji_file_integrity = AsyncMock()
        with (
            patch.object(emoji_manager, "clear_temp_emoji", AsyncMock(side_effect=OSError("filesystem busy"))),
            patch.object(emoji_manager.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await manager.start_periodic_check_register()


class AuditMediaStorageRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_storage_retries_completed_media(self):
        state = SimpleNamespace(status="done", result_text="[图片：cat]")
        ref = SimpleNamespace(task_key="image:hash", kind="image", occurrence_index=0)
        with (
            patch.dict(media_background._media_task_states, {"image:hash": state}, clear=True),
            patch.dict(media_background._message_media_refs, {"late-message": [ref]}, clear=True),
            patch.object(media_background, "_backfill_message_placeholder", AsyncMock()) as backfill,
        ):
            await media_background.backfill_stored_message("late-message")
        backfill.assert_awaited_once_with("image", "late-message", "[图片：cat]", 0)

    async def test_duplicate_image_descriptions_keep_original_text(self):
        from peewee import SqliteDatabase
        from src.common.database.database_model import Images
        from src.chat.message_receive.storage import MessageStorage

        database = SqliteDatabase(":memory:")
        with database.bind_ctx([Images], bind_refs=False, bind_backrefs=False):
            database.create_tables([Images])
            for index in range(2):
                Images.create(
                    image_id=f"image-{index}",
                    emoji_hash=f"hash-{index}",
                    description="same",
                    path=f"/tmp/x-{index}",
                    timestamp=index,
                    type="image",
                )
            self.assertEqual(MessageStorage.replace_image_descriptions("[图片：same]"), "[图片：same]")
        database.close()

    async def test_private_cycle_history_is_bounded(self):
        from src.chat.brain_chat.brain_chat import BrainChatting

        chat = BrainChatting.__new__(BrainChatting)
        chat.history_loop = []
        chat._cycle_counter = 0
        for _ in range(1100):
            chat.start_cycle()
            chat.end_cycle({"loop_plan_info": {}, "loop_action_info": {}}, {})
        self.assertEqual(len(chat.history_loop), 100)
        self.assertEqual(chat.history_loop[-1].cycle_id, 1100)

    async def test_trace_files_are_private(self):
        from src.chat.logger.plan_reply_logger import PlanReplyLogger

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(PlanReplyLogger, "_REPLY_DIR", Path(directory)):
                PlanReplyLogger.log_reply("chat", "private history", "reply", [], "model")
            log_dir = Path(directory) / "chat"
            self.assertEqual(log_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(next(log_dir.glob("*.json")).stat().st_mode & 0o777, 0o600)

    async def test_reply_action_statistics_keep_bounded_history(self):
        from src.chat.utils import utils

        with tempfile.TemporaryDirectory() as directory:
            temp_dir = Path(directory) / "data" / "temp"
            temp_dir.mkdir(parents=True)
            for index in range(1001):
                (temp_dir / f"replyer_action_20000101_{index:06}.json").write_text("{}")
            unrelated = temp_dir / "unrelated.json"
            unrelated.write_text("{}")
            old_cwd = os.getcwd()
            os.chdir(directory)
            try:
                utils.record_replyer_action_temp("chat", "reason", 1)
            finally:
                os.chdir(old_cwd)
            self.assertEqual(len(list(temp_dir.glob("replyer_action_*.json"))), 1000)
            self.assertTrue(unrelated.exists())


class AuditRemainingChatContractsTest(unittest.IsolatedAsyncioTestCase):
    async def test_private_trigger_window_excludes_level_one_commands(self):
        from src.chat.brain_chat import brain_chat

        chat = brain_chat.BrainChatting.__new__(brain_chat.BrainChatting)
        chat.stream_id = "private"
        chat.last_read_time = 1
        with patch.object(brain_chat.message_api, "get_messages_by_time_in_chat", return_value=[]) as query:
            chat._get_pending_private_messages(2)
        self.assertEqual(query.call_args.kwargs["filter_intercept_message_level"], 0)

    async def test_after_send_cannot_rewrite_persisted_content(self):
        from src.chat.message_receive import uni_message_sender
        from src.plugin_system.base.component_types import MaiMessages
        from tests.test_message_receive_utils import make_sending

        message = make_sending()
        modified = MaiMessages()
        modified.modify_plain_text("fabricated after delivery")
        events = SimpleNamespace(
            handle_mai_events=AsyncMock(side_effect=[(True, None), (True, None), (True, modified)])
        )
        sender = uni_message_sender.UniversalMessageSender()
        sender.storage = SimpleNamespace(store_message=AsyncMock())
        with (
            patch("src.plugin_system.core.events_manager.events_manager", events),
            patch.object(uni_message_sender, "_send_message", AsyncMock(return_value=True)),
        ):
            self.assertTrue(await sender.send_message(message))
        self.assertEqual(sender.storage.store_message.await_args.args[0].processed_plain_text, "sent text")

    async def test_loop_retries_in_same_task_with_bounded_backoff(self):
        from src.chat.heart_flow import heartFC_chat

        chat = make_hfc()
        chat.running = True
        chat._loopbody = AsyncMock(side_effect=RuntimeError("persistent failure"))
        waits = []

        async def wait(delay):
            waits.append(delay)
            if len(waits) == 6:
                chat.running = False

        with (
            patch.object(heartFC_chat.asyncio, "sleep", wait),
            patch.object(heartFC_chat.asyncio, "create_task") as create,
        ):
            await chat._main_chat_loop()
        self.assertEqual(waits, [3, 6, 12, 24, 48, 60])
        create.assert_not_called()

    async def test_private_observe_without_message_context_uses_default_prompt_scope(self):
        from src.chat.brain_chat import brain_chat
        from tests.test_brain_chat_planner import BrainChattingUnitTest

        chat = BrainChattingUnitTest().make_chat()
        chat.tool_registry = SimpleNamespace()
        chat._run_native_tool_turn = AsyncMock(
            return_value=SimpleNamespace(
                decisions=[],
                tool_results=[],
                reply_sent=False,
                loop_info=None,
                reply_text="",
                should_continue=True,
            )
        )
        chat.print_cycle_info = Mock()
        chat._schedule_native_post_turn_tasks = Mock()
        with patch.object(
            brain_chat, "get_chat_manager", return_value=SimpleNamespace(get_stream=Mock(return_value=None))
        ):
            self.assertTrue(await chat._observe_native([]))
        chat._run_native_tool_turn.assert_awaited_once()

    async def test_group_reply_action_accepts_missing_action_data(self):
        from src.chat.heart_flow import heartFC_chat
        from src.common.data_models.info_data_model import ActionPlannerInfo

        chat = make_hfc()
        with (
            patch.object(heartFC_chat, "record_replyer_action_temp"),
            patch.object(heartFC_chat.database_api, "store_action_info", AsyncMock()),
            patch.object(
                heartFC_chat.generator_api, "generate_reply", AsyncMock(return_value=(False, None))
            ) as generate,
        ):
            result = await chat._execute_action(ActionPlannerInfo(action_type="reply"), [], "thinking", {}, {})
        generate.assert_awaited_once()
        self.assertFalse(result["success"])

    async def test_emoji_deletion_loads_new_upload_missing_from_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "upload.png"
            image.write_bytes(b"image")
            row = SimpleNamespace(
                full_path=str(image),
                emoji_hash="uploaded",
                description="desc",
                emotion="",
                usage_count=0,
                last_used_time=0,
                register_time=0,
                format="png",
                delete_instance=Mock(return_value=1),
            )
            manager = make_emoji_manager()
            with (
                patch.object(manager, "_ensure_db"),
                patch.object(emoji_manager.Emoji, "get_or_none", return_value=row),
                patch.object(emoji_manager.Emoji, "get", return_value=row),
            ):
                self.assertTrue(await manager.delete_emoji("uploaded"))
            self.assertFalse(image.exists())
            manager.vector_index.delete.assert_awaited_once_with("uploaded")
            self.assertEqual(manager.emoji_num, 0)

    async def test_temporary_cache_cleanup_preserves_referenced_image(self):
        with tempfile.TemporaryDirectory() as directory:
            images = Path(directory) / "images"
            images.mkdir()
            tracked = images / "tracked.png"
            tracked.write_bytes(b"image")
            for index in range(101):
                (images / f"untracked-{index}.png").write_bytes(b"image")
            with (
                patch.object(emoji_manager, "BASE_DIR", directory),
                patch.object(emoji_manager.Images, "select", return_value=[SimpleNamespace(path=str(tracked))]),
            ):
                await emoji_manager.clear_temp_emoji()
            self.assertEqual(list(images.iterdir()), [tracked])
