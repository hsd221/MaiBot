import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from src.webui import chat_routes, emoji_routes, plugin_routes
import tests.test_chat_history_import_routes as import_tests
import tests.test_webui_chat_routes as chat_tests
import tests.test_webui_emoji_routes as emoji_tests
import tests.test_webui_plugin_routes as plugin_tests


class ImportCancellationAuditTest(unittest.IsolatedAsyncioTestCase):
    setUp = import_tests.ChatHistoryImportRoutesTest.setUp
    tearDown = import_tests.ChatHistoryImportRoutesTest.tearDown
    _upload = import_tests.ChatHistoryImportRoutesTest._upload

    async def test_cancel_does_not_overwrite_concurrently_completed_task(self):
        response = await self._upload()
        self.model.update(status="running", progress_stage="learning").where(
            self.model.import_id == response.import_id
        ).execute()
        stale = self.model.get(self.model.import_id == response.import_id)
        self.model.update(status="completed", result_json='{"done":true}').where(
            self.model.import_id == response.import_id
        ).execute()
        with patch.object(self.routes, "_get_task_or_404", return_value=stale):
            with self.assertRaises(HTTPException) as error:
                await self.routes.delete_chat_history_import(response.import_id, None, None)
        self.assertEqual(error.exception.status_code, 409)
        final = self.model.get(self.model.import_id == response.import_id)
        self.assertEqual(final.status, "completed")
        self.assertEqual(json.loads(final.result_json), {"done": True})

    async def test_cancel_does_not_interrupt_concurrently_started_commit(self):
        response = await self._upload()
        self.model.update(status="running", progress_stage="learning").where(
            self.model.import_id == response.import_id
        ).execute()
        stale = self.model.get(self.model.import_id == response.import_id)
        self.model.update(progress_stage="storing").where(self.model.import_id == response.import_id).execute()
        running = Mock()
        self.routes._running_tasks[response.import_id] = running
        with patch.object(self.routes, "_get_task_or_404", return_value=stale):
            with self.assertRaises(HTTPException) as error:
                await self.routes.delete_chat_history_import(response.import_id, None, None)
        self.assertEqual(error.exception.status_code, 409)
        running.cancel.assert_not_called()
        self.assertFalse(self.model.get(self.model.import_id == response.import_id).cancel_requested)

    async def test_delete_does_not_remove_a_task_started_after_read(self):
        response = await self._upload()
        stale = self.model.get(self.model.import_id == response.import_id)
        self.model.update(status="running", progress_stage="learning").where(
            self.model.import_id == response.import_id
        ).execute()
        with (
            patch.object(self.routes, "_get_task_or_404", return_value=stale),
            patch.object(self.routes, "_cleanup_task_files") as cleanup,
        ):
            with self.assertRaises(HTTPException) as error:
                await self.routes.delete_chat_history_import(response.import_id, None, None)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.model.get(self.model.import_id == response.import_id).status, "running")
        cleanup.assert_not_called()


class PluginListAuditTest(plugin_tests.PluginRouteBase):
    async def test_legacy_id_is_returned_without_mutating_manifest(self):
        path = self.plugins_dir / "Legacy"
        plugin_tests.write_manifest(path, {"name": "Legacy", "version": "1.0.0", "author": "Author"})
        before = (path / "_manifest.json").read_bytes()
        result = await plugin_routes.get_installed_plugins(**self.auth_kwargs())
        self.assertEqual(result["plugins"][0]["id"], "Author.Legacy")
        self.assertEqual((path / "_manifest.json").read_bytes(), before)


class EmojiDeletionAuditTest(emoji_tests.EmojiRoutesTestCase):
    async def test_single_and_batch_delete_use_manager(self):
        emoji = self.create_emoji(emoji_hash="one")
        manager = SimpleNamespace(delete_emoji=AsyncMock(return_value=True))
        with patch("src.chat.emoji_system.emoji_manager.get_emoji_manager", return_value=manager):
            await emoji_routes.delete_emoji(emoji.id)
            manager.delete_emoji.assert_awaited_once_with(emoji.emoji_hash)
            manager.delete_emoji.reset_mock()
            await emoji_routes.batch_delete_emojis(emoji_routes.BatchDeleteRequest(emoji_ids=[emoji.id]))
            manager.delete_emoji.assert_awaited_once_with(emoji.emoji_hash)


class VirtualIdentityAuditTest(unittest.IsolatedAsyncioTestCase):
    async def test_nonexistent_profile_cannot_activate_virtual_identity(self):
        websocket = chat_tests.FakeWebSocket(
            receive_items=[
                {
                    "type": "set_virtual_identity",
                    "config": {
                        "enabled": True,
                        "platform": "qq",
                        "person_id": "missing",
                        "user_id": "forged",
                        "nickname": "Forged",
                    },
                }
            ]
        )
        manager = chat_routes.ChatConnectionManager()
        with (
            patch.object(chat_routes, "chat_manager", manager),
            patch.object(chat_routes, "chat_history", SimpleNamespace(get_history=Mock(return_value=[]))),
            patch.object(chat_routes, "get_profile_person_dict", return_value=None),
            patch.object(chat_routes, "is_websocket_origin_allowed", return_value=True),
            patch.object(chat_routes, "verify_ws_token", return_value=True),
        ):
            await chat_routes.websocket_chat(
                websocket,
                user_id="test",
                user_name="Test",
                platform=None,
                person_id=None,
                group_name=None,
                group_id=None,
                token="test",
            )
        self.assertFalse(any(item["type"] == "virtual_identity_set" for item in websocket.sent))
        self.assertTrue(any(item["type"] == "error" for item in websocket.sent))

    async def test_existing_profile_ignores_forged_account_and_nickname(self):
        websocket = chat_tests.FakeWebSocket(
            receive_items=[
                {
                    "type": "set_virtual_identity",
                    "config": {
                        "enabled": True,
                        "platform": "qq",
                        "person_id": "qq:known",
                        "user_id": "forged",
                        "nickname": "Forged",
                    },
                }
            ]
        )
        person = {
            "person_id": "qq:known",
            "platform": "qq",
            "user_id": "known",
            "nickname": "Known",
            "person_name": "Known",
        }
        with (
            patch.object(chat_routes, "chat_manager", chat_routes.ChatConnectionManager()),
            patch.object(chat_routes, "chat_history", SimpleNamespace(get_history=Mock(return_value=[]))),
            patch.object(chat_routes, "get_profile_person_dict", return_value=person),
            patch.object(chat_routes, "is_websocket_origin_allowed", return_value=True),
            patch.object(chat_routes, "verify_ws_token", return_value=True),
        ):
            await chat_routes.websocket_chat(
                websocket,
                user_id="test",
                user_name="Test",
                platform=None,
                person_id=None,
                group_name=None,
                group_id=None,
                token="test",
            )
        activation = next(item for item in websocket.sent if item["type"] == "virtual_identity_set")
        self.assertEqual(activation["config"]["user_id"], "known")
        self.assertEqual(activation["config"]["user_nickname"], "Known")
