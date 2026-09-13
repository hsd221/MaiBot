import base64
import hashlib
import io
import os
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from src.chat.emoji_system import emoji_manager


class StopPeriodicLoop(Exception):
    """用于在单轮扫描结束后停止无限循环测试。"""


def write_png(path: Path, color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    image = Image.new("RGB", (2, 2), color=color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    data = buffer.getvalue()
    path.write_bytes(data)
    return data


def png_base64(color: tuple[int, int, int] = (0, 255, 0)) -> str:
    image = Image.new("RGB", (2, 2), color=color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def gif_base64() -> str:
    first = Image.new("RGB", (2, 2), (255, 0, 0))
    second = Image.new("RGB", (2, 2), (0, 255, 0))
    buffer = io.BytesIO()
    first.save(buffer, format="GIF", save_all=True, append_images=[second], duration=100, loop=0)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def visual_observation(
    image_base64: str,
    description: str,
    *,
    image_format: str = "png",
    is_animated: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        description=description,
        image_hash=hashlib.md5(base64.b64decode(image_base64)).hexdigest(),
        image_format=image_format,
        is_animated=is_animated,
    )


def make_manager() -> emoji_manager.EmojiManager:
    manager = object.__new__(emoji_manager.EmojiManager)
    manager._initialized = True
    manager._scan_task = None
    manager.emoji_objects = []
    manager.emoji_num = 0
    manager.emoji_num_max = 10
    manager.emoji_num_max_reach_deletion = True
    manager.vlm = SimpleNamespace()
    manager.llm_emotion_judge = SimpleNamespace()
    manager.vector_index = SimpleNamespace(
        upsert=AsyncMock(return_value=True),
        delete=AsyncMock(return_value=True),
        search=AsyncMock(),
    )
    return manager


def make_emoji(
    emoji_hash: str,
    *,
    full_path: str = "/tmp/emoji.png",
    description: str = "描述",
    emotions: list[str] | None = None,
    usage_count: int = 0,
    is_deleted: bool = False,
) -> emoji_manager.MaiEmoji:
    emoji = emoji_manager.MaiEmoji(full_path)
    emoji.hash = emoji_hash
    emoji.description = description
    emoji.emotion = ["开心"] if emotions is None else emotions
    emoji.usage_count = usage_count
    emoji.last_used_time = 1.0
    emoji.register_time = 2.0
    emoji.format = "png"
    emoji.is_deleted = is_deleted
    return emoji


class MaiEmojiTest(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_hash_format_reads_existing_image_and_marks_missing_files_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "emoji.png"
            image_bytes = write_png(image_path)
            emoji = emoji_manager.MaiEmoji(str(image_path))

            self.assertTrue(await emoji.initialize_hash_format())

            self.assertEqual(emoji.hash, hashlib.md5(image_bytes).hexdigest())
            self.assertEqual(emoji.format, "png")
            self.assertFalse(emoji.is_deleted)

            missing = emoji_manager.MaiEmoji(str(Path(temp_dir) / "missing.png"))
            self.assertIsNone(await missing.initialize_hash_format())
            self.assertTrue(missing.is_deleted)

    async def test_register_to_db_moves_file_and_delete_removes_file_and_database_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir) / "source"
            registered_dir = Path(temp_dir) / "registered"
            source_dir.mkdir()
            registered_dir.mkdir()
            source_file = source_dir / "emoji.png"
            source_file.write_bytes(b"fake image")
            emoji = emoji_manager.MaiEmoji(str(source_file))
            emoji.hash = "hash-1"
            emoji.description = "desc"
            emoji.emotion = ["开心", "好笑"]
            emoji.format = "png"

            with (
                patch.object(emoji_manager, "EMOJI_REGISTERED_DIR", str(registered_dir)),
                patch.object(emoji_manager.Emoji, "create") as create_record,
            ):
                self.assertTrue(await emoji.register_to_db())

            self.assertFalse(source_file.exists())
            self.assertEqual(Path(emoji.full_path), registered_dir / "emoji.png")
            create_record.assert_called_once()
            self.assertEqual(create_record.call_args.kwargs["emoji_hash"], "hash-1")
            self.assertEqual(create_record.call_args.kwargs["emotion"], "开心,好笑")

            missing = emoji_manager.MaiEmoji(str(source_dir / "missing.png"))
            self.assertFalse(await missing.register_to_db())

            delete_record = SimpleNamespace(delete_instance=Mock(return_value=1))
            with patch.object(emoji_manager.Emoji, "get", return_value=delete_record):
                self.assertTrue(await emoji.delete())

            self.assertTrue(emoji.is_deleted)
            self.assertFalse(Path(emoji.full_path).exists())


class EmojiHelperFunctionTest(unittest.IsolatedAsyncioTestCase):
    async def test_clear_temp_emoji_preserves_pending_queue_and_cleans_transient_caches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            emoji_dir = base_dir / "emoji"
            image_dir = base_dir / "image"
            images_dir = base_dir / "images"
            for directory in (emoji_dir, image_dir, images_dir):
                directory.mkdir()
                for index in range(101):
                    (directory / f"{index}.tmp").write_text("x", encoding="utf-8")

            with patch.object(emoji_manager, "BASE_DIR", str(base_dir)):
                await emoji_manager.clear_temp_emoji()

            self.assertEqual(len(list(emoji_dir.iterdir())), 101)
            self.assertEqual(list(image_dir.iterdir()), [])
            self.assertEqual(list(images_dir.iterdir()), [])

    async def test_to_emoji_objects_readable_list_clean_unused_and_clear_temp_files(self) -> None:
        valid_record = SimpleNamespace(
            id=1,
            full_path="/tmp/emoji-1.png",
            emoji_hash="hash-1",
            description="desc",
            emotion="开心，难过",
            usage_count=3,
            last_used_time=None,
            register_time=123.0,
            format="png",
        )
        missing_path = SimpleNamespace(
            id=2,
            full_path="",
            emoji_hash="hash-2",
            description="desc",
            emotion="",
            usage_count=0,
            last_used_time=None,
            register_time=None,
            format="png",
        )
        missing_hash = SimpleNamespace(
            id=3,
            full_path="/tmp/emoji-3.png",
            emoji_hash="",
            description="desc",
            emotion="",
            usage_count=0,
            last_used_time=None,
            register_time=None,
            format="png",
        )

        objects, errors = emoji_manager._to_emoji_objects([valid_record, missing_path, missing_hash])

        self.assertEqual(len(objects), 1)
        self.assertEqual(errors, 2)
        self.assertEqual(objects[0].emotion, ["开心", "难过"])
        self.assertEqual(objects[0].usage_count, 3)
        self.assertIn("描述: desc", emoji_manager._emoji_objects_to_readable_list(objects)[0])

        with tempfile.TemporaryDirectory() as temp_dir:
            emoji_dir = Path(temp_dir) / "registered"
            emoji_dir.mkdir()
            tracked = emoji_dir / "tracked.png"
            untracked = emoji_dir / "untracked.png"
            tracked.write_bytes(b"tracked")
            untracked.write_bytes(b"untracked")
            os.utime(untracked, (1, 1))
            objects[0].full_path = str(tracked)

            removed_count = await emoji_manager.clean_unused_emojis(str(emoji_dir), objects, 2)

            self.assertEqual(removed_count, 3)
            self.assertTrue(tracked.exists())
            self.assertFalse(untracked.exists())
            self.assertEqual(await emoji_manager.clean_unused_emojis(str(emoji_dir / "missing"), objects, 5), 5)

        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            emoji_dir = base_dir / "emoji"
            image_dir = base_dir / "image"
            emoji_dir.mkdir()
            image_dir.mkdir()
            for index in range(101):
                (emoji_dir / f"{index}.tmp").write_text("x")
            for index in range(2):
                (image_dir / f"{index}.tmp").write_text("x")

            with patch.object(emoji_manager, "BASE_DIR", str(base_dir)):
                await emoji_manager.clear_temp_emoji()

            self.assertEqual(len(list(emoji_dir.iterdir())), 101)
            self.assertEqual(len(list(image_dir.iterdir())), 2)


class EmojiManagerPeriodicRegistrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_periodic_scan_processes_the_entire_pending_queue_before_sleeping(self) -> None:
        manager = make_manager()
        manager.get_all_emoji_from_db = AsyncMock()
        manager.check_emoji_file_integrity = AsyncMock()

        with tempfile.TemporaryDirectory() as temp_dir:
            pending_dir = Path(temp_dir)
            filenames = ["first.png", "removed-on-failure.png", "last.png"]
            for filename in filenames:
                (pending_dir / filename).write_bytes(b"image")

            async def register(filename: str) -> bool:
                if filename == "removed-on-failure.png":
                    (pending_dir / filename).unlink()
                    return False
                return True

            manager.register_emoji_by_filename = AsyncMock(side_effect=register)

            with (
                patch.object(emoji_manager, "EMOJI_DIR", str(pending_dir)),
                patch.object(emoji_manager.os, "listdir", return_value=filenames),
                patch.object(emoji_manager.global_config.emoji, "steal_emoji", True),
                patch.object(emoji_manager, "clear_temp_emoji", new=AsyncMock()),
                patch.object(emoji_manager.asyncio, "sleep", new=AsyncMock(side_effect=StopPeriodicLoop)),
                self.assertRaises(StopPeriodicLoop),
            ):
                await manager.start_periodic_check_register()

        self.assertEqual(
            [call.args[0] for call in manager.register_emoji_by_filename.await_args_list],
            filenames,
        )


class EmojiManagerLookupTest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_vector_selection_skips_search_and_index_writes(self) -> None:
        manager = make_manager()
        emoji = make_emoji("hash-a", emotions=["开心"])
        manager.emoji_objects = [emoji]

        with patch.object(
            emoji_manager.global_config.emoji,
            "vector_selection_enabled",
            False,
            create=True,
        ):
            self.assertIsNone(await manager.get_emoji_candidates_by_vector("开心", limit=5))
            await manager._index_emoji_vector(emoji)

        manager.vector_index.search.assert_not_awaited()
        manager.vector_index.upsert.assert_not_awaited()

    async def test_emoji_selection_distance_memory_and_database_fallbacks(self) -> None:
        manager = make_manager()
        happy = make_emoji("happy", full_path="/tmp/happy.png", description="happy desc", emotions=["开心", "快乐"])
        sad = make_emoji("sad", full_path="/tmp/sad.png", emotions=["难过"], is_deleted=True)
        manager.emoji_objects = [sad, make_emoji("empty", emotions=[]), happy]

        self.assertEqual(manager._levenshtein_distance("kitten", "sitting"), 3)
        self.assertEqual(manager._levenshtein_distance("", "abc"), 3)

        with (
            patch.object(manager, "record_usage") as record_usage,
            patch.object(emoji_manager.random, "choice", side_effect=lambda values: values[0]),
        ):
            result = await manager.get_emoji_for_text("开心")

        self.assertEqual(result, ("/tmp/happy.png", "[ happy desc ]", "开心"))
        record_usage.assert_called_once_with("happy")
        self.assertIs(await manager.get_emoji_from_manager("happy"), happy)
        self.assertIsNone(await manager.get_emoji_from_manager("sad"))
        self.assertEqual(await manager.get_emoji_tag_by_hash("happy"), ["开心", "快乐"])
        self.assertEqual(await manager.get_emoji_description_by_hash("happy"), "happy desc")

        manager.vector_index.search.return_value = [
            SimpleNamespace(emoji_hash="happy", emotions=("开心", "快乐"), similarity=0.93)
        ]
        vector_candidates = await manager.get_emoji_candidates_by_vector("轻松开心", limit=5)
        self.assertEqual(vector_candidates, [(happy, "开心、快乐", 0.93)])
        indexed_candidates = manager.vector_index.search.await_args.kwargs["candidates"]
        self.assertEqual([candidate.emoji_hash for candidate in indexed_candidates], ["happy"])

        manager.emoji_objects = []
        db_record = SimpleNamespace(emotion="喜悦，偷笑", description="db desc")
        with patch.object(emoji_manager.Emoji, "get_or_none", return_value=db_record):
            self.assertEqual(await manager.get_emoji_tag_by_hash("db-hash"), ["喜悦", "偷笑"])
            self.assertEqual(await manager.get_emoji_description_by_hash("db-hash"), "db desc")

        manager.emoji_objects = []
        self.assertIsNone(await manager.get_emoji_for_text("开心"))

    async def test_database_loading_queries_and_delete_emoji_update_manager_state(self) -> None:
        manager = make_manager()
        loaded = [make_emoji("hash-1")]

        with (
            patch.object(manager, "_ensure_db"),
            patch.object(emoji_manager.Emoji, "select", return_value=["record"]),
            patch.object(emoji_manager, "_to_emoji_objects", return_value=(loaded, 0)),
        ):
            await manager.get_all_emoji_from_db()

        self.assertEqual(manager.emoji_objects, loaded)
        self.assertEqual(manager.emoji_num, 1)

        fake_query = SimpleNamespace(where=Mock(return_value=["filtered"]))
        with (
            patch.object(manager, "_ensure_db"),
            patch.object(emoji_manager.Emoji, "select", return_value=fake_query),
            patch.object(emoji_manager, "_to_emoji_objects", return_value=(loaded, 0)) as to_objects,
        ):
            self.assertEqual(await manager.get_emoji_from_db("hash-1"), loaded)

        to_objects.assert_called_once_with(["filtered"])

        emoji = make_emoji("hash-1")
        emoji.delete = AsyncMock(return_value=True)
        manager.emoji_objects = [emoji]
        manager.emoji_num = 1

        self.assertTrue(await manager.delete_emoji("hash-1"))
        self.assertEqual(manager.emoji_objects, [])
        self.assertEqual(manager.emoji_num, 0)
        manager.vector_index.delete.assert_awaited_once_with("hash-1")

        self.assertFalse(await manager.delete_emoji("missing"))

        failed = make_emoji("hash-2")
        failed.delete = AsyncMock(return_value=False)
        manager.emoji_objects = [failed]
        manager.emoji_num = 1
        self.assertFalse(await manager.delete_emoji("hash-2"))
        self.assertEqual(manager.emoji_num, 1)

    async def test_replace_a_emoji_respects_llm_decision_and_registers_new_emoji_after_deletion(self) -> None:
        manager = make_manager()
        old = make_emoji("old", description="old desc", usage_count=3)
        manager.emoji_objects = [old]
        manager.emoji_num = 1
        manager.emoji_num_max = 1
        manager.llm_emotion_judge = SimpleNamespace(generate_response_async=AsyncMock(return_value=("删除编号1", None)))
        new = make_emoji("new", description="new desc")
        new.register_to_db = AsyncMock(return_value=True)
        manager.delete_emoji = AsyncMock(return_value=True)

        with (
            patch.object(emoji_manager.random, "choices", return_value=[old]),
            patch.object(
                emoji_manager.global_config,
                "bot",
                SimpleNamespace(nickname="Mai"),
            ),
        ):
            self.assertTrue(await manager.replace_a_emoji(new))

        manager.delete_emoji.assert_awaited_once_with("old")
        new.register_to_db.assert_awaited_once()
        self.assertIn(new, manager.emoji_objects)
        self.assertEqual(manager.emoji_num, 2)

        manager.llm_emotion_judge = SimpleNamespace(generate_response_async=AsyncMock(return_value=("不删除", None)))
        self.assertFalse(await manager.replace_a_emoji(make_emoji("skip")))

        manager.llm_emotion_judge = SimpleNamespace(generate_response_async=AsyncMock(return_value=("删除编号9", None)))
        self.assertFalse(await manager.replace_a_emoji(make_emoji("invalid")))


class EmojiDescriptionAndRegistrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_build_emoji_description_uses_shared_visual_recognition_before_semantic_processing(self) -> None:
        manager = make_manager()
        manager.vlm = SimpleNamespace(generate_response_for_image=AsyncMock())
        semantic_payload = json.dumps(
            {
                "emotion": ["疑惑"],
                "scene": "当没有理解对方时，用于表达疑问",
                "intent": "请求对方解释",
                "content": "人物歪头看向画面外",
                "text": "什么？",
                "style": "无明确梗或特殊风格",
            },
            ensure_ascii=False,
        )
        manager.llm_emotion_judge = SimpleNamespace(
            generate_response_async=AsyncMock(return_value=(semantic_payload, None))
        )
        shared_image_manager = SimpleNamespace(
            recognize_image=AsyncMock(
                return_value=SimpleNamespace(
                    description="人物歪头看向画面外，画面下方写着‘什么？’。",
                    image_hash="shared-hash",
                    image_format="png",
                    is_animated=False,
                )
            )
        )

        with (
            patch.object(emoji_manager, "get_image_manager", return_value=shared_image_manager),
            patch.object(emoji_manager.EmojiDescriptionCache, "get_or_none", return_value=None),
            patch.object(emoji_manager.EmojiDescriptionCache, "create"),
            patch.object(emoji_manager.global_config, "emoji", SimpleNamespace(content_filtration=False)),
        ):
            description, emotions = await manager.build_emoji_description(png_base64())

        self.assertTrue(description.startswith("[表情包：情感：疑惑；"))
        self.assertEqual(emotions, ["疑惑"])
        shared_image_manager.recognize_image.assert_awaited_once()
        manager.vlm.generate_response_for_image.assert_not_awaited()
        self.assertIn(
            "人物歪头看向画面外，画面下方写着‘什么？’。",
            manager.llm_emotion_judge.generate_response_async.await_args.args[0],
        )

    async def test_build_emoji_description_upgrades_cached_visual_description_and_handles_invalid_input(self) -> None:
        manager = make_manager()
        image_b64 = png_base64()
        manager.vlm = SimpleNamespace(generate_response_for_image=AsyncMock())
        semantic_payload = json.dumps(
            {
                "emotion": ["开心", "好笑", "无语"],
                "scene": "当群友说出离谱内容时，用于笑着吐槽",
                "intent": "表达不认同并活跃气氛",
                "content": "一只猫张嘴大笑后露出无语表情",
                "text": "你认真的？",
                "style": "猫 meme，反差幽默",
            },
            ensure_ascii=False,
        )
        manager.llm_emotion_judge = SimpleNamespace(
            generate_response_async=AsyncMock(return_value=(semantic_payload, None))
        )
        cache_record = SimpleNamespace(description="缓存描述", emotion_tags="", save=Mock())
        shared_image_manager = SimpleNamespace(
            recognize_image=AsyncMock(return_value=visual_observation(image_b64, "统一视觉描述"))
        )

        with (
            patch.object(emoji_manager, "get_image_manager", return_value=shared_image_manager),
            patch.object(emoji_manager.EmojiDescriptionCache, "get_or_none", return_value=cache_record),
            patch.object(
                emoji_manager.global_config,
                "emoji",
                SimpleNamespace(content_filtration=False),
            ),
        ):
            description, emotions = await manager.build_emoji_description(image_b64)

        semantic_description = (
            "情感：开心、好笑、无语；适用场景：当群友说出离谱内容时，用于笑着吐槽；"
            "表达意图：表达不认同并活跃气氛；画面内容：一只猫张嘴大笑后露出无语表情；"
            "画面文字：你认真的？；风格/梗：猫 meme，反差幽默"
        )
        self.assertEqual(description, f"[表情包：{semantic_description}]")
        self.assertEqual(emotions, ["开心", "好笑", "无语"])
        manager.vlm.generate_response_for_image.assert_not_awaited()
        semantic_prompt = manager.llm_emotion_judge.generate_response_async.await_args.args[0]
        self.assertIn("统一视觉描述", semantic_prompt)
        self.assertNotIn("缓存描述", semantic_prompt)
        self.assertEqual(cache_record.description, semantic_description)
        self.assertEqual(cache_record.emotion_tags, "开心,好笑,无语")
        cache_record.save.assert_called_once()

        self.assertEqual(await manager.build_emoji_description("not-base64"), ("", []))

    async def test_build_emoji_description_reuses_semantic_cache_without_another_llm_call(self) -> None:
        manager = make_manager()
        image_b64 = png_base64()
        manager.vlm = SimpleNamespace(generate_response_for_image=AsyncMock())
        manager.llm_emotion_judge = SimpleNamespace(generate_response_async=AsyncMock())
        semantic_description = (
            "情感：开心、好笑；适用场景：当朋友讲笑话时，用于表示被逗乐；"
            "表达意图：积极回应并延续轻松气氛；画面内容：小狗拍桌大笑；"
            "画面文字：哈哈哈；风格/梗：夸张反应图"
        )
        cache_record = SimpleNamespace(description=semantic_description, emotion_tags="", save=Mock())
        shared_image_manager = SimpleNamespace(
            recognize_image=AsyncMock(return_value=visual_observation(image_b64, "统一视觉描述"))
        )

        with (
            patch.object(emoji_manager, "get_image_manager", return_value=shared_image_manager),
            patch.object(emoji_manager.EmojiDescriptionCache, "get_or_none", return_value=cache_record),
            patch.object(emoji_manager.global_config, "emoji", SimpleNamespace(content_filtration=False)),
        ):
            description, emotions = await manager.build_emoji_description(image_b64)

        self.assertEqual(description, f"[表情包：{semantic_description}]")
        self.assertEqual(emotions, ["开心", "好笑"])
        manager.vlm.generate_response_for_image.assert_not_awaited()
        manager.llm_emotion_judge.generate_response_async.assert_not_awaited()
        self.assertEqual(cache_record.emotion_tags, "开心,好笑")
        cache_record.save.assert_called_once()

    async def test_build_emoji_description_audits_frames_after_unified_animated_recognition(self) -> None:
        manager = make_manager()
        image_b64 = gif_base64()
        manager.vlm = SimpleNamespace(
            generate_response_for_image=AsyncMock(),
            generate_response_for_images=AsyncMock(return_value=("是", None)),
        )
        semantic_payload = json.dumps(
            {
                "emotion": ["开心"],
                "scene": "当聊天气氛轻松时，用于表示开心回应",
                "intent": "积极接住对方的话题",
                "content": "角色连续点头并露出笑容",
                "text": "无文字",
                "style": "循环动态反应图",
            },
            ensure_ascii=False,
        )
        manager.llm_emotion_judge = SimpleNamespace(
            generate_response_async=AsyncMock(return_value=(semantic_payload, None))
        )
        stale_cache = SimpleNamespace(description="旧拼图描述", emotion_tags="旧情绪", save=Mock())
        shared_image_manager = SimpleNamespace(
            recognize_image=AsyncMock(
                return_value=visual_observation(
                    image_b64,
                    "角色先低头，随后连续点头并露出笑容。",
                    image_format="gif",
                    is_animated=True,
                )
            ),
            extract_gif_frames=Mock(return_value=["frame-1", "frame-2"]),
        )

        with (
            patch.object(emoji_manager, "get_image_manager", return_value=shared_image_manager),
            patch.object(emoji_manager.EmojiDescriptionCache, "get_or_none", side_effect=[stale_cache, stale_cache]),
            patch.object(
                emoji_manager.global_config,
                "emoji",
                SimpleNamespace(content_filtration=True, filtration_prompt="合规"),
            ),
        ):
            description, emotions = await manager.build_emoji_description(image_b64)

        semantic_description = (
            "情感：开心；适用场景：当聊天气氛轻松时，用于表示开心回应；"
            "表达意图：积极接住对方的话题；画面内容：角色连续点头并露出笑容；"
            "画面文字：无文字；风格/梗：循环动态反应图"
        )
        self.assertEqual(description, f"[表情包：{semantic_description}]")
        self.assertEqual(emotions, ["开心"])
        shared_image_manager.recognize_image.assert_awaited_once_with(image_b64)
        self.assertEqual(manager.vlm.generate_response_for_images.await_count, 1)
        args = manager.vlm.generate_response_for_images.await_args_list[0].args
        self.assertEqual([image_format for image_format, _ in args[1]], ["png", "png"])
        self.assertEqual(manager.vlm.generate_response_for_images.await_args_list[0].kwargs["max_tokens"], 1000)
        self.assertEqual(emoji_manager.read_gif_description_cache(stale_cache.description), semantic_description)
        manager.vlm.generate_response_for_image.assert_not_awaited()

    async def test_build_emoji_description_leaves_semantic_cache_untouched_when_audit_rejects(self) -> None:
        manager = make_manager()
        image_b64 = gif_base64()
        manager.vlm = SimpleNamespace(generate_response_for_images=AsyncMock(return_value=("否", None)))
        manager.llm_emotion_judge = SimpleNamespace(generate_response_async=AsyncMock())
        stale_cache = SimpleNamespace(description="旧拼图描述", emotion_tags="旧情绪", save=Mock())
        shared_image_manager = SimpleNamespace(
            recognize_image=AsyncMock(
                return_value=visual_observation(
                    image_b64,
                    "统一动态视觉描述",
                    image_format="gif",
                    is_animated=True,
                )
            ),
            extract_gif_frames=Mock(return_value=["frame-1", "frame-2"]),
        )

        with (
            patch.object(emoji_manager, "get_image_manager", return_value=shared_image_manager),
            patch.object(emoji_manager.EmojiDescriptionCache, "get_or_none", return_value=stale_cache),
            patch.object(
                emoji_manager.global_config,
                "emoji",
                SimpleNamespace(content_filtration=True, filtration_prompt="合规"),
            ),
        ):
            result = await manager.build_emoji_description(image_b64)

        self.assertEqual(result, ("", []))
        self.assertEqual(stale_cache.description, "旧拼图描述")
        self.assertEqual(stale_cache.emotion_tags, "旧情绪")
        stale_cache.save.assert_not_called()
        manager.llm_emotion_judge.generate_response_async.assert_not_awaited()

    async def test_build_emoji_description_audits_every_long_gif_batch_in_order(self) -> None:
        manager = make_manager()
        image_b64 = gif_base64()
        manager.vlm = SimpleNamespace(generate_response_for_images=AsyncMock(side_effect=[("是", None), ("否", None)]))
        manager.llm_emotion_judge = SimpleNamespace(generate_response_async=AsyncMock())
        image_manager = SimpleNamespace(
            recognize_image=AsyncMock(
                return_value=visual_observation(
                    image_b64,
                    "统一动态视觉描述",
                    image_format="gif",
                    is_animated=True,
                )
            ),
            extract_gif_frames=Mock(return_value=[f"frame-{index}" for index in range(17)]),
        )

        with (
            patch.object(emoji_manager, "get_image_manager", return_value=image_manager),
            patch.object(emoji_manager.EmojiDescriptionCache, "get_or_none", return_value=None),
            patch.object(
                emoji_manager.global_config,
                "emoji",
                SimpleNamespace(content_filtration=True, filtration_prompt="合规"),
            ),
        ):
            result = await manager.build_emoji_description(image_b64)

        self.assertEqual(result, ("", []))
        image_calls = manager.vlm.generate_response_for_images.await_args_list
        self.assertEqual([len(call.args[1]) for call in image_calls], [16, 1])
        self.assertEqual([call.kwargs["start_index"] for call in image_calls], [1, 17])
        manager.llm_emotion_judge.generate_response_async.assert_not_awaited()

    async def test_register_emoji_by_filename_handles_success_duplicates_replace_and_description_failures(self) -> None:
        manager = make_manager()

        with tempfile.TemporaryDirectory() as temp_dir:
            emoji_dir = Path(temp_dir) / "emoji"
            emoji_dir.mkdir()
            with patch.object(emoji_manager, "EMOJI_DIR", str(emoji_dir)):
                first = emoji_dir / "first.png"
                first_bytes = write_png(first)
                first_hash = hashlib.md5(first_bytes).hexdigest()

                manager.build_emoji_description = AsyncMock(return_value=("[表情包：desc]", ["开心"]))
                with patch.object(emoji_manager.MaiEmoji, "register_to_db", new=AsyncMock(return_value=True)):
                    self.assertTrue(await manager.register_emoji_by_filename("first.png"))

                self.assertEqual(manager.emoji_num, 1)
                self.assertEqual(manager.emoji_objects[0].hash, first_hash)
                self.assertEqual(manager.emoji_objects[0].emotion, ["开心"])
                manager.vector_index.upsert.assert_awaited_once_with(first_hash, ["开心"])
                manager.vector_index.upsert.reset_mock()

                duplicate = emoji_dir / "duplicate.png"
                duplicate.write_bytes(first_bytes)
                self.assertFalse(await manager.register_emoji_by_filename("duplicate.png"))
                self.assertFalse(duplicate.exists())

                manager.emoji_num = manager.emoji_num_max
                replacement = emoji_dir / "replacement.png"
                write_png(replacement, color=(0, 0, 255))
                manager.replace_a_emoji = AsyncMock(return_value=True)
                manager.build_emoji_description = AsyncMock(return_value=("[表情包：new]", ["新"]))

                self.assertTrue(await manager.register_emoji_by_filename("replacement.png"))
                manager.replace_a_emoji.assert_awaited_once()
                replacement_hash = hashlib.md5(replacement.read_bytes()).hexdigest()
                manager.vector_index.upsert.assert_awaited_once_with(replacement_hash, ["新"])

                failed_desc = emoji_dir / "failed.png"
                write_png(failed_desc, color=(255, 255, 0))
                manager.emoji_num = 0
                manager.build_emoji_description = AsyncMock(return_value=("", []))

                self.assertFalse(await manager.register_emoji_by_filename("failed.png"))
                self.assertFalse(failed_desc.exists())

                self.assertFalse(await manager.register_emoji_by_filename("missing.png"))


if __name__ == "__main__":
    unittest.main()
