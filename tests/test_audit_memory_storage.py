"""Audit regressions for bounded SQLite reads and WAL trim amplification."""

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.memory import store as store_module
from src.memory.store import MemoryStore
from src.memory.write_ops import OpStatus, WriteOpLogger
from tests.test_memory_store import MemoryDatabaseFixtureMixin, create_atom
from tests.test_memory_write_ops_and_reconciliation import make_op


class AuditWalTrimTest(unittest.TestCase):
    def test_full_nonterminal_wal_is_not_rewritten_by_noop_trim(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = WriteOpLogger(str(Path(directory) / "memory.db"), max_entries=2)
            operations = [make_op(str(index), status=OpStatus.FAILED) for index in range(3)]
            logger._write_all_ops(operations)
            with patch.object(logger, "_write_all_ops", wraps=logger._write_all_ops) as write:
                self.assertEqual(logger._auto_trim(), 0)
            write.assert_not_called()
            self.assertEqual(len(logger.get_recoverable_ops()), 3)


class AuditSqliteReadTest(MemoryDatabaseFixtureMixin, unittest.IsolatedAsyncioTestCase):
    async def test_batch_read_does_not_block_event_loop(self):
        store = object.__new__(MemoryStore)
        progressed = threading.Event()
        observed_progress = []
        query = Mock()
        query.where.return_value = []

        def slow_select(*args):
            observed_progress.append(progressed.wait(timeout=0.25))
            return query

        async def heartbeat():
            await asyncio.sleep(0)
            progressed.set()

        with patch.object(store_module.MemoryAtom, "select", side_effect=slow_select):
            await asyncio.gather(store.get_atoms_batch(["missing"]), heartbeat())
        self.assertEqual(observed_progress, [True])

    async def test_batch_read_inside_transaction_sees_uncommitted_atom_and_preserves_rollback(self):
        store = object.__new__(MemoryStore)
        try:
            with store_module.memory_db.atomic():
                create_atom("uncommitted", content="temporary")
                with patch.object(store_module.asyncio, "to_thread", side_effect=AssertionError("transaction moved")):
                    self.assertIn("uncommitted", await store.get_atoms_batch(["uncommitted"]))
                raise ValueError("rollback")
        except ValueError:
            pass
        self.assertNotIn("uncommitted", await store.get_atoms_batch(["uncommitted"]))

    async def test_canceled_bulk_read_does_not_mutate_source_or_cache(self):
        store = object.__new__(MemoryStore)
        atom = create_atom("existing", content="original")
        started = threading.Event()
        finish = threading.Event()
        finished = threading.Event()
        query = Mock()
        query.where.return_value = [atom]

        def slow_select(*args):
            started.set()
            try:
                finish.wait(timeout=2)
                return query
            finally:
                finished.set()

        with patch.object(store_module.MemoryAtom, "select", side_effect=slow_select):
            task = asyncio.create_task(store.get_atoms_batch(["existing"]))
            for _ in range(200):
                if started.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(started.is_set())
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(await asyncio.to_thread(finished.wait, 2))
        self.assertEqual((await store.get_atom("existing"))["content"], "original")

    async def test_list_atoms_reads_same_database_and_returns_plain_snapshots(self):
        store = object.__new__(MemoryStore)
        create_atom("visible", content="visible", status="active")
        create_atom("archived", content="hidden", status="archived")
        result = await store.list_atoms(status="active")
        self.assertEqual([row["atom_id"] for row in result], ["visible"])
        self.assertIsInstance(result[0], dict)

    async def test_shutdown_cancelling_all_tasks_waits_for_sqlite_worker(self):
        store = object.__new__(MemoryStore)
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        query = Mock()
        query.where.return_value = []

        def blocked_read(*args):
            started.set()
            try:
                release.wait(2)
                return query
            finally:
                finished.set()

        with patch.object(store_module.MemoryAtom, "select", side_effect=blocked_read):
            task = asyncio.create_task(store.get_atoms_batch(["atom"]))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                remaining = [item for item in asyncio.all_tasks() if item is not asyncio.current_task()]
                for item in remaining:
                    item.cancel()
                for _ in range(5):
                    await asyncio.sleep(0)
                self.assertFalse(task.done(), "shutdown released a still-running SQLite worker")
                self.assertFalse(finished.is_set())
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.to_thread(finished.wait, 1)
