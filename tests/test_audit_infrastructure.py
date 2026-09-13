import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.common.toml_utils import save_toml_with_format
from src.common import message_repository
from src.manager.async_task_manager import AsyncTask
from src.manager.local_store_manager import LocalStoreManager


class StoreAuditTest(unittest.TestCase):
    def test_failed_replace_preserves_last_committed_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = LocalStoreManager(str(path))
            store["counter"] = 1
            with patch("os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    store["counter"] = 2
            self.assertEqual(json.loads(path.read_text()), {"counter": 1})
            self.assertEqual(store["counter"], 1)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_invalid_store_is_preserved_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"counter":')
            store = LocalStoreManager(str(path))
            backups = list(Path(directory).glob("state.json.corrupt.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), '{"counter":')
            self.assertEqual(store.store, {})

    def test_toml_replacement_does_not_inherit_world_readable_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('name = "old"\n')
            path.chmod(0o644)
            save_toml_with_format({"name": "new"}, str(path))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_query_default_is_bounded_but_explicit_zero_keeps_export_semantics(self):
        from tests.test_chat_utils_and_repository import MessageRepositoryTest, make_message_record

        fixture = MessageRepositoryTest()
        fixture.setUp()
        try:
            with fixture.db.atomic():
                for index in range(1005):
                    make_message_record(f"audit-{index}", float(index + 10))
            default = message_repository.find_messages({"chat_id": "chat-1"})
            unbounded = message_repository.find_messages({"chat_id": "chat-1"}, limit=0)
            self.assertEqual(len(default), 1000)
            self.assertEqual(default[-1].message_id, "audit-1004")
            self.assertEqual(len(unbounded), 1008)
        finally:
            fixture.tearDown()


class PeriodicTaskAuditTest(unittest.IsolatedAsyncioTestCase):
    async def test_periodic_task_recovers_on_next_interval(self):
        abort = asyncio.Event()

        class TransientFailureTask(AsyncTask):
            calls = 0

            async def run(self):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("transient failure")
                abort.set()

        task = TransientFailureTask(run_interval=1)
        with patch("src.manager.async_task_manager.asyncio.sleep"):
            await task.start_task(abort)
        self.assertEqual(task.calls, 2)

    async def test_one_shot_failure_still_propagates(self):
        task = AsyncTask()
        with patch.object(task, "run", side_effect=RuntimeError("one shot")):
            with self.assertRaises(RuntimeError):
                await task.start_task(asyncio.Event())

    async def test_cancellation_does_not_restart_periodic_task(self):
        task = AsyncTask(run_interval=1)
        with patch.object(task, "run", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await task.start_task(asyncio.Event())
