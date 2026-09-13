import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.memory.dream_agent import DreamTask
from src.memory.insight_engine import InsightEngine
from src.memory.schema import InsightPool, MemoryAtom, configure_memory_database, initialize_database, memory_db
import tests.test_dream_task_behaviors as dream_tests


class MemoryScanConcurrencyAuditTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.previous_path = memory_db.database
        self.tmp = tempfile.TemporaryDirectory()
        configure_memory_database(str(Path(self.tmp.name) / "memory.db"))
        initialize_database()

    def tearDown(self):
        memory_db.close()
        configure_memory_database(str(self.previous_path))
        self.tmp.cleanup()

    async def test_cancelling_insight_scan_does_not_write_after_returning(self):
        engine = InsightEngine(object())
        started = threading.Event()
        release = threading.Event()
        original_create = InsightPool.create
        late_write = threading.Event()
        worker_finished = threading.Event()
        original_to_thread = asyncio.to_thread

        async def track_worker(function, *args, **kwargs):
            def run():
                try:
                    return function(*args, **kwargs)
                finally:
                    worker_finished.set()

            return await original_to_thread(run)

        def scan():
            started.set()
            release.wait(0.5)
            return [{"content": "must not persist", "source_atoms": None}]

        def create(**kwargs):
            late_write.set()
            return original_create(**kwargs)

        with (
            patch.object(engine, "_scan_atomic_patterns", side_effect=scan),
            patch.object(engine, "_scan_profile_evolution", return_value=[]),
            patch.object(engine, "_scan_association_network", return_value=[]),
            patch.object(engine, "_scan_dream_synthesis", return_value=[]),
            patch.object(InsightPool, "create", side_effect=create),
            patch("src.memory.insight_engine.asyncio.to_thread", side_effect=track_worker),
        ):
            task = asyncio.create_task(engine.generate_monthly_insights())
            self.addCleanup(release.set)
            await original_to_thread(started.wait, 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.set()
            self.assertTrue(await original_to_thread(worker_finished.wait, 1))
        self.assertFalse(late_write.is_set(), "cancelled monthly scan continued writing insights")
        self.assertEqual(InsightPool.select().count(), 0)

    async def test_concurrent_scans_commit_on_owner_thread_and_deduplicate(self):
        engine = InsightEngine(object())
        original_create = InsightPool.create
        write_threads = []
        insight = {"content": "same insight", "source_atoms": json.dumps(["atom"])}

        def create(**kwargs):
            write_threads.append(threading.get_ident())
            return original_create(**kwargs)

        with (
            patch.object(engine, "_scan_atomic_patterns", return_value=[insight]),
            patch.object(engine, "_scan_profile_evolution", return_value=[]),
            patch.object(engine, "_scan_association_network", return_value=[]),
            patch.object(engine, "_scan_dream_synthesis", return_value=[]),
            patch.object(InsightPool, "create", side_effect=create),
        ):
            results = await asyncio.gather(engine.generate_monthly_insights(), engine.generate_monthly_insights())
        self.assertEqual(write_threads, [threading.get_ident()])
        self.assertEqual(sum(map(len, results)), 1)
        self.assertEqual(InsightPool.select().count(), 1)

    async def test_soft_cap_revalidates_candidates_after_threaded_scan(self):
        task = DreamTask(dream_tests.FakeStore())
        for atom_id, changes in (
            ("planned", {"atom_type": "planned"}),
            ("other-person", {"entities": '["other"]'}),
            ("summary", {"source_scene": "dream", "source_id": "dream_soft_cap:existing"}),
        ):
            MemoryAtom.create(
                atom_id=atom_id, atom_type="episodic", content="keep", entities='["person"]', status="active"
            )
            MemoryAtom.update(**changes).where(MemoryAtom.atom_id == atom_id).execute()
        snapshot = {"person": ["planned", "other-person", "summary"]}
        with (
            patch.object(task, "_scan_soft_cap_candidates", return_value=({"person": 5}, snapshot)),
            patch.object(task, "_write_soft_cap_summary", new=AsyncMock()) as write,
            patch.object(task, "_archive_soft_cap_atoms", new=AsyncMock()) as archive,
        ):
            result = await task._merge_overflowing_user_memories(soft_cap=1, batch_size=3)
        write.assert_not_awaited()
        archive.assert_not_awaited()
        self.assertEqual(result["atoms_archived"], 0)

    def test_soft_cap_scan_bounds_candidates_and_does_not_load_content(self):
        task = DreamTask(dream_tests.FakeStore())
        for index in range(30):
            MemoryAtom.create(
                atom_id=str(index),
                atom_type="episodic",
                content="x" * 100_000,
                entities='["person"]',
                status="active",
                weight=index / 100,
            )
        counts, candidates = task._scan_soft_cap_candidates(3)
        self.assertEqual(counts, {"person": 30})
        self.assertEqual(candidates, {"person": ["0", "1", "2"]})
