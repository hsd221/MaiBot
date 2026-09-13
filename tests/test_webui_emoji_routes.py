import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from peewee import SqliteDatabase
from PIL import Image

from src.common.database.database_model import BaseModel, Emoji
from src.webui import emoji_routes


TEST_MODELS = [Emoji]


class FakeUploadFile:
    def __init__(self, filename: str, content_type: str | None, content: bytes) -> None:
        self.filename = filename
        self.content_type = content_type
        self._content = content

    async def read(self, size: int = -1) -> bytes:
        return self._content if size < 0 else self._content[:size]


class FakeImage:
    def __init__(self, size: tuple[int, int], image_format: str = "PNG") -> None:
        self.size = size
        self.format = image_format

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def verify(self) -> None:
        return None


def png_bytes(color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(output, format="PNG")
    return output.getvalue()


class EmojiRoutesTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source_dir = self.root / "source"
        self.source_dir.mkdir()
        self.thumbnail_dir = self.root / "thumbs"
        self.registered_dir = self.root / "registered"
        self.test_db = SqliteDatabase(":memory:")
        self.original_dbs = {model: model._meta.database for model in [BaseModel, *TEST_MODELS]}
        self.test_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
        self.test_db.connect()
        self.test_db.create_tables(TEST_MODELS)
        self.auth_patch = patch.object(emoji_routes, "verify_auth_token", return_value=True)
        self.cache_dir_patch = patch.object(emoji_routes, "THUMBNAIL_CACHE_DIR", self.thumbnail_dir)
        self.registered_dir_patch = patch.object(emoji_routes, "EMOJI_REGISTERED_DIR", str(self.registered_dir))
        self.auth_patch.start()
        self.cache_dir_patch.start()
        self.registered_dir_patch.start()

        async def delete_from_test_database(emoji_hash):
            return bool(Emoji.delete().where(Emoji.emoji_hash == emoji_hash).execute())

        self.manager_patch = patch(
            "src.chat.emoji_system.emoji_manager.get_emoji_manager",
            return_value=SimpleNamespace(delete_emoji=AsyncMock(side_effect=delete_from_test_database)),
        )
        self.manager_patch.start()
        self.addCleanup(self.manager_patch.stop)
        emoji_routes._thumbnail_locks.clear()
        emoji_routes._thumbnail_lock_users.clear()
        emoji_routes._generating_thumbnails.clear()

    def tearDown(self) -> None:
        emoji_routes._thumbnail_locks.clear()
        emoji_routes._thumbnail_lock_users.clear()
        emoji_routes._generating_thumbnails.clear()
        self.registered_dir_patch.stop()
        self.cache_dir_patch.stop()
        self.auth_patch.stop()
        self.test_db.drop_tables(TEST_MODELS)
        self.test_db.close()
        for model, database in self.original_dbs.items():
            model._meta.set_database(database)
        self.tmp.cleanup()

    def create_image_file(self, filename: str = "emoji.png", color: tuple[int, int, int] = (255, 0, 0)) -> Path:
        path = self.source_dir / filename
        path.write_bytes(png_bytes(color))
        return path

    def create_emoji(
        self,
        *,
        filename: str = "emoji.png",
        emoji_hash: str = "hash-1",
        description: str = "happy cat",
        is_registered: bool = False,
        is_banned: bool = False,
        usage_count: int = 0,
        record_time: float = 1.0,
        register_time: float | None = None,
        emotion: str | None = "happy",
    ) -> Emoji:
        source_path = self.create_image_file(filename)
        return Emoji.create(
            full_path=str(source_path),
            format=source_path.suffix.lstrip("."),
            emoji_hash=emoji_hash,
            description=description,
            query_count=1,
            is_registered=is_registered,
            is_banned=is_banned,
            emotion=emotion,
            record_time=record_time,
            register_time=register_time,
            usage_count=usage_count,
            last_used_time=2.0 if usage_count else None,
        )


class EmojiCrudRoutesTest(EmojiRoutesTestCase):
    async def test_list_detail_stats_update_register_ban_delete_and_batch_routes_use_database_state(self) -> None:
        first = self.create_emoji(
            filename="first.png",
            emoji_hash="hash-first",
            description="happy cat",
            is_registered=False,
            is_banned=True,
            usage_count=5,
            record_time=1.0,
        )
        second = self.create_emoji(
            filename="second.png",
            emoji_hash="hash-second",
            description="sad dog",
            is_registered=True,
            is_banned=False,
            usage_count=2,
            record_time=3.0,
            register_time=4.0,
            emotion=None,
        )

        listed = await emoji_routes.get_emoji_list(
            page=1,
            page_size=10,
            search="cat",
            is_registered=None,
            is_banned=None,
            format=None,
            sort_by="usage_count",
            sort_order="desc",
        )
        self.assertEqual(listed.total, 1)
        self.assertEqual(listed.data[0].emoji_hash, "hash-first")
        self.assertEqual(listed.data[0].emotion, "happy")

        filtered = await emoji_routes.get_emoji_list(
            page=1,
            page_size=10,
            search=None,
            is_registered=True,
            is_banned=False,
            format="png",
            sort_by="record_time",
            sort_order="asc",
        )
        self.assertEqual(filtered.total, 1)
        self.assertEqual(filtered.data[0].id, second.id)

        detail = await emoji_routes.get_emoji_detail(first.id)
        self.assertEqual(detail.data.description, "happy cat")
        with self.assertRaises(HTTPException) as missing_detail:
            await emoji_routes.get_emoji_detail(999)
        self.assertEqual(missing_detail.exception.status_code, 404)

        stats = await emoji_routes.get_emoji_stats()
        self.assertEqual(stats["data"]["total"], 2)
        self.assertEqual(stats["data"]["registered"], 1)
        self.assertEqual(stats["data"]["banned"], 1)
        self.assertEqual(stats["data"]["formats"], {"png": 2})
        self.assertEqual(stats["data"]["top_used"][0]["emoji_hash"], "hash-first")

        with patch.object(emoji_routes.time, "time", return_value=100.0):
            updated = await emoji_routes.update_emoji(
                first.id,
                emoji_routes.EmojiUpdateRequest(description="new", is_registered=True, emotion="joy"),
            )
        self.assertEqual(updated.data.description, "new")
        self.assertEqual(updated.data.register_time, 100.0)
        self.assertTrue(Emoji.get_by_id(first.id).is_registered)

        with self.assertRaises(HTTPException) as empty_update:
            await emoji_routes.update_emoji(first.id, emoji_routes.EmojiUpdateRequest())
        self.assertEqual(empty_update.exception.status_code, 400)

        with self.assertRaises(HTTPException) as already_registered:
            await emoji_routes.register_emoji(first.id)
        self.assertEqual(already_registered.exception.status_code, 400)

        third = self.create_emoji(
            filename="third.png",
            emoji_hash="hash-third",
            description="new bird",
            is_registered=False,
            is_banned=True,
            usage_count=1,
            record_time=5.0,
        )
        with patch.object(emoji_routes.time, "time", return_value=200.0):
            registered = await emoji_routes.register_emoji(third.id)
        self.assertTrue(registered.data.is_registered)
        self.assertFalse(registered.data.is_banned)
        self.assertEqual(registered.data.register_time, 200.0)

        banned = await emoji_routes.ban_emoji(second.id)
        self.assertTrue(banned.data.is_banned)
        self.assertFalse(banned.data.is_registered)

        deleted = await emoji_routes.delete_emoji(second.id)
        self.assertIn("成功删除表情包", deleted.message)
        with self.assertRaises(HTTPException) as missing_delete:
            await emoji_routes.delete_emoji(second.id)
        self.assertEqual(missing_delete.exception.status_code, 404)

        batch = await emoji_routes.batch_delete_emojis(emoji_routes.BatchDeleteRequest(emoji_ids=[first.id, 999]))
        self.assertEqual(batch.deleted_count, 1)
        self.assertEqual(batch.failed_count, 1)
        self.assertEqual(batch.failed_ids, [999])

        with self.assertRaises(HTTPException) as empty_batch:
            await emoji_routes.batch_delete_emojis(emoji_routes.BatchDeleteRequest(emoji_ids=[]))
        self.assertEqual(empty_batch.exception.status_code, 400)

    async def test_internal_failures_do_not_expose_database_details(self) -> None:
        secret = 'database error at /private/emoji.db: token="super-secret"'
        with (
            patch.object(emoji_routes.Emoji, "select", side_effect=RuntimeError(secret)),
            patch.object(emoji_routes.logger, "error") as logged,
            self.assertRaises(HTTPException) as failure,
        ):
            await emoji_routes.get_emoji_list(
                page=1,
                page_size=20,
                search=None,
                is_registered=None,
                is_banned=None,
                format=None,
                sort_by="usage_count",
                sort_order="desc",
            )

        self.assertEqual(failure.exception.status_code, 500)
        self.assertEqual(failure.exception.detail, "获取表情包列表失败")
        logged.assert_called_once()
        self.assertNotIn(secret, repr(logged.call_args))


class EmojiThumbnailRoutesTest(EmojiRoutesTestCase):
    async def test_thumbnail_helpers_generate_cleanup_stats_preheat_and_clear_cache(self) -> None:
        valid = self.create_emoji(filename="valid.png", emoji_hash="valid-hash", is_banned=False, usage_count=10)
        missing = Emoji.create(
            full_path=str(self.source_dir / "missing.png"),
            format="png",
            emoji_hash="missing-hash",
            description="missing",
            record_time=1.0,
            is_banned=False,
        )
        source_path = Path(valid.full_path)

        generated = emoji_routes._generate_thumbnail(str(source_path), valid.emoji_hash)
        self.assertTrue(generated.exists())
        self.assertEqual(generated.suffix, ".webp")
        # 同一哈希在持锁期间复用同一个 Lock，退出后立即回收，避免每张表情包永久占一个锁对象
        with emoji_routes._thumbnail_lock(valid.emoji_hash):
            outer_lock = emoji_routes._thumbnail_locks[valid.emoji_hash]
            with emoji_routes._thumbnail_lock(valid.emoji_hash + "-other"):
                pass
            self.assertIs(emoji_routes._thumbnail_locks[valid.emoji_hash], outer_lock)
            self.assertNotIn(valid.emoji_hash + "-other", emoji_routes._thumbnail_locks)
        self.assertEqual(emoji_routes._thumbnail_locks, {})
        self.assertEqual(emoji_routes._thumbnail_lock_users, {})

        (self.thumbnail_dir / "orphan-hash.webp").write_bytes(b"orphan")
        cleaned, kept = emoji_routes.cleanup_orphaned_thumbnails()
        self.assertEqual((cleaned, kept), (1, 1))
        self.assertFalse((self.thumbnail_dir / "orphan-hash.webp").exists())

        stats = await emoji_routes.get_thumbnail_cache_stats()
        self.assertEqual(stats.total_count, 1)
        self.assertEqual(stats.emoji_count, 2)
        self.assertEqual(stats.coverage_percent, 50.0)

        with patch.object(
            emoji_routes, "_generate_thumbnail", return_value=self.thumbnail_dir / "new.webp"
        ) as generate:
            preheated = await emoji_routes.preheat_thumbnail_cache(limit=5)
        self.assertEqual(preheated.generated_count, 0)
        self.assertEqual(preheated.skipped_count, 1)
        self.assertEqual(preheated.failed_count, 1)
        generate.assert_not_called()
        self.assertEqual(Emoji.get_by_id(missing.id).emoji_hash, "missing-hash")

        cleared = await emoji_routes.clear_all_thumbnail_cache()
        self.assertEqual(cleared.cleaned_count, 1)
        self.assertFalse(list(self.thumbnail_dir.glob("*.webp")))

        self.thumbnail_dir.rmdir()
        empty_clear = await emoji_routes.clear_all_thumbnail_cache()
        self.assertEqual(empty_clear.cleaned_count, 0)

    async def test_get_emoji_thumbnail_auth_original_cache_and_generation_paths(self) -> None:
        emoji = self.create_emoji(filename="thumb.png", emoji_hash="thumb-hash", is_registered=True)
        token_manager = SimpleNamespace(verify_token=Mock(side_effect=lambda token: token == "valid"))

        with patch.object(emoji_routes, "get_token_manager", return_value=token_manager):
            original = await emoji_routes.get_emoji_thumbnail(emoji.id, maibot_session="valid", original=True)
        self.assertEqual(original.path, emoji.full_path)
        self.assertEqual(original.media_type, "image/png")

        cache_path = emoji_routes._get_thumbnail_cache_path(emoji.emoji_hash)
        emoji_routes._ensure_thumbnail_cache_dir()
        cache_path.write_bytes(b"cached-webp")
        with patch.object(emoji_routes, "get_token_manager", return_value=token_manager):
            cached = await emoji_routes.get_emoji_thumbnail(emoji.id, maibot_session="valid", original=False)
        self.assertEqual(cached.path, str(cache_path))
        self.assertEqual(cached.media_type, "image/webp")

        cache_path.unlink()
        submit = Mock()
        with (
            patch.object(emoji_routes, "get_token_manager", return_value=token_manager),
            patch.object(emoji_routes._thumbnail_executor, "submit", submit),
        ):
            generating = await emoji_routes.get_emoji_thumbnail(emoji.id, authorization="Bearer valid", original=False)
        self.assertEqual(generating.status_code, 202)
        self.assertEqual(json.loads(generating.body)["status"], "generating")
        self.assertIn(emoji.emoji_hash, emoji_routes._generating_thumbnails)
        submit.assert_called_once()

        with patch.object(emoji_routes, "get_token_manager", return_value=token_manager):
            with self.assertRaises(HTTPException) as invalid_token:
                await emoji_routes.get_emoji_thumbnail(
                    emoji.id,
                    maibot_session=None,
                    authorization=None,
                    original=False,
                )
        self.assertEqual(invalid_token.exception.status_code, 401)

        Path(emoji.full_path).unlink()
        with patch.object(emoji_routes, "get_token_manager", return_value=token_manager):
            with self.assertRaises(HTTPException) as missing_file:
                await emoji_routes.get_emoji_thumbnail(emoji.id, maibot_session="valid", original=False)
        self.assertEqual(missing_file.exception.status_code, 404)


class EmojiAuthHelperTest(unittest.TestCase):
    def test_verify_auth_token_delegates_to_shared_auth_checker(self) -> None:
        with patch.object(emoji_routes, "verify_auth_token_from_cookie_or_header", return_value=True) as verify:
            self.assertTrue(emoji_routes.verify_auth_token("cookie", "Bearer token"))

        verify.assert_called_once_with("cookie", "Bearer token")


class EmojiUploadRoutesTest(EmojiRoutesTestCase):
    async def test_upload_emoji_validates_file_type_content_duplicates_and_persists_record(self) -> None:
        content = png_bytes()
        expected_hash = hashlib.md5(content).hexdigest()

        with patch.object(emoji_routes.time, "time", return_value=123.0):
            uploaded = await emoji_routes.upload_emoji(
                FakeUploadFile("cat.png", "image/png", content),
                description="cat",
                emotion=" happy, joy ",
                is_registered=True,
            )

        self.assertTrue(uploaded.success)
        self.assertEqual(uploaded.data.emoji_hash, expected_hash)
        self.assertEqual(uploaded.data.description, "cat")
        self.assertEqual(uploaded.data.emotion, "happy,joy")
        self.assertEqual(uploaded.data.record_time, 123.0)
        self.assertEqual(uploaded.data.register_time, 123.0)
        self.assertTrue(Path(uploaded.data.full_path).exists())

        with self.assertRaises(HTTPException) as duplicate:
            await emoji_routes.upload_emoji(
                FakeUploadFile("duplicate.png", "image/png", content),
                description="duplicate",
                emotion="",
                is_registered=False,
            )
        self.assertEqual(duplicate.exception.status_code, 409)

        for upload_file, expected_status in [
            (FakeUploadFile("unknown", None, content), 400),
            (FakeUploadFile("bad.txt", "text/plain", b"text"), 400),
            (FakeUploadFile("empty.png", "image/png", b""), 400),
            (FakeUploadFile("broken.png", "image/png", b"not image"), 400),
        ]:
            with self.subTest(filename=upload_file.filename):
                with self.assertRaises(HTTPException) as exc:
                    await emoji_routes.upload_emoji(upload_file, description="", emotion="", is_registered=False)
                self.assertEqual(exc.exception.status_code, expected_status)

    async def test_batch_upload_reports_success_duplicate_invalid_and_empty_files(self) -> None:
        first = png_bytes((255, 0, 0))
        duplicate = first
        second = png_bytes((0, 255, 0))

        with patch.object(emoji_routes.time, "time", return_value=456.0):
            result = await emoji_routes.batch_upload_emoji(
                [
                    FakeUploadFile("first.png", "image/png", first),
                    FakeUploadFile("duplicate.png", "image/png", duplicate),
                    FakeUploadFile("bad.txt", "text/plain", b"text"),
                    FakeUploadFile("empty.png", "image/png", b""),
                    FakeUploadFile("second.png", "image/png", second),
                ],
                emotion="fun, nice",
                is_registered=False,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["total"], 5)
        self.assertEqual(result["uploaded"], 2)
        self.assertEqual(result["failed"], 3)
        self.assertEqual(Emoji.select().count(), 2)
        self.assertEqual([detail["success"] for detail in result["details"]], [True, False, False, False, True])
        self.assertIn("已存在相同的表情包", result["details"][1]["error"])
        self.assertEqual(Emoji.select().first().emotion, "fun,nice")

    async def test_upload_rejects_oversized_files_before_image_decode(self) -> None:
        oversized = b"x" * (emoji_routes.MAX_EMOJI_FILE_BYTES + 1)

        with patch.object(emoji_routes.Image, "open") as image_open:
            with self.assertRaises(HTTPException) as blocked:
                await emoji_routes.upload_emoji(
                    FakeUploadFile("huge.png", "image/png", oversized),
                    description="",
                    emotion="",
                    is_registered=False,
                )

        self.assertEqual(blocked.exception.status_code, 413)
        image_open.assert_not_called()

    async def test_upload_rejects_excessive_image_dimensions(self) -> None:
        too_wide = FakeImage((emoji_routes.MAX_EMOJI_DIMENSION + 1, 1))

        with patch.object(emoji_routes.Image, "open", return_value=too_wide):
            with self.assertRaises(HTTPException) as blocked:
                await emoji_routes.upload_emoji(
                    FakeUploadFile("wide.png", "image/png", b"fake-image"),
                    description="",
                    emotion="",
                    is_registered=False,
                )

        self.assertEqual(blocked.exception.status_code, 413)
        self.assertFalse(list(self.registered_dir.glob("*")))

    async def test_batch_upload_rejects_more_than_supported_file_count(self) -> None:
        files = [FakeUploadFile(f"{index}.png", "image/png", png_bytes()) for index in range(21)]

        with self.assertRaises(HTTPException) as blocked:
            await emoji_routes.batch_upload_emoji(files, emotion="", is_registered=False)

        self.assertEqual(blocked.exception.status_code, 413)

    async def test_batch_upload_hides_internal_per_file_errors(self) -> None:
        with (
            patch.object(emoji_routes.Emoji, "get_or_none", side_effect=RuntimeError("database at /secret/path.db")),
            patch.object(emoji_routes.logger, "error") as logged,
        ):
            result = await emoji_routes.batch_upload_emoji(
                [FakeUploadFile("first.png", "image/png", png_bytes())],
                emotion="",
                is_registered=False,
            )

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["details"][0]["error"], "上传失败")
        self.assertNotIn("secret", str(result))
        logged.assert_called_once()
        self.assertNotIn("secret", repr(logged.call_args))


if __name__ == "__main__":
    unittest.main()
