import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.memory import store as store_module
from src.memory.store import MemoryStoreConfig, QdrantManager
import tests.test_vector_migration as migration_tests


class QdrantRemoteIoAuditTest(unittest.IsolatedAsyncioTestCase):
    def manager(self, client, *, remote=True):
        manager = QdrantManager(MemoryStoreConfig(qdrant_url="http://qdrant.test:6333" if remote else ""))
        manager._client = client
        manager._available = True
        return manager

    async def test_remote_search_leaves_event_loop_available(self):
        finished = threading.Event()
        client_threads = []
        heartbeat = []

        def search(**kwargs):
            client_threads.append(threading.get_ident())
            threading.Event().wait(0.1)
            finished.set()
            return []

        manager = self.manager(SimpleNamespace(search=search))

        async def tick():
            while not finished.is_set():
                heartbeat.append(True)
                await asyncio.sleep(0)

        await asyncio.gather(manager.search_similar_atoms([0.1]), tick())
        self.assertTrue(heartbeat, "network search blocked the event loop")
        self.assertNotIn(threading.get_ident(), client_threads)

    async def test_local_search_stays_on_owning_thread(self):
        threads = []
        manager = self.manager(
            SimpleNamespace(search=lambda **kwargs: threads.append(threading.get_ident()) or []), remote=False
        )
        await manager.search_similar_atoms([0.1])
        self.assertEqual(threads, [threading.get_ident()])

    async def test_cancelled_write_finishes_before_subsequent_delete(self):
        started = threading.Event()
        release = threading.Event()
        order = []

        def upsert(**kwargs):
            started.set()
            release.wait(0.5)
            order.append("upsert")

        manager = self.manager(SimpleNamespace(upsert=upsert, delete=lambda **kwargs: order.append("delete")))
        task = asyncio.create_task(manager.upsert_atom_vector("atom", [0.1], {}))
        self.addCleanup(release.set)
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        deletion = asyncio.create_task(manager.delete_atom_vector("atom"))
        await asyncio.sleep(0)
        self.assertFalse(task.done(), "cancellation released an unfinished remote mutation")
        self.assertEqual(order, [])
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(await deletion)
        self.assertEqual(order, ["upsert", "delete"])

    async def test_queued_cancelled_operation_never_reaches_client(self):
        started = threading.Event()
        release = threading.Event()
        order = []

        def search(**kwargs):
            started.set()
            release.wait(0.5)
            return []

        manager = self.manager(SimpleNamespace(search=search, delete=lambda **kwargs: order.append("delete")))
        searching = asyncio.create_task(manager.search_similar_atoms([0.1]))
        self.addCleanup(release.set)
        await asyncio.to_thread(started.wait, 1)
        deletion = asyncio.create_task(manager.delete_atom_vector("atom"))
        await asyncio.sleep(0)
        deletion.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await deletion
        await searching
        self.assertEqual(order, [])


class QdrantRemoteMigrationAuditTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_alias_activation_finishes_state_commit_on_loop(self):
        started = threading.Event()
        release = threading.Event()
        client = migration_tests.AliasQdrantClient({"old": 2, "new": 2}, {"memory_atoms__active": "old"})
        update_aliases = client.update_collection_aliases

        def blocking_alias(**kwargs):
            started.set()
            release.wait(0.5)
            return update_aliases(**kwargs)

        client.update_collection_aliases = blocking_alias
        manager = QdrantManager(MemoryStoreConfig(qdrant_url="http://qdrant.test:6333", embedding_dimension=2))
        manager._available = True
        manager._client = client
        manager._atom_migration_target = "new"
        manager._vector_search_enabled = False
        writes = []
        with patch.object(
            manager, "_save_vector_state", side_effect=lambda **kwargs: writes.append(threading.get_ident())
        ):
            task = asyncio.create_task(manager.activate_atom_migration())
            self.addCleanup(release.set)
            await asyncio.to_thread(started.wait, 1)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(client.aliases["memory_atoms__active"], "new")
        self.assertIsNone(manager.atom_migration_target)
        self.assertTrue(manager.vector_search_enabled)
        self.assertEqual(writes, [threading.get_ident()])

    async def test_remote_initialization_offloads_client_calls_but_not_sqlite(self):
        client = migration_tests.AliasQdrantClient({})
        manager = QdrantManager(MemoryStoreConfig(qdrant_url="http://qdrant.test:6333", embedding_dimension=2))
        manager._available = True
        network_threads = []
        get_collections = client.get_collections

        def track_collections():
            network_threads.append(threading.get_ident())
            return get_collections()

        client.get_collections = track_collections
        state_threads = []
        state = SimpleNamespace(
            active_signature=manager._embedding_signature, active_dimension=2, target_collection=None, status="ready"
        )
        with (
            patch.object(store_module, "_QdrantClient", return_value=client),
            patch.object(manager, "_load_vector_state", return_value=None),
            patch.object(
                manager,
                "_save_vector_state",
                side_effect=lambda **kwargs: state_threads.append(threading.get_ident()) or state,
            ),
        ):
            await manager.initialize()
        self.assertTrue(network_threads)
        self.assertNotIn(threading.get_ident(), network_threads)
        self.assertEqual(set(state_threads), {threading.get_ident()})
        self.assertTrue(manager.vector_search_enabled)


class QdrantRemoteProfileSwitchAuditTest(unittest.IsolatedAsyncioTestCase):
    setUp = migration_tests.VectorMigrationManagerTest.setUp

    async def test_remote_profile_switch_waits_for_validated_write(self):
        client = migration_tests.AliasQdrantClient({})
        manager = QdrantManager(
            MemoryStoreConfig(qdrant_url="http://qdrant.test:6333", embedding_dimension=2, embedding_signature="old")
        )
        with patch.object(store_module, "_QdrantClient", return_value=client):
            await manager.initialize()
        started = threading.Event()
        release = threading.Event()
        original_upsert = client.upsert

        def blocking_upsert(**kwargs):
            started.set()
            release.wait(0.5)
            return original_upsert(**kwargs)

        client.upsert = blocking_upsert
        write = asyncio.create_task(
            manager.upsert_atom_vector("atom", [0.1, 0.2], {"embedding_signature": "old", "embedding_dimension": 2})
        )
        self.addCleanup(release.set)
        await asyncio.to_thread(started.wait, 1)
        profile = SimpleNamespace(signature="new", model_name="new-model", dimension=3)
        switch = asyncio.create_task(manager.reconfigure_embedding(profile))
        await asyncio.sleep(0)
        self.assertEqual(manager.config.embedding_signature, "old")
        release.set()
        self.assertTrue(await write)
        self.assertTrue(await switch)
        self.assertEqual(client.upserts[0][1][0].payload["embedding_signature"], "old")
        self.assertEqual(manager.config.embedding_signature, "new")
        self.assertIsNotNone(manager.atom_migration_target)
        self.assertFalse(
            await manager.upsert_atom_vector(
                "stale", [0.1, 0.2], {"embedding_signature": "old", "embedding_dimension": 2}
            )
        )


class QdrantShutdownCancellationAuditTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_tasks_cancellation_finishes_alias_and_sqlite_commit(self):
        started = threading.Event()
        release = threading.Event()
        client = migration_tests.AliasQdrantClient({"old": 2, "new": 2}, {"memory_atoms__active": "old"})
        original_update = client.update_collection_aliases

        def update(**kwargs):
            started.set()
            release.wait(0.5)
            return original_update(**kwargs)

        client.update_collection_aliases = update
        manager = QdrantManager(MemoryStoreConfig(qdrant_url="http://qdrant.test:6333", embedding_dimension=2))
        manager._available = True
        manager._client = client
        manager._active_atoms_collection = "old"
        manager._atom_migration_target = "new"
        writes = []
        before = asyncio.all_tasks()
        with patch.object(manager, "_save_vector_state", side_effect=lambda **kwargs: writes.append(kwargs)):
            operation = asyncio.create_task(manager.activate_atom_migration())
            self.addCleanup(release.set)
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            pending = asyncio.all_tasks() - before
            for task in pending:
                task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(operation.done())
            self.assertTrue(manager._remote_operation_lock.locked())
            release.set()
            await asyncio.gather(*pending, return_exceptions=True)
        self.assertTrue(operation.cancelled())
        self.assertFalse(manager._remote_operation_lock.locked())
        self.assertEqual(client.aliases["memory_atoms__active"], "new")
        self.assertIsNone(manager.atom_migration_target)
        self.assertEqual(manager._active_atoms_collection, "new")
        self.assertEqual(len(writes), 1)
