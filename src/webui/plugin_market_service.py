"""Fetch and validate the backend-configured RiyaBot plugin Registry."""

from __future__ import annotations

import asyncio

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import Any

from src.common.logger import get_logger, hash_id
from src.plugin_system.marketplace import (
    PluginRegistry,
    normalize_registry_url,
    parse_plugin_registry,
)
from src.webui.git_mirror_service import get_git_mirror_service


logger = get_logger("webui.plugin_market")
RawFetcher = Callable[..., Awaitable[dict[str, Any]]]
PLUGIN_REGISTRY_CATALOG_CACHE_SECONDS = 30.0


class PluginRegistryFetchError(RuntimeError):
    """The configured Registry could not be reached safely."""


class PluginRegistryFormatError(ValueError):
    """The Registry response failed the local contract."""


@dataclass(frozen=True)
class PluginRegistrySnapshot:
    registry_url: str
    registry: PluginRegistry
    fetched_at: datetime


_registry_catalog_cache: dict[str, tuple[float, PluginRegistrySnapshot]] = {}
_registry_catalog_inflight: dict[str, asyncio.Task[PluginRegistrySnapshot]] = {}


async def fetch_plugin_registry(
    registry_url: str,
    *,
    raw_fetcher: RawFetcher | None = None,
    cache_ttl_seconds: float = 0,
) -> PluginRegistrySnapshot:
    try:
        registry_url = normalize_registry_url(registry_url)
    except (TypeError, ValueError) as exc:
        raise PluginRegistryFetchError("插件 Registry 地址无效") from exc

    if cache_ttl_seconds > 0:
        now = monotonic()
        cached = _registry_catalog_cache.get(registry_url)
        if cached is not None and cached[0] > now:
            return cached[1]

        task = _registry_catalog_inflight.get(registry_url)
        if task is None:
            task = asyncio.create_task(fetch_plugin_registry(registry_url, raw_fetcher=raw_fetcher))
            _registry_catalog_inflight[registry_url] = task

            def complete_inflight(completed: asyncio.Task[PluginRegistrySnapshot]) -> None:
                if _registry_catalog_inflight.get(registry_url) is completed:
                    _registry_catalog_inflight.pop(registry_url, None)
                if completed.cancelled():
                    return
                try:
                    snapshot = completed.result()
                except Exception:
                    return
                _registry_catalog_cache[registry_url] = (
                    monotonic() + cache_ttl_seconds,
                    snapshot,
                )

            task.add_done_callback(complete_inflight)

        return await asyncio.shield(task)

    fetcher = raw_fetcher or get_git_mirror_service().fetch_raw_file
    try:
        result = await fetcher(
            owner="",
            repo="",
            branch="",
            file_path="",
            custom_url=registry_url,
        )
    except Exception as exc:
        logger.warning(
            "插件 Registry 请求失败",
            event_code="plugin.market.registry_fetch_failed",
            registry_hash=hash_id(registry_url),
            error_type=type(exc).__name__,
        )
        raise PluginRegistryFetchError("插件 Registry 暂时不可用") from exc

    if not isinstance(result, dict) or result.get("success") is not True:
        logger.warning(
            "插件 Registry 请求未成功",
            event_code="plugin.market.registry_fetch_failed",
            registry_hash=hash_id(registry_url),
        )
        raise PluginRegistryFetchError("插件 Registry 暂时不可用")

    raw = result.get("data")
    if not isinstance(raw, str):
        raise PluginRegistryFormatError("插件 Registry 响应格式无效")
    try:
        registry = parse_plugin_registry(raw)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "插件 Registry 校验失败",
            event_code="plugin.market.registry_invalid",
            registry_hash=hash_id(registry_url),
            error_type=type(exc).__name__,
        )
        raise PluginRegistryFormatError("插件 Registry 响应格式无效") from exc

    return PluginRegistrySnapshot(
        registry_url=registry_url,
        registry=registry,
        fetched_at=datetime.now(timezone.utc),
    )


__all__ = [
    "PLUGIN_REGISTRY_CATALOG_CACHE_SECONDS",
    "PluginRegistryFetchError",
    "PluginRegistryFormatError",
    "PluginRegistrySnapshot",
    "fetch_plugin_registry",
]
