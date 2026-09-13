import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.memory.user_profile import ProfileStore, UserProfile
from src.webui import person_routes
import tests.test_memory_profiles as profile_tests


class ProfileStreamingAuditTest(profile_tests.MemoryProfileDatabaseFixtureMixin, unittest.TestCase):
    def test_iteration_loads_profiles_without_per_profile_queries(self):
        store = ProfileStore()
        store.save_profile(UserProfile(user_id="one", nickname="One"))
        store.save_profile(UserProfile(user_id="two", nickname="Two"))
        with patch.object(store, "_resolve_profile_row", side_effect=AssertionError("N+1 lookup")):
            profiles = list(store.iter_profiles())
        self.assertEqual({profile.user_id for profile in profiles}, {"one", "two"})


class ProfileRouteStreamingAuditTest(unittest.IsolatedAsyncioTestCase):
    async def test_paginated_profile_scan_runs_outside_event_loop(self):
        event_thread = threading.get_ident()
        scan_threads = []

        def stream():
            scan_threads.append(threading.get_ident())
            for i in range(100):
                yield UserProfile(user_id=str(i), platform="qq", interests=["game"])

        fake_store = SimpleNamespace(iter_profiles=stream, list_profiles=Mock(side_effect=AssertionError("eager IDs")))
        with (
            patch.object(person_routes, "ProfileStore", return_value=fake_store),
            patch.object(person_routes, "verify_auth_token", return_value=True),
        ):
            response = await person_routes.get_person_list(
                page=3,
                page_size=7,
                search="game",
                is_known=True,
                platform="qq",
                maibot_session=None,
                authorization=None,
            )
        self.assertEqual(response.total, 100)
        self.assertEqual([person.user_id for person in response.data], [str(i) for i in range(14, 21)])
        self.assertTrue(scan_threads)
        self.assertNotIn(event_thread, scan_threads)
