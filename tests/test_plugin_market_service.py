import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from src.webui.plugin_market_service import (
    PluginRegistryFetchError,
    PluginRegistryFormatError,
    fetch_plugin_registry,
)

from tests.test_plugin_marketplace import registry_document


class PluginMarketServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_plugin_registry_uses_pinned_raw_fetcher_and_returns_typed_snapshot(self) -> None:
        registry_url = "https://plugins.riyabot.example/registry.json"
        raw_fetcher = AsyncMock(
            return_value={
                "success": True,
                "data": json.dumps(registry_document()),
                "mirror_used": "custom",
                "attempts": 1,
                "url": registry_url,
            }
        )

        snapshot = await fetch_plugin_registry(registry_url, raw_fetcher=raw_fetcher)

        self.assertEqual(snapshot.registry_url, registry_url)
        self.assertEqual(snapshot.registry.meta.name, "RiyaBot Official Plugin Market")
        self.assertIn("github.alice.weather", snapshot.registry.plugins)
        raw_fetcher.assert_awaited_once_with(
            owner="",
            repo="",
            branch="",
            file_path="",
            custom_url=registry_url,
        )

    async def test_fetch_plugin_registry_rejects_non_https_url_before_fetch(self) -> None:
        raw_fetcher = AsyncMock()

        with self.assertRaises(PluginRegistryFetchError):
            await fetch_plugin_registry("http://127.0.0.1/registry.json", raw_fetcher=raw_fetcher)

        raw_fetcher.assert_not_awaited()

    async def test_fetch_plugin_registry_maps_network_and_schema_failures_to_stable_errors(self) -> None:
        cases = [
            (
                "network",
                {"success": False, "error": "token=secret at /private/path"},
                PluginRegistryFetchError,
            ),
            (
                "schema",
                {"success": True, "data": '{"plugins": {}}'},
                PluginRegistryFormatError,
            ),
        ]

        for name, result, expected_error in cases:
            with self.subTest(case=name):
                raw_fetcher = AsyncMock(return_value=result)
                with self.assertRaises(expected_error) as failure:
                    await fetch_plugin_registry(
                        "https://plugins.riyabot.example/registry.json",
                        raw_fetcher=raw_fetcher,
                    )

                self.assertNotIn("secret", str(failure.exception))
                self.assertNotIn("private", str(failure.exception))

    async def test_catalog_cache_coalesces_concurrent_fetches_without_caching_lifecycle_reads(self) -> None:
        registry_url = "https://cache.plugins.riyabot.example/registry.json"
        raw_fetcher = AsyncMock(
            return_value={
                "success": True,
                "data": json.dumps(registry_document()),
                "mirror_used": "custom",
                "attempts": 1,
                "url": registry_url,
            }
        )

        snapshots = await asyncio.gather(
            *(
                fetch_plugin_registry(
                    registry_url,
                    raw_fetcher=raw_fetcher,
                    cache_ttl_seconds=30,
                )
                for _ in range(4)
            )
        )

        self.assertEqual(raw_fetcher.await_count, 1)
        self.assertTrue(all(snapshot is snapshots[0] for snapshot in snapshots))

        await fetch_plugin_registry(registry_url, raw_fetcher=raw_fetcher)
        self.assertEqual(raw_fetcher.await_count, 2)

    async def test_catalog_cache_survives_cancellation_of_the_only_waiter(self) -> None:
        registry_url = "https://cancelled-waiter.plugins.riyabot.example/registry.json"
        fetch_started = asyncio.Event()
        release_fetch = asyncio.Event()

        async def delayed_fetcher(**_kwargs: object) -> dict[str, object]:
            fetch_started.set()
            await release_fetch.wait()
            return {
                "success": True,
                "data": json.dumps(registry_document()),
                "mirror_used": "custom",
                "attempts": 1,
                "url": registry_url,
            }

        raw_fetcher = AsyncMock(side_effect=delayed_fetcher)
        waiting = asyncio.create_task(
            fetch_plugin_registry(
                registry_url,
                raw_fetcher=raw_fetcher,
                cache_ttl_seconds=30,
            )
        )
        await fetch_started.wait()

        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting

        release_fetch.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        snapshot = await fetch_plugin_registry(
            registry_url,
            raw_fetcher=raw_fetcher,
            cache_ttl_seconds=30,
        )

        self.assertEqual(snapshot.registry_url, registry_url)
        self.assertEqual(raw_fetcher.await_count, 1)

    async def test_catalog_cache_does_not_cache_failed_fetches(self) -> None:
        registry_url = "https://retry.plugins.riyabot.example/registry.json"
        raw_fetcher = AsyncMock(
            side_effect=[
                {"success": False, "error": "temporary failure"},
                {
                    "success": True,
                    "data": json.dumps(registry_document()),
                    "mirror_used": "custom",
                    "attempts": 1,
                    "url": registry_url,
                },
            ]
        )

        with self.assertRaises(PluginRegistryFetchError):
            await fetch_plugin_registry(
                registry_url,
                raw_fetcher=raw_fetcher,
                cache_ttl_seconds=30,
            )

        snapshot = await fetch_plugin_registry(
            registry_url,
            raw_fetcher=raw_fetcher,
            cache_ttl_seconds=30,
        )

        self.assertEqual(snapshot.registry_url, registry_url)
        self.assertEqual(raw_fetcher.await_count, 2)


if __name__ == "__main__":
    unittest.main()
