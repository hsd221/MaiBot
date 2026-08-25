"""Persistence boundary for plugin installation provenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from src.common.database.database_model import PluginInstallation
from src.common.logger import get_logger, hash_id
from src.plugin_system.marketplace import (
    normalize_registry_url,
    normalize_repository_url,
    validate_artifact_sha256,
    validate_market_plugin_id,
    validate_registry_ref,
    validate_semver,
    validate_source_commit,
)


InstallMethod = Literal["market", "git", "upload", "local"]
logger = get_logger("plugin_installation_store")


@dataclass(frozen=True)
class InstallationSource:
    plugin_id: str
    install_method: InstallMethod
    registry_url: str | None
    repository_url: str | None
    source_ref: str | None
    source_commit: str | None
    artifact_sha256: str | None
    installed_version: str
    installed_at: datetime
    updated_at: datetime
    last_checked_at: datetime | None


class PluginInstallationStore:
    """Keep mutable installation state separate from author-owned manifests."""

    _INSTALL_METHODS = {"market", "git", "upload", "local"}

    @staticmethod
    def _as_datetime(value: object) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        raise ValueError("插件安装时间无效")

    @staticmethod
    def _validate_datetime(value: object, *, optional: bool = False) -> None:
        if value is None and optional:
            return
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("插件安装时间无效")

    @staticmethod
    def _validate(source: InstallationSource) -> None:
        if source.install_method not in PluginInstallationStore._INSTALL_METHODS:
            raise ValueError("插件安装方式无效")
        if not source.plugin_id or len(source.plugin_id) > 128:
            raise ValueError("插件 ID 无效")
        if not source.installed_version or len(source.installed_version) > 128:
            raise ValueError("插件版本无效")
        PluginInstallationStore._validate_datetime(source.installed_at)
        PluginInstallationStore._validate_datetime(source.updated_at)
        PluginInstallationStore._validate_datetime(source.last_checked_at, optional=True)

        if source.install_method == "market":
            required = (
                source.registry_url,
                source.repository_url,
                source.source_ref,
                source.source_commit,
            )
            if any(value is None for value in required):
                raise ValueError("市场安装来源不完整")
            try:
                validate_market_plugin_id(source.plugin_id)
                if normalize_registry_url(source.registry_url) != source.registry_url:
                    raise ValueError("Registry URL 未规范化")
                if normalize_repository_url(source.repository_url) != source.repository_url:
                    raise ValueError("仓库 URL 未规范化")
                validate_registry_ref(source.source_ref)
                validate_source_commit(source.source_commit)
                validate_semver(source.installed_version)
                if source.artifact_sha256 is not None:
                    validate_artifact_sha256(source.artifact_sha256)
            except (TypeError, ValueError) as exc:
                raise ValueError("市场安装来源无效") from exc
        elif source.registry_url is not None:
            raise ValueError("非市场安装不能绑定 Registry")

    @staticmethod
    def _to_source(record: PluginInstallation) -> InstallationSource:
        installed_at = PluginInstallationStore._as_datetime(record.installed_at)
        updated_at = PluginInstallationStore._as_datetime(record.updated_at)
        if installed_at is None or updated_at is None:
            raise ValueError("插件安装时间无效")
        source = InstallationSource(
            plugin_id=record.plugin_id,
            install_method=record.install_method,
            registry_url=record.registry_url,
            repository_url=record.repository_url,
            source_ref=record.source_ref,
            source_commit=record.source_commit,
            artifact_sha256=record.artifact_sha256,
            installed_version=record.installed_version,
            installed_at=installed_at,
            updated_at=updated_at,
            last_checked_at=PluginInstallationStore._as_datetime(record.last_checked_at),
        )
        PluginInstallationStore._validate(source)
        return source

    @staticmethod
    def _read_source(record: PluginInstallation) -> InstallationSource | None:
        try:
            return PluginInstallationStore._to_source(record)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "忽略无效的插件安装来源记录",
                event_code="plugin.installation.invalid_record",
                plugin_id_hash=hash_id(record.plugin_id),
                error_type=type(exc).__name__,
            )
            return None

    def get(self, plugin_id: str) -> InstallationSource | None:
        record = PluginInstallation.get_or_none(PluginInstallation.plugin_id == plugin_id)
        return self._read_source(record) if record is not None else None

    def list_all(self) -> dict[str, InstallationSource]:
        sources: dict[str, InstallationSource] = {}
        for record in PluginInstallation.select():
            source = self._read_source(record)
            if source is not None:
                sources[source.plugin_id] = source
        return sources

    def save(self, source: InstallationSource) -> InstallationSource:
        self._validate(source)
        database = PluginInstallation._meta.database
        with database.atomic():
            record = PluginInstallation.get_or_none(PluginInstallation.plugin_id == source.plugin_id)
            installed_at = self._as_datetime(record.installed_at) if record is not None else source.installed_at
            self._validate_datetime(installed_at)
            values = {
                "install_method": source.install_method,
                "registry_url": source.registry_url,
                "repository_url": source.repository_url,
                "source_ref": source.source_ref,
                "source_commit": source.source_commit,
                "artifact_sha256": source.artifact_sha256,
                "installed_version": source.installed_version,
                "installed_at": installed_at,
                "updated_at": source.updated_at,
                "last_checked_at": source.last_checked_at,
            }
            if record is None:
                record = PluginInstallation.create(plugin_id=source.plugin_id, **values)
            else:
                for field_name, value in values.items():
                    setattr(record, field_name, value)
                record.save()
        return self._to_source(record)

    def delete(self, plugin_id: str) -> bool:
        return bool(PluginInstallation.delete().where(PluginInstallation.plugin_id == plugin_id).execute())


__all__ = ["InstallationSource", "InstallMethod", "PluginInstallationStore"]
