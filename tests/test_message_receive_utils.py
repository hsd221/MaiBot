import base64
import hashlib
import asyncio
import io
import json
import sys
import tempfile
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from maim_message import BaseMessageInfo, GroupInfo, MessageBase, Seg, UserInfo
from peewee import SqliteDatabase
from PIL import Image

from src.chat.message_receive import media_background
from src.chat.message_receive.chat_stream import ChatManager, ChatStream
from src.chat.message_receive.message import MessageRecv, MessageSending, MessageSet
from src.chat.message_receive.storage import MessageStorage
from src.chat.message_receive import uni_message_sender
from src.chat.utils import utils_image
from src.common.data_models.message_component_model import (
    AtComponent,
    EmojiComponent,
    FileComponent,
    ForwardComponent,
    ImageComponent,
    MessageComponentSequence,
    ReplyComponent,
    SegmentListComponent,
    TextComponent,
    UnknownComponent,
    VoiceComponent,
)
from src.common.database.database_model import Emoji, EmojiDescriptionCache, ImageDescriptions, Images, Messages
from src.plugin_system.base.base_command import BaseCommand
from src.plugin_system.base.component_types import EventType, MaiMessages


def animated_png_base64() -> str:
    first = Image.new("RGBA", (4, 3), color=(220, 30, 40, 255))
    second = Image.new("RGBA", (4, 3), color=(30, 220, 40, 255))
    buffer = io.BytesIO()
    first.save(buffer, format="PNG", save_all=True, append_images=[second], duration=100, loop=0)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def make_stream(*, platform: str = "qq", group_id: str | None = "group-1") -> ChatStream:
    return ChatStream(
        stream_id="stream-1",
        platform=platform,
        user_info=UserInfo(platform=platform, user_id="user-1", user_nickname="Alice", user_cardname="Ali"),
        group_info=GroupInfo(platform=platform, group_id=group_id, group_name="Group") if group_id else None,
        data={"create_time": 1.0, "last_active_time": 2.0},
    )


def make_recv(*, message_id: str = "recv-1", processed_plain_text: str = "hello") -> MessageRecv:
    return MessageRecv(
        {
            "message_info": {
                "platform": "qq",
                "message_id": message_id,
                "time": 10.0,
                "group_info": {"platform": "qq", "group_id": "group-1", "group_name": "Group"},
                "user_info": {
                    "platform": "qq",
                    "user_id": "user-1",
                    "user_nickname": "Alice",
                    "user_cardname": "Ali",
                },
                "additional_config": {"source": "unit"},
            },
            "message_segment": {"type": "text", "data": processed_plain_text},
            "raw_message": processed_plain_text,
            "processed_plain_text": processed_plain_text,
        }
    )


def make_sending(*, platform: str = "qq", group_id: str | None = "group-1") -> MessageSending:
    stream = make_stream(platform=platform, group_id=group_id)
    return MessageSending(
        message_id="send-1",
        chat_stream=stream,
        bot_user_info=UserInfo(platform=platform, user_id="bot", user_nickname="Mai"),
        sender_info=stream.user_info,
        message_segment=Seg(type="text", data="sent text"),
        display_message="shown",
        reply_to="qq:user-1",
        selected_expressions=[{"id": 1}],
    )


class AsyncScope:
    def __init__(self, name: str, calls: list[str] | None = None) -> None:
        self.name = name
        self.calls = calls

    async def __aenter__(self):
        if self.calls is not None:
            self.calls.append(self.name)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class MessageStorageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary_db = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_db.cleanup)
        self.db = SqliteDatabase(f"{temporary_db.name}/messages.db")
        self.models = [Messages, Images]
        self.original_dbs = {model: model._meta.database for model in self.models}
        self.db.bind(self.models, bind_refs=False, bind_backrefs=False)
        self.db.connect()
        self.db.create_tables(self.models)

    def tearDown(self) -> None:
        self.db.drop_tables(self.models)
        self.db.close()
        for model, database in self.original_dbs.items():
            model._meta.set_database(database)

    async def test_store_message_serializes_recv_metadata_and_filters_internal_markup(self) -> None:
        Images.create(
            image_id="img-new",
            emoji_hash="hash-1",
            description="cat",
            path="/tmp/new.png",
            timestamp=2.0,
            type="image",
        )
        message = make_recv(
            processed_plain_text="<MainRule>hidden</MainRule>hello [图片：cat]<UserMessage>raw</UserMessage>"
        )
        message.interest_value = 0.75
        message.is_mentioned = True
        message.is_at = True
        message.reply_probability_boost = 0.4
        message.priority_mode = "priority"
        message.priority_info = {"score": 2}
        message.is_emoji = True
        message.is_picid = True
        message.is_command = True
        message.intercept_message_level = 3
        message.key_words = ["你好", "cat"]
        message.key_words_lite = ["cat"]

        await MessageStorage.store_message(message, make_stream())

        record = Messages.get(Messages.message_id == "recv-1")
        self.assertEqual(record.processed_plain_text, "hello [picid:img-new]")
        self.assertEqual(record.chat_id, "stream-1")
        self.assertEqual(record.user_nickname, "Alice")
        self.assertEqual(record.chat_info_group_id, "group-1")
        self.assertEqual(record.interest_value, 0.75)
        self.assertTrue(record.is_mentioned)
        self.assertTrue(record.is_at)
        self.assertEqual(record.reply_probability_boost, 0.4)
        self.assertEqual(json.loads(record.key_words), ["你好", "cat"])
        self.assertEqual(json.loads(record.key_words_lite), ["cat"])
        self.assertEqual(json.loads(record.additional_config), {"source": "unit"})
        self.assertEqual(record.intercept_message_level, 3)

    async def test_store_message_skips_notify_and_stores_sending_display_fields(self) -> None:
        notify = make_recv(message_id="notice")
        notify.is_notify = True
        await MessageStorage.store_message(notify, make_stream())
        self.assertEqual(Messages.select().count(), 0)

        message = make_sending()
        message.processed_plain_text = "<schedule>hidden</schedule>sent text"
        message.display_message = "shown <MainRule>hidden</MainRule>text"
        message.message_info.additional_config = {"outbound": True}

        await MessageStorage.store_message(message, message.chat_stream)

        record = Messages.get(Messages.message_id == "send-1")
        self.assertEqual(record.processed_plain_text, "sent text")
        self.assertEqual(record.display_message, "shown text")
        self.assertEqual(record.reply_to, "qq:user-1")
        self.assertEqual(record.interest_value, 0)
        self.assertFalse(record.is_mentioned)
        self.assertEqual(json.loads(record.selected_expressions), [{"id": 1}])
        self.assertEqual(json.loads(record.additional_config), {"outbound": True})

    def test_storage_serializers_and_image_description_replacement_handle_edges(self) -> None:
        Images.create(
            image_id="img-old",
            emoji_hash="hash-old",
            description="cat",
            path="/tmp/old.png",
            timestamp=1.0,
            type="image",
        )
        Images.create(
            image_id="img-new",
            emoji_hash="hash-new",
            description="cat",
            path="/tmp/new.png",
            timestamp=3.0,
            type="image",
        )

        self.assertEqual(MessageStorage._serialize_keywords(["你", "好"]), '["你", "好"]')
        self.assertEqual(MessageStorage._serialize_keywords("not-list"), "[]")
        self.assertEqual(MessageStorage._deserialize_keywords('["a", "b"]'), ["a", "b"])
        self.assertEqual(MessageStorage._deserialize_keywords("{bad json"), [])
        self.assertEqual(MessageStorage._serialize_additional_config("raw"), "raw")
        self.assertEqual(MessageStorage._serialize_additional_config({"x": 1}), '{"x": 1}')
        self.assertEqual(MessageStorage._serialize_additional_config(None), "{}")
        self.assertEqual(MessageStorage._serialize_selected_expressions(None), "")
        self.assertEqual(MessageStorage._serialize_selected_expressions([1, {"id": 2}]), '[1, {"id": 2}]')
        self.assertEqual(
            MessageStorage.replace_image_descriptions("look [图片：cat] and [图片：missing]"),
            "look [图片：cat] and [图片：missing]",
        )

    def test_update_message_reports_missing_ids_matches_latest_and_handles_errors(self) -> None:
        Messages.create(
            message_id="local-id",
            time=1.0,
            chat_id="stream-1",
            chat_info_stream_id="stream-1",
            chat_info_platform="qq",
            chat_info_user_platform="qq",
            chat_info_user_id="user-1",
            chat_info_user_nickname="Alice",
            chat_info_create_time=1.0,
            chat_info_last_active_time=2.0,
            user_platform="qq",
            user_id="bot",
            user_nickname="Mai",
            processed_plain_text="old",
        )

        self.assertFalse(MessageStorage.update_message("local-id", None))
        self.assertTrue(MessageStorage.update_message("local-id", "platform-id"))
        self.assertEqual(Messages.get().message_id, "platform-id")
        self.assertFalse(MessageStorage.update_message("missing", "new-id"))

        with patch.object(Messages, "select", side_effect=RuntimeError("db down")):
            self.assertFalse(MessageStorage.update_message("platform-id", "new-id"))


class MediaBackgroundHelpersTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        media_background._media_task_states.clear()
        media_background._message_media_refs.clear()
        media_background._backfill_locks.clear()
        media_background._backfill_lock_users.clear()
        self.db = SqliteDatabase(":memory:")
        self.models = [Messages, Images, ImageDescriptions, Emoji, EmojiDescriptionCache]
        self.original_dbs = {model: model._meta.database for model in self.models}
        self.db.bind(self.models, bind_refs=False, bind_backrefs=False)
        self.db.connect()
        self.db.create_tables(self.models)

    def tearDown(self) -> None:
        media_background._media_task_states.clear()
        media_background._message_media_refs.clear()
        media_background._backfill_locks.clear()
        media_background._backfill_lock_users.clear()
        self.db.drop_tables(self.models)
        self.db.close()
        for model, database in self.original_dbs.items():
            model._meta.set_database(database)

    async def test_backfill_lock_is_released_after_last_user_exits(self) -> None:
        """回填锁按 message_id 创建，用完必须回收，否则每条含媒体的消息永久占一个 Lock。"""
        async with media_background._backfill_lock("msg-1"):
            self.assertIn("msg-1", media_background._backfill_locks)

            # 嵌套使用者仍在时不能提前回收
            async def nested() -> None:
                async with media_background._backfill_lock("msg-1"):
                    pass

            waiter = asyncio.ensure_future(nested())
            await asyncio.sleep(0)
            self.assertEqual(media_background._backfill_lock_users["msg-1"], 2)

        await waiter
        self.assertEqual(media_background._backfill_locks, {})
        self.assertEqual(media_background._backfill_lock_users, {})

    def test_media_caches_are_bounded(self) -> None:
        """识别缓存的键随消息量无限增长，必须有上界。"""
        with (
            patch.object(media_background, "_MAX_TRACKED_MEDIA_TASKS", 3),
            patch.object(media_background, "_MAX_TRACKED_MESSAGES", 3),
        ):
            for index in range(10):
                state = media_background._MediaTaskState(kind="image", media_hash=f"h{index}")
                media_background._media_task_states[f"image:h{index}"] = state
                media_background._trim_media_task_states()
                media_background._remember_message_ref(f"msg-{index}", f"image:h{index}", state)

            self.assertEqual(len(media_background._media_task_states), 3)
            self.assertEqual(len(media_background._message_media_refs), 3)
            # 保留的是最近使用的条目
            self.assertEqual(list(media_background._message_media_refs), ["msg-7", "msg-8", "msg-9"])

            # 识别中的任务不能被淘汰，否则 _run_media_task 回写时状态已消失
            for key in list(media_background._media_task_states):
                media_background._media_task_states[key].status = "processing"
            for index in range(10, 14):
                state = media_background._MediaTaskState(kind="image", media_hash=f"h{index}")
                state.status = "processing"
                media_background._media_task_states[f"image:h{index}"] = state
                media_background._trim_media_task_states()

            self.assertEqual(len(media_background._media_task_states), 7)

    def test_message_media_refs_are_bounded_and_keep_latest_occurrences(self) -> None:
        state = media_background._MediaTaskState(kind="image", media_hash="same-image")
        task_key = "image:same-image"
        media_background._media_task_states[task_key] = state

        with patch.object(media_background, "_MAX_MEDIA_REFS_PER_MESSAGE", 3):
            for _ in range(10):
                media_background._remember_message_ref("msg-repeated", task_key, state)

        refs = media_background._message_media_refs["msg-repeated"]
        self.assertEqual([ref.occurrence_index for ref in refs], [7, 8, 9])
        self.assertEqual(
            state.message_refs, [("msg-repeated", 7, None), ("msg-repeated", 8, None), ("msg-repeated", 9, None)]
        )

    def test_message_cache_eviction_removes_task_state_refs(self) -> None:
        state = media_background._MediaTaskState(kind="image", media_hash="shared-image")
        task_key = "image:shared-image"
        media_background._media_task_states[task_key] = state

        with patch.object(media_background, "_MAX_TRACKED_MESSAGES", 3):
            for index in range(10):
                media_background._remember_message_ref(f"msg-{index}", task_key, state)

        self.assertEqual(list(media_background._message_media_refs), ["msg-7", "msg-8", "msg-9"])
        self.assertEqual(
            state.message_refs,
            [("msg-7", 0, None), ("msg-8", 0, None), ("msg-9", 0, None)],
        )

    def test_hash_format_and_placeholder_replacement_helpers_are_stable(self) -> None:
        encoded = base64.b64encode(b"image-bytes").decode()

        self.assertEqual(media_background._hash_media_data(encoded), hashlib.md5(b"image-bytes").hexdigest())
        self.assertEqual(
            media_background._make_task_key("image", encoded), f"image:{hashlib.md5(b'image-bytes').hexdigest()}"
        )
        self.assertFalse(media_background._is_successful_result("image", "[表情包：开心]"))
        self.assertTrue(media_background._is_successful_result("emoji", "[表情包：开心]"))
        self.assertEqual(media_background._format_cached_result("voice", "hello"), "[语音：hello]")
        self.assertEqual(media_background._format_cached_result("image", " [图片：已有] "), "[图片：已有]")
        self.assertIsNone(media_background._format_cached_result("unknown", "text"))
        self.assertEqual(media_background._media_token_pattern("voice"), r"\[(?:语音消息|语音：[^\]]+)\]")
        self.assertEqual(
            media_background._replace_placeholder_occurrence("image", "[图片] [图片：旧] [图片]", "[图片：新]", 0),
            "[图片：新] [图片：旧] [图片]",
        )
        self.assertEqual(
            media_background._replace_placeholder_occurrence("image", "[图片] [图片：旧] [图片]", "[图片：新]", 1),
            "[图片] [图片：旧] [图片]",
        )
        self.assertEqual(
            media_background._replace_placeholder_occurrence("image", "[图片] [图片：旧] [图片]", "[图片：新]", 2),
            "[图片] [图片：旧] [图片：新]",
        )

    def test_load_cached_media_result_reads_image_and_emoji_persistent_caches(self) -> None:
        media_hash = "hash-1"
        semantic_description = (
            "情感：开心、惊讶；适用场景：当收到意外好消息时，用于表达惊喜；"
            "表达意图：积极回应；画面内容：角色举手欢呼；画面文字：好耶；风格/梗：庆祝反应图"
        )
        Images.create(
            image_id="img-1",
            emoji_hash=media_hash,
            description="cached image",
            path="/tmp/image.png",
            timestamp=1.0,
            type="image",
        )
        Emoji.create(
            full_path="/tmp/emoji.gif",
            format="gif",
            emoji_hash=media_hash,
            description=f"[表情包：{semantic_description}]",
            emotion="开心，惊讶",
            record_time=1.0,
        )

        self.assertIsNone(media_background._load_cached_media_result("image", media_hash))
        self.assertIsNone(media_background._load_cached_media_result("emoji", media_hash))

        ImageDescriptions.create(
            type=utils_image.VISION_DESCRIPTION_CACHE_TYPE,
            image_description_hash=media_hash,
            description="unified vision",
            timestamp=1.0,
        )
        self.assertEqual(media_background._load_cached_media_result("image", media_hash), "[图片：unified vision]")
        self.assertEqual(
            media_background._load_cached_media_result("emoji", media_hash),
            f"[表情包：{semantic_description}]",
        )
        self.assertIsNone(media_background._load_cached_media_result("voice", media_hash))

        Emoji.create(
            full_path="/tmp/legacy-emoji.png",
            format="png",
            emoji_hash="legacy-registered",
            description="旧注册视觉描述",
            emotion="开心",
            record_time=1.0,
        )
        self.assertIsNone(media_background._load_cached_media_result("emoji", "legacy-registered"))

        EmojiDescriptionCache.create(
            emoji_hash="legacy-static",
            description="旧式视觉描述",
            emotion_tags="开心,惊讶",
            timestamp=1.0,
        )
        self.assertIsNone(media_background._load_cached_media_result("emoji", "legacy-static"))

    def test_load_cached_media_result_ignores_stale_animated_cache_and_strips_current_marker(self) -> None:
        animated_data = animated_png_base64()
        media_hash = media_background._hash_media_data(animated_data)
        Emoji.create(
            full_path="/tmp/stale.png",
            format="png",
            emoji_hash=media_hash,
            description="旧拼图描述",
            emotion="旧情绪",
            record_time=1.0,
        )
        EmojiDescriptionCache.create(
            emoji_hash=media_hash,
            description="旧拼图描述",
            emotion_tags="旧情绪",
            timestamp=1.0,
        )

        self.assertIsNone(media_background._load_cached_media_result("emoji", media_hash, animated_data))

        cache_record = EmojiDescriptionCache.get(EmojiDescriptionCache.emoji_hash == media_hash)
        semantic_description = (
            "情感：开心；适用场景：当收到好消息时，用于表达开心；表达意图：积极回应；"
            "画面内容：角色跳起来欢呼；画面文字：好耶；风格/梗：庆祝反应图"
        )
        cache_record.description = utils_image.write_gif_description_cache(semantic_description)
        cache_record.emotion_tags = "新版情绪"
        cache_record.save()
        ImageDescriptions.create(
            type=utils_image.VISION_DESCRIPTION_CACHE_TYPE,
            image_description_hash=media_hash,
            description="unified animated vision",
            timestamp=1.0,
        )

        self.assertEqual(
            media_background._load_cached_media_result("emoji", media_hash, animated_data),
            f"[表情包：{semantic_description}]",
        )

    def test_enhance_media_placeholders_uses_only_completed_task_refs(self) -> None:
        done_state = media_background._MediaTaskState(
            kind="image",
            media_hash="hash-1",
            status="done",
            result_text="[图片：完成]",
        )
        pending_state = media_background._MediaTaskState(kind="emoji", media_hash="hash-2", status="processing")
        media_background._media_task_states["image:hash-1"] = done_state
        media_background._media_task_states["emoji:hash-2"] = pending_state
        media_background._message_media_refs["msg-1"] = [
            media_background._MessageMediaRef(kind="image", task_key="image:hash-1", occurrence_index=1),
            media_background._MessageMediaRef(kind="emoji", task_key="emoji:hash-2", occurrence_index=0),
        ]

        self.assertEqual(
            media_background.enhance_media_placeholders("msg-1", "[图片] [图片] [表情包]"),
            "[图片] [图片：完成] [表情包]",
        )
        self.assertEqual(media_background.enhance_media_placeholders(None, "[图片]"), "[图片]")
        self.assertEqual(media_background.enhance_media_placeholders("msg-1", None), "")

    async def test_run_media_task_records_success_and_failure_without_blocking_callers(self) -> None:
        success_state = media_background._MediaTaskState(
            kind="image",
            media_hash="hash-1",
            status="processing",
            message_refs=[("msg-1", 0, None)],
        )
        media_background._media_task_states["image:hash-1"] = success_state

        with (
            patch.object(media_background, "_analyze_media", new=AsyncMock(return_value="[图片：完成]")),
            patch.object(media_background, "_schedule_placeholder_backfill") as schedule_backfill,
        ):
            await media_background._run_media_task("image:hash-1", "data")

        self.assertEqual(success_state.status, "done")
        self.assertEqual(success_state.result_text, "[图片：完成]")
        self.assertIsNone(success_state.task)
        schedule_backfill.assert_called_once_with("image", "msg-1", "[图片：完成]", 0)

        failed_state = media_background._MediaTaskState(kind="emoji", media_hash="hash-2", status="processing")
        media_background._media_task_states["emoji:hash-2"] = failed_state
        with (
            patch.object(media_background, "_analyze_media", new=AsyncMock(return_value="[图片：不是表情]")),
            patch.object(media_background, "_schedule_placeholder_backfill") as schedule_backfill,
        ):
            await media_background._run_media_task("emoji:hash-2", "data")

        self.assertEqual(failed_state.status, "failed")
        self.assertIsNone(failed_state.task)
        schedule_backfill.assert_not_called()

    async def test_backfill_message_placeholder_updates_database_record(self) -> None:
        Messages.create(
            message_id="msg-1",
            time=1.0,
            chat_id="stream-1",
            chat_info_stream_id="stream-1",
            chat_info_platform="qq",
            chat_info_user_platform="qq",
            chat_info_user_id="user-1",
            chat_info_user_nickname="Alice",
            chat_info_create_time=1.0,
            chat_info_last_active_time=2.0,
            user_platform="qq",
            user_id="user-1",
            user_nickname="Alice",
            processed_plain_text="[图片] [图片]",
        )

        await media_background._backfill_message_placeholder("image", "msg-1", "[图片：完成]", 1)

        self.assertEqual(Messages.get().processed_plain_text, "[图片] [图片：完成]")

    async def test_media_scheduler_deduplicates_cached_done_processing_and_missing_loop_paths(self) -> None:
        media_hash = media_background._hash_media_data("image-data")
        task_key = f"image:{media_hash}"

        with (
            patch.object(media_background, "_load_cached_media_result", return_value="[图片：cached]") as load_cache,
            patch.object(media_background, "_schedule_placeholder_backfill") as schedule_backfill,
        ):
            media_background.schedule_image_description_task("image-data", "msg-1")

        self.assertEqual(media_background._media_task_states[task_key].status, "done")
        self.assertEqual(media_background._media_task_states[task_key].result_text, "[图片：cached]")
        schedule_backfill.assert_called_once_with("image", "msg-1", "[图片：cached]", 0)
        load_cache.assert_called_once_with("image", media_hash, "image-data")

        with patch.object(media_background, "_schedule_placeholder_backfill") as schedule_backfill:
            media_background.schedule_image_description_task("image-data", "msg-2")

        schedule_backfill.assert_called_once_with("image", "msg-2", "[图片：cached]", 0)

        processing_key = media_background._make_task_key("emoji", "emoji-data")
        processing_task = asyncio.create_task(asyncio.sleep(0))
        media_background._media_task_states[processing_key] = media_background._MediaTaskState(
            kind="emoji",
            media_hash=processing_key.split(":", 1)[1],
            status="processing",
            task=processing_task,
        )
        media_background.schedule_emoji_description_task("emoji-data", "msg-3")
        self.assertEqual(media_background._media_task_states[processing_key].task, processing_task)
        await processing_task

        unknown_done = media_background._MediaTaskState(kind="unknown", media_hash="hash", status="done")
        self.assertFalse(media_background._is_successful_result("unknown", "[x]"))
        self.assertIsNone(media_background._format_cached_result("unknown", "x"))
        self.assertIsNone(await media_background._analyze_media("unknown", "data"))
        media_background._remember_message_ref(None, "unknown:hash", unknown_done)
        self.assertEqual(unknown_done.message_refs, [])

        with patch.object(
            media_background, "asyncio", SimpleNamespace(get_running_loop=Mock(side_effect=RuntimeError))
        ):
            media_background._schedule_placeholder_backfill("image", "msg-4", "[图片：done]", 0)
            media_background._schedule_media_task("voice", "voice-data", "msg-5")

    async def test_media_background_retries_missing_records_and_marks_task_failure(self) -> None:
        sleep_calls = []

        async def fake_sleep(delay):
            sleep_calls.append(delay)

        with (
            patch.object(media_background.Messages, "select", side_effect=RuntimeError("db down")),
            patch.object(media_background.asyncio, "sleep", new=fake_sleep),
        ):
            await media_background._backfill_message_placeholder("image", "missing", "[图片：done]", 0)

        self.assertEqual(sleep_calls, [0.5, 1.0, 1.5, 2.0, 2.5, 3.0])

        failed_state = media_background._MediaTaskState(kind="voice", media_hash="hash", status="processing")
        media_background._media_task_states["voice:hash"] = failed_state
        with patch.object(media_background, "_analyze_media", new=AsyncMock(side_effect=RuntimeError("voice down"))):
            await media_background._run_media_task("voice:hash", "data")

        self.assertEqual(failed_state.status, "failed")
        self.assertIsNone(failed_state.task)


class MessageRecvLightweightMediaTest(unittest.IsolatedAsyncioTestCase):
    async def test_lightweight_media_processing_schedules_background_tasks_and_formats_cards(self) -> None:
        message = make_recv(processed_plain_text="")
        message.message_segment = Seg(
            type="seglist",
            data=[
                Seg(type="text", data="hello"),
                Seg(type="image", data="aW1hZ2U="),
                Seg(type="emoji", data="ZW1vamk="),
                Seg(type="voice", data="dm9pY2U="),
                Seg(
                    type="music_card",
                    data={
                        "title": "Song",
                        "singer": "Singer",
                        "tag": "Radio",
                        "jump_url": "https://jump.test",
                        "music_url": "https://music.test",
                    },
                ),
                Seg(type="mention_bot", data=0.8),
            ],
        )

        with (
            patch("src.chat.message_receive.message.schedule_image_description_task") as schedule_image,
            patch("src.chat.message_receive.message.schedule_emoji_description_task") as schedule_emoji,
            patch("src.chat.message_receive.message.schedule_voice_transcription_task") as schedule_voice,
        ):
            await message.process(enable_heavy_media_analysis=False, enable_voice_transcription=False)

        self.assertEqual(
            message.processed_plain_text,
            "hello [图片] [表情包] [语音消息] "
            "[音乐: Song - Singer (Radio)] 跳转链接: https://jump.test 音乐链接: https://music.test",
        )
        schedule_image.assert_called_once_with("aW1hZ2U=", "recv-1")
        expected_chat_id = ChatManager._generate_stream_id(
            message.message_info.platform,
            message.message_info.user_info,
            message.message_info.group_info,
        )
        schedule_emoji.assert_called_once_with("ZW1vamk=", "recv-1", chat_id=expected_chat_id)
        schedule_voice.assert_called_once_with("dm9pY2U=", "recv-1")
        self.assertEqual(message.is_mentioned, 0.8)

    async def test_chat_id_derivation_failure_does_not_block_emoji_processing(self) -> None:
        message = make_recv(processed_plain_text="")
        message.message_segment = Seg(type="emoji", data="ZW1vamk=")

        with (
            patch.object(ChatManager, "_generate_stream_id", side_effect=ValueError("missing identity")),
            patch("src.chat.message_receive.message.schedule_emoji_description_task") as schedule_emoji,
        ):
            await message.process(enable_heavy_media_analysis=False)

        self.assertEqual(message.processed_plain_text, "[表情包]")
        schedule_emoji.assert_called_once_with("ZW1vamk=", "recv-1", chat_id=None)


class MessageSetTest(unittest.TestCase):
    def test_message_set_sorts_retrieves_removes_and_rejects_wrong_types(self) -> None:
        first = make_sending()
        first.message_info.message_id = "first"
        first.message_info.time = 1.0
        second = make_sending()
        second.message_info.message_id = "second"
        second.message_info.time = 3.0
        third = make_sending()
        third.message_info.message_id = "third"
        third.message_info.time = 2.0

        message_set = MessageSet(make_stream(), "set-1")
        message_set.add_message(second)
        message_set.add_message(first)
        message_set.add_message(third)

        self.assertEqual(
            [message.message_info.message_id for message in message_set.messages], ["first", "third", "second"]
        )
        self.assertEqual(len(message_set), 3)
        self.assertEqual(str(message_set), "MessageSet(id=set-1, count=3)")
        self.assertIs(message_set.get_message_by_index(1), third)
        self.assertIsNone(message_set.get_message_by_index(-1))
        self.assertIsNone(message_set.get_message_by_index(99))
        self.assertIs(message_set.get_message_by_time(0.5), first)
        self.assertIs(message_set.get_message_by_time(2.5), second)
        self.assertIs(message_set.get_message_by_time(99.0), second)

        self.assertTrue(message_set.remove_message(third))
        self.assertFalse(message_set.remove_message(third))
        self.assertEqual(len(message_set), 2)
        message_set.clear_messages()
        self.assertEqual(len(message_set), 0)
        self.assertIsNone(message_set.get_message_by_time(1.0))
        with self.assertRaises(TypeError):
            message_set.add_message(object())


class ChatBotHelpersTest(unittest.IsolatedAsyncioTestCase):
    async def test_apply_modified_message_filters_notices_and_echo_updates_are_isolated(self) -> None:
        from src.chat.message_receive import bot as bot_module

        message = make_recv(processed_plain_text="before")
        message.message_info.additional_config = "raw"
        modified = MaiMessages(additional_data={"at_bot": True, "custom": "value"})
        modified.modify_message_segments([Seg(type="text", data="after")], suppress_warning=True)
        modified.modify_plain_text("after", suppress_warning=True)

        bot_module._apply_modified_message(message, modified, merge_additional_data=True)

        self.assertEqual(message.processed_plain_text, "after")
        self.assertEqual(message.message_segment.type, "seglist")
        self.assertEqual(message.message_info.additional_config, {"at_bot": True, "custom": "value"})

        user_info = message.message_info.user_info
        group_info = message.message_info.group_info
        with patch.object(
            bot_module.global_config,
            "message_receive",
            SimpleNamespace(ban_words=["blocked"], ban_msgs_regex=[r"spam-\d+"]),
        ):
            self.assertTrue(bot_module._check_ban_words("this is blocked", user_info, group_info))
            self.assertFalse(bot_module._check_ban_words("allowed", user_info, group_info))
            self.assertTrue(bot_module._check_ban_regex("spam-42", user_info, group_info))
            self.assertFalse(bot_module._check_ban_regex("", user_info, group_info))
            self.assertFalse(bot_module._check_ban_regex(None, user_info, group_info))

        chat_bot = bot_module.ChatBot.__new__(bot_module.ChatBot)
        notice = make_recv(message_id="notice")
        notice.message_segment = Seg(
            type="notify",
            data={
                "sub_type": "recall",
                "scene": "group",
                "message_id": "recalled-1",
                "recalled_user_info": {"user_id": "user-2", "user_nickname": "Bob"},
            },
        )
        self.assertTrue(await chat_bot.handle_notice_message(notice))
        self.assertTrue(notice.is_notify)
        self.assertIsNone(await chat_bot.handle_notice_message(make_recv(message_id="normal")))

        with patch.object(bot_module.MessageStorage, "update_message", return_value=True) as update_message:
            await chat_bot.echo_message_process({"content": {"type": "echo", "echo": "local", "actual_id": "platform"}})
        update_message.assert_called_once_with("local", "platform")

        with patch.object(bot_module.MessageStorage, "update_message") as update_message:
            await chat_bot.echo_message_process({"content": {"type": "text"}})
            await chat_bot.echo_message_process({})
        update_message.assert_not_called()

        with patch.object(bot_module.MessageStorage, "update_message", return_value=False) as update_message:
            await chat_bot.echo_message_process({"content": {"type": "echo", "echo": "local", "actual_id": "platform"}})
        update_message.assert_called_once_with("local", "platform")

    async def test_notice_message_handles_self_recall_other_notice_and_parse_errors(self) -> None:
        from src.chat.message_receive import bot as bot_module

        chat_bot = bot_module.ChatBot.__new__(bot_module.ChatBot)
        self_recall = make_recv(message_id="notice")
        self_recall.message_segment = Seg(
            type="notify",
            data={
                "sub_type": "recall",
                "message_id": "recalled-self",
                "recalled_user_info": {"user_id": "user-1", "user_nickname": "Alice"},
            },
        )
        other_notice = make_recv(message_id="notice")
        other_notice.message_segment = Seg(type="notify", data={"sub_type": "poke", "scene": "group"})
        broken_notice = make_recv(message_id="notice")
        broken_notice.message_info = SimpleNamespace(
            message_id="notice",
            user_info=object(),
            group_info=SimpleNamespace(group_id="group-1"),
        )
        broken_notice.message_segment = SimpleNamespace(
            type="notify",
            data=SimpleNamespace(get=Mock(side_effect=RuntimeError("bad notice"))),
        )

        self.assertTrue(await chat_bot.handle_notice_message(self_recall))
        self.assertTrue(await chat_bot.handle_notice_message(other_notice))
        self.assertTrue(await chat_bot.handle_notice_message(broken_notice))
        self.assertTrue(broken_notice.is_notify)

    async def test_process_commands_handles_disabled_commands_and_before_hook_skip(self) -> None:
        from src.chat.message_receive import bot as bot_module

        class FakeCommand(BaseCommand):
            executed = False

            async def execute(self):
                self.__class__.executed = True
                return True, "executed", 2

        registry = SimpleNamespace(
            find_command_by_text=Mock(
                return_value=(FakeCommand, {"arg": "value"}, SimpleNamespace(plugin_name="plugin", name="cmd"))
            ),
            get_plugin_config=Mock(return_value={"trace": 1}),
        )
        announcements = SimpleNamespace(get_disabled_chat_commands=Mock(return_value=["cmd"]))
        chat_bot = bot_module.ChatBot.__new__(bot_module.ChatBot)
        message = make_recv(processed_plain_text="!cmd")
        message.update_chat_stream(SimpleNamespace(stream_id="stream-command"))

        with (
            patch.object(bot_module, "component_registry", registry),
            patch.object(bot_module, "global_announcement_manager", announcements),
        ):
            self.assertEqual(await chat_bot._process_commands(message), (False, None, True))

        self.assertFalse(message.is_command)
        self.assertFalse(FakeCommand.executed)

        modified = MaiMessages(
            additional_data={"execute_command": False, "response": "blocked", "continue_process": False}
        )
        events = SimpleNamespace(handle_mai_events=AsyncMock(return_value=(True, modified)))
        announcements.get_disabled_chat_commands.return_value = []
        message = make_recv(processed_plain_text="!cmd")
        message.update_chat_stream(SimpleNamespace(stream_id="stream-command"))

        with (
            patch.object(bot_module, "component_registry", registry),
            patch.object(bot_module, "global_announcement_manager", announcements),
            patch.object(bot_module, "events_manager", events),
        ):
            self.assertEqual(await chat_bot._process_commands(message), (True, "blocked", False))

        self.assertTrue(message.is_command)
        self.assertFalse(FakeCommand.executed)
        events.handle_mai_events.assert_awaited_once()

        events = SimpleNamespace(handle_mai_events=AsyncMock(return_value=(False, None)))
        message = make_recv(processed_plain_text="!cmd")
        message.update_chat_stream(SimpleNamespace(stream_id="stream-command"))
        with (
            patch.object(bot_module, "component_registry", registry),
            patch.object(bot_module, "global_announcement_manager", announcements),
            patch.object(bot_module, "events_manager", events),
        ):
            self.assertEqual(await chat_bot._process_commands(message), (True, None, False))

    async def test_process_commands_applies_hook_mutations_and_reports_execution_errors(self) -> None:
        from src.chat.message_receive import bot as bot_module

        class FakeCommand(BaseCommand):
            executed_groups = None

            async def execute(self):
                self.__class__.executed_groups = dict(self.matched_groups)
                return True, "original", 2

        class FailingCommand(BaseCommand):
            sent_texts = []

            async def execute(self):
                raise RuntimeError("boom")

            async def send_text(self, content, set_reply=False, reply_message=None, storage_message=True):
                self.__class__.sent_texts.append(content)
                return True

        class FakeEvents:
            def __init__(self, before_modified=None, after_modified=None):
                self.before_modified = before_modified
                self.after_modified = after_modified
                self.calls = []

            async def handle_mai_events(self, event_type, message, **kwargs):
                self.calls.append((event_type, kwargs.get("extra_data")))
                if event_type == EventType.ON_COMMAND_BEFORE_EXECUTE:
                    return True, self.before_modified
                if event_type == EventType.ON_COMMAND_AFTER_EXECUTE:
                    return True, self.after_modified
                return True, None

        def make_registry(command_class):
            return SimpleNamespace(
                find_command_by_text=Mock(
                    return_value=(command_class, {"arg": "value"}, SimpleNamespace(plugin_name="plugin", name="cmd"))
                ),
                get_plugin_config=Mock(return_value={}),
            )

        announcements = SimpleNamespace(get_disabled_chat_commands=Mock(return_value=[]))
        chat_bot = bot_module.ChatBot.__new__(bot_module.ChatBot)
        before_modified = MaiMessages(additional_data={"matched_groups": {"arg": "rewritten"}})
        after_modified = MaiMessages(
            additional_data={"response": "after", "intercept_message_level": 0, "continue_process": True}
        )
        events = FakeEvents(before_modified=before_modified, after_modified=after_modified)
        message = make_recv(processed_plain_text="!cmd")
        message.update_chat_stream(SimpleNamespace(stream_id="stream-command"))

        with (
            patch.object(bot_module, "component_registry", make_registry(FakeCommand)),
            patch.object(bot_module, "global_announcement_manager", announcements),
            patch.object(bot_module, "events_manager", events),
        ):
            self.assertEqual(await chat_bot._process_commands(message), (True, "after", True))

        self.assertEqual(FakeCommand.executed_groups, {"arg": "rewritten"})
        self.assertEqual(message.intercept_message_level, 0)
        self.assertEqual(
            [call[0] for call in events.calls],
            [EventType.ON_COMMAND_BEFORE_EXECUTE, EventType.ON_COMMAND_AFTER_EXECUTE],
        )
        self.assertEqual(events.calls[1][1]["success"], True)
        self.assertEqual(events.calls[1][1]["intercept_message_level"], 2)

        failing_events = FakeEvents()
        FailingCommand.sent_texts = []
        message = make_recv(processed_plain_text="!cmd")
        message.update_chat_stream(SimpleNamespace(stream_id="stream-command"))
        with (
            patch.object(bot_module, "component_registry", make_registry(FailingCommand)),
            patch.object(bot_module, "global_announcement_manager", announcements),
            patch.object(bot_module, "events_manager", failing_events),
        ):
            self.assertEqual(await chat_bot._process_commands(message), (True, "命令执行失败，请稍后重试", False))

        self.assertEqual(FailingCommand.sent_texts, ["命令执行出错: 命令执行失败，请稍后重试"])
        self.assertEqual(failing_events.calls[1][1]["success"], False)
        self.assertEqual(failing_events.calls[1][1]["response"], "命令执行失败，请稍后重试")

    async def test_process_commands_after_hook_cancellations_and_outer_failures_are_safe(self) -> None:
        from src.chat.message_receive import bot as bot_module

        class FakeCommand(BaseCommand):
            async def execute(self):
                return True, "done", 0

        class FailingCommand(BaseCommand):
            sent_texts = []

            async def execute(self):
                raise RuntimeError("boom")

            async def send_text(self, content, set_reply=False, reply_message=None, storage_message=True):
                self.__class__.sent_texts.append(content)
                raise RuntimeError("send failed")

        def make_registry(command_class):
            return SimpleNamespace(
                find_command_by_text=Mock(
                    return_value=(command_class, {"arg": "value"}, SimpleNamespace(plugin_name="plugin", name="cmd"))
                ),
                get_plugin_config=Mock(return_value={}),
            )

        class AfterCancelEvents:
            async def handle_mai_events(self, event_type, message, **kwargs):
                if event_type == EventType.ON_COMMAND_AFTER_EXECUTE:
                    return False, None
                return True, None

        class AfterModifyEvents:
            async def handle_mai_events(self, event_type, message, **kwargs):
                if event_type == EventType.ON_COMMAND_AFTER_EXECUTE:
                    return True, MaiMessages(additional_data={"success": False, "response": "rewritten"})
                return True, None

        announcements = SimpleNamespace(get_disabled_chat_commands=Mock(return_value=[]))
        chat_bot = bot_module.ChatBot.__new__(bot_module.ChatBot)
        message = make_recv(processed_plain_text="!cmd")
        message.update_chat_stream(SimpleNamespace(stream_id="stream-command"))

        with (
            patch.object(bot_module, "component_registry", make_registry(FakeCommand)),
            patch.object(bot_module, "global_announcement_manager", announcements),
            patch.object(bot_module, "events_manager", AfterCancelEvents()),
        ):
            self.assertEqual(await chat_bot._process_commands(message), (True, "done", False))

        FailingCommand.sent_texts = []
        message = make_recv(processed_plain_text="!cmd")
        message.update_chat_stream(SimpleNamespace(stream_id="stream-command"))
        with (
            patch.object(bot_module, "component_registry", make_registry(FailingCommand)),
            patch.object(bot_module, "global_announcement_manager", announcements),
            patch.object(bot_module, "events_manager", AfterModifyEvents()),
        ):
            self.assertEqual(await chat_bot._process_commands(message), (True, "rewritten", False))

        self.assertEqual(FailingCommand.sent_texts, ["命令执行出错: rewritten"])

        with patch.object(
            bot_module.component_registry,
            "find_command_by_text",
            side_effect=RuntimeError("registry down"),
        ):
            self.assertEqual(
                await chat_bot._process_commands(make_recv(processed_plain_text="!cmd")), (False, None, True)
            )

    async def test_message_process_runs_hooked_pipeline_and_stores_command_skip(self) -> None:
        from src.chat.message_receive import bot as bot_module

        stream = make_stream()
        manager = SimpleNamespace(register_message=Mock(), get_or_create_stream=AsyncMock(return_value=stream))
        events = SimpleNamespace(handle_mai_events=AsyncMock(return_value=(True, None)))
        chat_bot = bot_module.ChatBot.__new__(bot_module.ChatBot)
        chat_bot._started = False
        chat_bot.heartflow_message_receiver = SimpleNamespace(process_message=AsyncMock())

        message_data = make_recv(processed_plain_text="hello").to_dict()
        message_data["message_info"]["group_info"]["group_id"] = 123
        message_data["message_info"]["user_info"]["user_id"] = 456

        with (
            patch.object(bot_module, "events_manager", events),
            patch.object(bot_module, "get_chat_manager", return_value=manager),
            patch.object(bot_module, "_check_ban_words", return_value=False),
            patch.object(bot_module, "_check_ban_regex", return_value=False),
            patch.object(chat_bot, "_process_commands", new=AsyncMock(return_value=(False, None, True))) as commands,
        ):
            await chat_bot.message_process(message_data)

        self.assertTrue(chat_bot._started)
        manager.register_message.assert_called_once()
        manager.get_or_create_stream.assert_awaited_once()
        self.assertEqual(manager.get_or_create_stream.await_args.kwargs["group_info"].group_id, "123")
        self.assertEqual(manager.get_or_create_stream.await_args.kwargs["user_info"].user_id, "456")
        commands.assert_awaited_once()
        chat_bot.heartflow_message_receiver.process_message.assert_awaited_once()
        self.assertEqual(events.handle_mai_events.await_count, 4)

        chat_bot = bot_module.ChatBot.__new__(bot_module.ChatBot)
        chat_bot._started = True
        chat_bot.heartflow_message_receiver = SimpleNamespace(process_message=AsyncMock())
        manager = SimpleNamespace(register_message=Mock(), get_or_create_stream=AsyncMock(return_value=stream))
        events = SimpleNamespace(handle_mai_events=AsyncMock(return_value=(True, None)))

        with (
            patch.object(bot_module, "events_manager", events),
            patch.object(bot_module, "get_chat_manager", return_value=manager),
            patch.object(bot_module, "_check_ban_words", return_value=False),
            patch.object(bot_module, "_check_ban_regex", return_value=False),
            patch.object(chat_bot, "_process_commands", new=AsyncMock(return_value=(True, "done", False))),
            patch.object(bot_module.MessageStorage, "store_message", new=AsyncMock()) as store_message,
        ):
            await chat_bot.message_process(make_recv(processed_plain_text="!cmd").to_dict())

        store_message.assert_awaited_once()
        chat_bot.heartflow_message_receiver.process_message.assert_not_awaited()

    async def test_message_process_early_returns_filters_template_scope_and_exception_guard(self) -> None:
        from src.chat.message_receive import bot as bot_module

        stream = make_stream()
        message_data = make_recv(processed_plain_text="hello").to_dict()

        chat_bot = bot_module.ChatBot.__new__(bot_module.ChatBot)
        chat_bot._started = True
        chat_bot.heartflow_message_receiver = SimpleNamespace(process_message=AsyncMock())
        pre_cancel_events = SimpleNamespace(handle_mai_events=AsyncMock(side_effect=[(False, None)]))
        with patch.object(bot_module, "events_manager", pre_cancel_events):
            await chat_bot.message_process(dict(message_data))
        chat_bot.heartflow_message_receiver.process_message.assert_not_awaited()

        chat_bot = bot_module.ChatBot.__new__(bot_module.ChatBot)
        chat_bot._started = True
        chat_bot.heartflow_message_receiver = SimpleNamespace(process_message=AsyncMock())
        before_cancel_events = SimpleNamespace(handle_mai_events=AsyncMock(side_effect=[(True, None), (False, None)]))
        with patch.object(bot_module, "events_manager", before_cancel_events):
            await chat_bot.message_process(dict(message_data))
        chat_bot.heartflow_message_receiver.process_message.assert_not_awaited()

        chat_bot = bot_module.ChatBot.__new__(bot_module.ChatBot)
        chat_bot._started = True
        chat_bot.heartflow_message_receiver = SimpleNamespace(process_message=AsyncMock())
        after_cancel_events = SimpleNamespace(
            handle_mai_events=AsyncMock(side_effect=[(True, None), (True, None), (False, None)])
        )
        with patch.object(bot_module, "events_manager", after_cancel_events):
            await chat_bot.message_process(dict(message_data))
        chat_bot.heartflow_message_receiver.process_message.assert_not_awaited()

        manager = SimpleNamespace(register_message=Mock(), get_or_create_stream=AsyncMock(return_value=stream))
        chat_bot = bot_module.ChatBot.__new__(bot_module.ChatBot)
        chat_bot._started = True
        chat_bot.heartflow_message_receiver = SimpleNamespace(process_message=AsyncMock())
        with (
            patch.object(
                bot_module, "events_manager", SimpleNamespace(handle_mai_events=AsyncMock(return_value=(True, None)))
            ),
            patch.object(bot_module, "get_chat_manager", return_value=manager),
            patch.object(bot_module, "_check_ban_words", return_value=True),
            patch.object(bot_module, "_check_ban_regex", return_value=False),
        ):
            await chat_bot.message_process(make_recv(processed_plain_text="blocked").to_dict())
        manager.register_message.assert_not_called()

        template_data = make_recv(processed_plain_text="templated").to_dict()
        template_data["message_info"]["template_info"] = {
            "template_default": False,
            "template_name": "custom-scope",
            "template_items": {"rule": "value"},
        }
        scope_calls = []
        prompt_manager = SimpleNamespace(
            async_message_scope=Mock(side_effect=lambda name: AsyncScope(name, scope_calls)),
            register_context_prompts=Mock(),
        )
        manager = SimpleNamespace(register_message=Mock(), get_or_create_stream=AsyncMock(return_value=stream))
        chat_bot = bot_module.ChatBot.__new__(bot_module.ChatBot)
        chat_bot._started = True
        chat_bot.heartflow_message_receiver = SimpleNamespace(process_message=AsyncMock())
        with (
            patch.object(
                bot_module, "events_manager", SimpleNamespace(handle_mai_events=AsyncMock(return_value=(True, None)))
            ),
            patch.object(bot_module, "get_chat_manager", return_value=manager),
            patch.object(bot_module, "_check_ban_words", return_value=False),
            patch.object(bot_module, "_check_ban_regex", return_value=False),
            patch.object(chat_bot, "_process_commands", new=AsyncMock(return_value=(False, None, True))),
            patch.object(bot_module, "prompt_manager", prompt_manager),
        ):
            await chat_bot.message_process(template_data)

        self.assertEqual(scope_calls, ["custom-scope"])
        prompt_manager.register_context_prompts.assert_called_once_with("custom-scope", {"rule": "value"})
        chat_bot.heartflow_message_receiver.process_message.assert_awaited_once()

        chat_bot = bot_module.ChatBot.__new__(bot_module.ChatBot)
        chat_bot._started = True
        chat_bot.heartflow_message_receiver = SimpleNamespace(process_message=AsyncMock())
        with patch.object(
            bot_module, "events_manager", SimpleNamespace(handle_mai_events=AsyncMock(return_value=(True, None)))
        ):
            await chat_bot.message_process({"message_info": None})


class UniversalMessageSenderHelpersTest(unittest.IsolatedAsyncioTestCase):
    def test_parse_message_segments_handles_nested_dicts_forward_nodes_and_unknowns(self) -> None:
        forward_node = MessageBase(
            message_info=BaseMessageInfo(message_id="fwd-1"),
            message_segment=Seg(type="text", data="forward text"),
        ).to_dict()
        segment = Seg(
            type="seglist",
            data=[
                Seg(type="text", data="hello"),
                Seg(type="image", data="img64"),
                Seg(type="emoji", data="gif64"),
                Seg(type="voiceurl", data="https://voice.test/a.wav"),
                Seg(type="forward", data=[forward_node]),
            ],
        )

        self.assertEqual(uni_message_sender.parse_message_segments(None), [])
        self.assertEqual(
            uni_message_sender.parse_message_segments({"message_segment": {"type": "text", "data": "wrapped"}}),
            [{"type": "text", "data": "wrapped"}],
        )
        self.assertEqual(
            uni_message_sender.parse_message_segments({"type": "custom", "data": {"x": 1}}),
            [{"type": "unknown", "original_type": "custom", "data": "{'x': 1}"}],
        )

        parsed = uni_message_sender.parse_message_segments(segment)

        self.assertEqual(parsed[0], {"type": "text", "data": "hello"})
        self.assertEqual(parsed[1], {"type": "image", "data": "data:image/png;base64,img64"})
        self.assertEqual(parsed[2], {"type": "emoji", "data": "data:image/gif;base64,gif64"})
        self.assertEqual(parsed[3], {"type": "voice", "data": "https://voice.test/a.wav"})
        self.assertEqual(parsed[4]["type"], "forward")
        self.assertEqual(parsed[4]["data"][0]["content"], [{"type": "text", "data": "forward text"}])

    def test_parse_message_components_converts_all_supported_component_types(self) -> None:
        sequence = MessageComponentSequence(
            [
                TextComponent("hello"),
                ImageComponent(base64_data="img64"),
                EmojiComponent(base64_data="data:image/gif;base64,already"),
                VoiceComponent(transcript="voice text"),
                AtComponent(target_user_id="user-2", target_name="Bob"),
                ReplyComponent(target_message_id="msg-1", target_text="quoted"),
                FileComponent(name="doc.txt", size="12", url="https://file.test"),
                ForwardComponent(nodes=[MessageComponentSequence([TextComponent("nested")])]),
                SegmentListComponent(sequence=MessageComponentSequence([TextComponent("inner")])),
                UnknownComponent(segment_type="custom", data={"x": 1}),
            ]
        )

        parsed = uni_message_sender.parse_message_components(sequence)

        self.assertEqual(parsed[0], {"type": "text", "data": "hello"})
        self.assertEqual(parsed[1], {"type": "image", "data": "data:image/png;base64,img64"})
        self.assertEqual(parsed[2], {"type": "emoji", "data": "data:image/gif;base64,already"})
        self.assertEqual(parsed[3], {"type": "text", "data": "[语音：voice text]"})
        self.assertEqual(parsed[4], {"type": "at", "data": "Bob"})
        self.assertEqual(parsed[5], {"type": "reply", "data": {"message_id": "msg-1", "text": "quoted"}})
        self.assertEqual(
            parsed[6], {"type": "file", "data": {"name": "doc.txt", "size": "12", "url": "https://file.test"}}
        )
        self.assertEqual(parsed[7], {"type": "forward", "data": [{"content": [{"type": "text", "data": "nested"}]}]})
        self.assertEqual(parsed[8], {"type": "text", "data": "inner"})
        self.assertEqual(parsed[9], {"type": "unknown", "original_type": "custom", "data": "{'x': 1}"})

    async def test_apply_modified_message_and_local_send_branches_are_isolated(self) -> None:
        message = make_sending(platform="bilibili_live", group_id=None)
        modified_message = SimpleNamespace(
            _modify_flags=SimpleNamespace(modify_message_segments=True, modify_plain_text=True),
            message_segments=[Seg(type="text", data="changed")],
            plain_text="plain changed",
        )

        uni_message_sender._apply_modified_message(message, modified_message)

        self.assertEqual(message.message_segment.type, "seglist")
        self.assertEqual(message.message_components.components[0].text, "changed")
        self.assertEqual(message.processed_plain_text, "plain changed")
        self.assertTrue(await uni_message_sender._send_message(message, show_log=False))

    async def test_send_message_routes_webui_rich_text_api_fallback_and_errors(self) -> None:
        chat_manager = SimpleNamespace(broadcast=AsyncMock())
        message = make_sending(platform="webui", group_id="webui_virtual_group_unit")
        message.processed_plain_text = "sent text"
        message.message_segment = Seg(
            type="seglist", data=[Seg(type="image", data="img64"), Seg(type="text", data="caption")]
        )
        message.message_components = MessageComponentSequence(
            [ImageComponent(base64_data="img64"), TextComponent("caption")]
        )

        with patch.object(uni_message_sender, "get_webui_chat_broadcaster", return_value=(chat_manager, "webui")):
            self.assertTrue(await uni_message_sender._send_message(message, show_log=True))

        payload = chat_manager.broadcast.await_args.args[0]
        self.assertEqual(payload["message_type"], "rich")
        self.assertEqual(payload["segments"][0]["type"], "image")
        self.assertIsInstance(payload["timestamp"], float)
        self.assertEqual(payload["group_id"], "webui_virtual_group_unit")

        text_message = make_sending(platform="webui", group_id="group-1")
        text_message.processed_plain_text = "plain"
        text_message.message_components = MessageComponentSequence([TextComponent("plain")])
        with patch.object(uni_message_sender, "get_webui_chat_broadcaster", return_value=(chat_manager, "webui")):
            self.assertTrue(await uni_message_sender._send_message(text_message, show_log=False))
        self.assertIsNone(chat_manager.broadcast.await_args.args[0]["segments"])

        class FakeExtraServer:
            def __init__(self, running=True, result=True):
                self.running = running
                self.result = result
                self.sent = []

            def is_running(self):
                return self.running

            async def send_message(self, api_message):
                self.sent.append(api_message)
                return {"conn-1": self.result}

        legacy_error = RuntimeError("legacy down")
        fallback_server = FakeExtraServer(result=True)
        global_api = SimpleNamespace(
            send_message=AsyncMock(side_effect=legacy_error),
            extra_server=fallback_server,
            platform_map={"qq": "api-key"},
        )
        normal_message = make_sending(platform="qq", group_id="group-1")
        from src.config import config as config_module

        class FakeMessageDim:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeAPIMessageBase:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        fake_maim_message_submodule = SimpleNamespace(APIMessageBase=FakeAPIMessageBase, MessageDim=FakeMessageDim)
        import builtins

        real_import = builtins.__import__

        def import_fake_maim_message(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "maim_message.message":
                return fake_maim_message_submodule
            return real_import(name, globals, locals, fromlist, level)

        with (
            patch.object(uni_message_sender, "get_webui_chat_broadcaster", return_value=(None, None)),
            patch.object(uni_message_sender, "get_global_api", return_value=global_api),
            patch.object(config_module.global_config.maim_message, "enable_api_server", True),
            patch("builtins.__import__", side_effect=import_fake_maim_message),
            patch.object(uni_message_sender, "logger") as sender_log,
        ):
            self.assertTrue(await uni_message_sender._send_message(normal_message, show_log=True))
        self.assertNotIn("api-key", str(sender_log.mock_calls))
        self.assertEqual(len(fallback_server.sent), 1)

        disabled_api = SimpleNamespace(send_message=AsyncMock(side_effect=legacy_error), extra_server=fallback_server)
        with (
            patch.object(uni_message_sender, "get_webui_chat_broadcaster", return_value=(None, None)),
            patch.object(uni_message_sender, "get_global_api", return_value=disabled_api),
            patch.object(config_module.global_config.maim_message, "enable_api_server", False),
        ):
            with self.assertRaises(RuntimeError):
                await uni_message_sender._send_message(normal_message, show_log=False)

        no_key_api = SimpleNamespace(
            send_message=AsyncMock(side_effect=legacy_error),
            extra_server=FakeExtraServer(result=False),
            platform_map={},
        )
        with (
            patch.object(uni_message_sender, "get_webui_chat_broadcaster", return_value=(None, None)),
            patch.object(uni_message_sender, "get_global_api", return_value=no_key_api),
            patch.object(config_module.global_config.maim_message, "enable_api_server", True),
        ):
            with self.assertRaises(RuntimeError):
                await uni_message_sender._send_message(normal_message, show_log=False)

    def test_webui_broadcaster_import_fallback_and_virtual_group_detection(self) -> None:
        uni_message_sender._webui_chat_broadcaster = None
        original_modules = {
            name: sys.modules.get(name)
            for name in [
                "src.webui",
                "src.webui.chat_routes",
            ]
        }
        sys.modules.pop("src.webui.chat_routes", None)
        try:
            with patch.dict(sys.modules, {"src.webui": ModuleType("src.webui")}):
                self.assertEqual(uni_message_sender.get_webui_chat_broadcaster(), (None, None))
                self.assertEqual(uni_message_sender.get_webui_chat_broadcaster(), (None, None))
        finally:
            uni_message_sender._webui_chat_broadcaster = None
            for name, module in original_modules.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

        self.assertTrue(uni_message_sender.is_webui_virtual_group("webui_virtual_group_1"))
        self.assertFalse(uni_message_sender.is_webui_virtual_group(""))

    async def test_universal_sender_validates_required_message_fields_and_honors_event_cancellation(self) -> None:
        sender = uni_message_sender.UniversalMessageSender()
        message = make_sending()
        message.chat_stream = None
        with self.assertRaises(ValueError):
            await sender.send_message(message)

        message = make_sending()
        fake_events = SimpleNamespace(handle_mai_events=AsyncMock(return_value=(False, None)))

        with patch.dict(
            "sys.modules",
            {
                "src.plugin_system.core.events_manager": SimpleNamespace(events_manager=fake_events),
                "src.plugin_system.base.component_types": SimpleNamespace(
                    EventType=SimpleNamespace(POST_SEND_PRE_PROCESS="pre")
                ),
            },
        ):
            self.assertFalse(await sender.send_message(message))

        fake_events.handle_mai_events.assert_awaited_once()
        self.assertFalse(message.processed_plain_text)

        message = make_sending()
        message.message_info.message_id = None
        with self.assertRaises(ValueError):
            await sender.send_message(message)

    async def test_universal_sender_processes_sends_after_events_and_stores_when_enabled(self) -> None:
        sender = uni_message_sender.UniversalMessageSender()
        sender.storage = SimpleNamespace(store_message=AsyncMock())
        message = make_sending()
        fake_events = SimpleNamespace(handle_mai_events=AsyncMock(return_value=(True, None)))

        with (
            patch.dict(
                "sys.modules",
                {
                    "src.plugin_system.core.events_manager": SimpleNamespace(events_manager=fake_events),
                    "src.plugin_system.base.component_types": SimpleNamespace(
                        EventType=SimpleNamespace(POST_SEND_PRE_PROCESS="pre", POST_SEND="post", AFTER_SEND="after")
                    ),
                },
            ),
            patch.object(uni_message_sender, "_send_message", new=AsyncMock(return_value=True)) as send_message,
            patch.object(uni_message_sender, "calculate_typing_time", return_value=0.0),
            patch.object(uni_message_sender.asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            result = await sender.send_message(message, typing=True)

        self.assertTrue(result)
        self.assertEqual(message.processed_plain_text, "sent text")
        self.assertEqual(fake_events.handle_mai_events.await_count, 3)
        send_message.assert_awaited_once_with(message, show_log=True)
        sleep.assert_awaited_once_with(0.0)
        sender.storage.store_message.assert_awaited_once_with(message, message.chat_stream)

    async def test_universal_sender_set_reply_post_cancel_after_cancel_no_storage_and_error_propagation(self) -> None:
        reply = make_recv(message_id="reply-1", processed_plain_text="quoted")
        message = make_sending()
        message.reply = reply
        sender = uni_message_sender.UniversalMessageSender()
        sender.storage = SimpleNamespace(store_message=AsyncMock())
        post_cancel_events = SimpleNamespace(handle_mai_events=AsyncMock(side_effect=[(True, None), (False, None)]))

        with (
            patch.dict(
                "sys.modules",
                {
                    "src.plugin_system.core.events_manager": SimpleNamespace(events_manager=post_cancel_events),
                    "src.plugin_system.base.component_types": SimpleNamespace(
                        EventType=SimpleNamespace(POST_SEND_PRE_PROCESS="pre", POST_SEND="post", AFTER_SEND="after")
                    ),
                },
            ),
            patch.object(uni_message_sender, "_send_message", new=AsyncMock()) as send_message,
        ):
            self.assertFalse(await sender.send_message(message, set_reply=True))

        self.assertEqual(message.reply_to_message_id, "reply-1")
        send_message.assert_not_awaited()
        sender.storage.store_message.assert_not_awaited()

        message = make_sending()
        sender = uni_message_sender.UniversalMessageSender()
        sender.storage = SimpleNamespace(store_message=AsyncMock())
        after_cancel_events = SimpleNamespace(
            handle_mai_events=AsyncMock(side_effect=[(True, None), (True, None), (False, None)])
        )
        with (
            patch.dict(
                "sys.modules",
                {
                    "src.plugin_system.core.events_manager": SimpleNamespace(events_manager=after_cancel_events),
                    "src.plugin_system.base.component_types": SimpleNamespace(
                        EventType=SimpleNamespace(POST_SEND_PRE_PROCESS="pre", POST_SEND="post", AFTER_SEND="after")
                    ),
                },
            ),
            patch.object(uni_message_sender, "_send_message", new=AsyncMock(return_value=True)),
        ):
            self.assertTrue(await sender.send_message(message, storage_message=True))
        sender.storage.store_message.assert_not_awaited()

        message = make_sending()
        normal_events = SimpleNamespace(handle_mai_events=AsyncMock(return_value=(True, None)))
        with (
            patch.dict(
                "sys.modules",
                {
                    "src.plugin_system.core.events_manager": SimpleNamespace(events_manager=normal_events),
                    "src.plugin_system.base.component_types": SimpleNamespace(
                        EventType=SimpleNamespace(POST_SEND_PRE_PROCESS="pre", POST_SEND="post", AFTER_SEND="after")
                    ),
                },
            ),
            patch.object(uni_message_sender, "_send_message", new=AsyncMock(return_value=False)),
        ):
            self.assertFalse(await sender.send_message(message, storage_message=True))

        message = make_sending()
        with (
            patch.dict(
                "sys.modules",
                {
                    "src.plugin_system.core.events_manager": SimpleNamespace(events_manager=normal_events),
                    "src.plugin_system.base.component_types": SimpleNamespace(
                        EventType=SimpleNamespace(POST_SEND_PRE_PROCESS="pre", POST_SEND="post", AFTER_SEND="after")
                    ),
                },
            ),
            patch.object(message, "process", new=AsyncMock(side_effect=RuntimeError("process down"))),
        ):
            with self.assertRaises(RuntimeError):
                await sender.send_message(message)


if __name__ == "__main__":
    unittest.main()
