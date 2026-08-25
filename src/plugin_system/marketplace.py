"""Typed contracts for reviewed plugin manifests and Registry documents."""

from __future__ import annotations

import json
import ipaddress
import re
import unicodedata

from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PLUGIN_SDK_VERSION = "1.0.0"
MAX_PLUGIN_REGISTRY_BYTES = 5 * 1024 * 1024
MAX_PLUGIN_REGISTRY_ENTRIES = 10_000

_MARKET_PLUGIN_ID_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?){2,}$"
)
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)*$")
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_GIT_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BIDI_CONTROL_CODEPOINTS = frozenset(
    {
        0x061C,
        0x200E,
        0x200F,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
    }
)
_UNICODE_LINE_SEPARATOR_CODEPOINTS = frozenset({0x2028, 0x2029})


def validate_market_plugin_id(plugin_id: str) -> str:
    """Validate the stable, lowercase namespace used by reviewed market plugins."""
    if not isinstance(plugin_id, str) or len(plugin_id) > 128 or not _MARKET_PLUGIN_ID_RE.fullmatch(plugin_id):
        raise ValueError("市场插件 ID 必须是小写的至少三段命名空间")
    return plugin_id


def validate_semver(version: str) -> str:
    if not isinstance(version, str) or len(version) > 128 or not _SEMVER_RE.fullmatch(version):
        raise ValueError("版本必须符合 SemVer 2.0.0")
    return version


def _validate_display_text(value: str, *, multiline: bool = False) -> str:
    for character in value:
        codepoint = ord(character)
        is_allowed_newline = multiline and character == "\n"
        if (
            (unicodedata.category(character) in {"Cc", "Cs"} and not is_allowed_newline)
            or codepoint in _BIDI_CONTROL_CODEPOINTS
            or codepoint in _UNICODE_LINE_SEPARATOR_CODEPOINTS
        ):
            raise ValueError("展示文本包含不允许的控制字符")
    return value


def _semver_key(version: str) -> tuple[Any, ...]:
    match = _SEMVER_RE.fullmatch(version)
    if match is None:
        raise ValueError("版本必须符合 SemVer 2.0.0")
    prerelease = match.group(4)
    prerelease_key = (
        tuple((0, int(identifier)) if identifier.isdigit() else (1, identifier) for identifier in prerelease.split("."))
        if prerelease
        else ()
    )
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        1 if prerelease is None else 0,
        prerelease_key,
    )


def _normalize_https_url(value: str, *, repository: bool = False) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 2048:
        raise ValueError("URL 格式无效")
    if any(
        unicodedata.category(character) in {"Cc", "Cs"}
        or ord(character) in _BIDI_CONTROL_CODEPOINTS
        or ord(character) in _UNICODE_LINE_SEPARATOR_CODEPOINTS
        for character in value
    ):
        raise ValueError("URL 格式无效")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("URL 格式无效") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("URL 必须是无内嵌凭据的 HTTPS 地址")
    if repository and (parsed.query or parsed.fragment):
        raise ValueError("仓库 URL 不允许查询参数或片段")

    raw_hostname = parsed.hostname
    if "%" in raw_hostname:
        raise ValueError("URL 主机名格式无效")
    try:
        address = ipaddress.ip_address(raw_hostname)
    except ValueError:
        try:
            hostname = raw_hostname.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError as exc:
            raise ValueError("URL 主机名格式无效") from exc
        labels = hostname.split(".")
        if (
            not hostname
            or len(hostname) > 253
            or any(len(label) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) for label in labels)
        ):
            raise ValueError("URL 主机名格式无效") from None
        netloc = hostname
    else:
        hostname = address.compressed
        netloc = f"[{hostname}]" if isinstance(address, ipaddress.IPv6Address) else hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    path = parsed.path.rstrip("/")
    if repository and path.endswith(".git"):
        path = path[:-4]
    if repository and (not path or path == "/"):
        raise ValueError("仓库 URL 缺少仓库路径")
    return urlunsplit(("https", netloc, path, parsed.query, parsed.fragment))


def normalize_registry_url(value: str) -> str:
    """Normalize the backend-configured HTTPS Registry endpoint."""
    normalized = _normalize_https_url(value)
    parsed = urlsplit(normalized)
    if parsed.query or parsed.fragment:
        raise ValueError("Registry URL 不允许查询参数或片段")
    return normalized


def normalize_repository_url(value: str) -> str:
    """Normalize an HTTPS repository identity used by reviewed releases."""
    return _normalize_https_url(value, repository=True)


def _normalize_registry_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Registry 时间必须包含时区")
    return value.astimezone(timezone.utc)


def _validate_registry_datetime_text(value: Any) -> Any:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError("Registry 时间必须是 ISO 8601 文本")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Registry 时间必须是 ISO 8601 文本") from exc


def _validate_git_ref(value: str) -> str:
    forbidden = {"..", "@{", "\\", "~", "^", ":", "?", "*", "["}
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 255
        or value.startswith(("-", ".", "/"))
        or value.endswith((".", "/"))
        or "//" in value
        or any(character in value for character in forbidden)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(part.endswith(".lock") for part in value.split("/"))
    ):
        raise ValueError("Registry ref 不是安全的 Git Tag")
    return value


def validate_registry_ref(value: str) -> str:
    """Validate the immutable Git Tag coordinate accepted from a Registry."""
    return _validate_git_ref(value)


def validate_source_commit(value: str) -> str:
    if not isinstance(value, str) or not _GIT_COMMIT_RE.fullmatch(value):
        raise ValueError("commit 必须是完整的小写 Git 对象 ID")
    return value


def validate_artifact_sha256(value: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError("sha256 必须是完整的小写 SHA-256")
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManifestAuthor(_StrictModel):
    name: str = Field(min_length=1, max_length=128)
    url: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_display_text(value)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        return _normalize_https_url(value) if value is not None else None


class ManifestUrls(_StrictModel):
    repository: str
    homepage: str | None = None
    documentation: str | None = None
    issues: str | None = None

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        return normalize_repository_url(value)

    @field_validator("homepage", "documentation", "issues")
    @classmethod
    def validate_optional_url(cls, value: str | None) -> str | None:
        return _normalize_https_url(value) if value is not None else None


class CompatibilityRange(_StrictModel):
    min_version: str
    max_version: str | None = None

    @field_validator("min_version", "max_version")
    @classmethod
    def validate_version(cls, value: str | None) -> str | None:
        return validate_semver(value) if value is not None else None

    @model_validator(mode="after")
    def validate_order(self) -> "CompatibilityRange":
        if self.max_version is not None and _semver_key(self.min_version) > _semver_key(self.max_version):
            raise ValueError("最低兼容版本不能高于最高兼容版本")
        return self


class ManifestI18n(_StrictModel):
    default_locale: str = Field(min_length=2, max_length=32)
    supported_locales: list[str] = Field(min_length=1, max_length=32)

    @field_validator("default_locale")
    @classmethod
    def validate_default_locale(cls, value: str) -> str:
        if not _LOCALE_RE.fullmatch(value):
            raise ValueError("默认语言格式无效")
        return value

    @field_validator("supported_locales")
    @classmethod
    def validate_supported_locales(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(
            not 2 <= len(locale) <= 32 or not _LOCALE_RE.fullmatch(locale) for locale in value
        ):
            raise ValueError("支持语言必须唯一且格式有效")
        return value

    @model_validator(mode="after")
    def require_default_locale(self) -> "ManifestI18n":
        if self.default_locale not in self.supported_locales:
            raise ValueError("默认语言必须包含在支持语言中")
        return self


class ManifestComponent(_StrictModel):
    type: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=512)

    @field_validator("type", "name")
    @classmethod
    def validate_single_line_text(cls, value: str) -> str:
        return _validate_display_text(value)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return _validate_display_text(value, multiline=True)


class ManifestPluginInfo(_StrictModel):
    is_built_in: bool = False
    plugin_type: str = Field(default="general", min_length=1, max_length=64)
    components: list[ManifestComponent] = Field(default_factory=list, max_length=256)

    @field_validator("is_built_in", mode="before")
    @classmethod
    def validate_is_built_in(cls, value: Any) -> Any:
        if not isinstance(value, bool):
            raise ValueError("is_built_in 必须是布尔值")
        return value

    @field_validator("plugin_type")
    @classmethod
    def validate_plugin_type(cls, value: str) -> str:
        return _validate_display_text(value)


class ManifestV2(_StrictModel):
    manifest_version: Literal[2]
    id: str
    name: str = Field(min_length=1, max_length=256)
    version: str
    description: str = Field(min_length=1, max_length=4096)
    author: ManifestAuthor
    license: str = Field(min_length=1, max_length=128)
    urls: ManifestUrls
    host_application: CompatibilityRange
    sdk: CompatibilityRange
    entrypoint: Literal["plugin.py"]
    capabilities: list[str] = Field(default_factory=list, max_length=128)
    i18n: ManifestI18n
    keywords: list[str] = Field(default_factory=list, max_length=64)
    categories: list[str] = Field(default_factory=list, max_length=32)
    plugin_info: ManifestPluginInfo | None = None

    @field_validator("manifest_version", mode="before")
    @classmethod
    def validate_manifest_version_type(cls, value: Any) -> Any:
        if type(value) is not int or value != 2:
            raise ValueError("Manifest manifest_version 必须是数字 2")
        return value

    @field_validator("name", "license")
    @classmethod
    def validate_single_line_text(cls, value: str) -> str:
        return _validate_display_text(value)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return _validate_display_text(value, multiline=True)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_market_plugin_id(value)

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return validate_semver(value)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(
            not 1 <= len(item) <= 128 or not _CAPABILITY_RE.fullmatch(item) for item in value
        ):
            raise ValueError("capabilities 必须唯一且使用小写命名空间")
        return value

    @field_validator("keywords", "categories")
    @classmethod
    def validate_labels(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not item or len(item) > 64 for item in value):
            raise ValueError("清单标签格式无效")
        for item in value:
            _validate_display_text(item)
        return value


RegistryStatus = Literal["approved", "yanked", "blocked"]


class RegistryArtifactNotSupportedError(ValueError):
    """The Registry points at an artifact format not handled by this release."""


class RegistryMeta(_StrictModel):
    schema_version: Literal[1]
    name: str = Field(min_length=1, max_length=128)
    repository: str

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version_type(cls, value: Any) -> Any:
        if type(value) is not int or value != 1:
            raise ValueError("Registry schema_version 必须是数字 1")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_display_text(value)

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        return normalize_repository_url(value)


class RegistryVersion(_StrictModel):
    version: str
    ref: str
    commit: str
    artifact_url: str | None = None
    sha256: str | None = None
    status: RegistryStatus
    released_at: datetime
    manifest: ManifestV2

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return validate_semver(value)

    @field_validator("ref")
    @classmethod
    def validate_ref(cls, value: str) -> str:
        return validate_registry_ref(value)

    @field_validator("commit")
    @classmethod
    def validate_commit(cls, value: str) -> str:
        return validate_source_commit(value)

    @field_validator("artifact_url")
    @classmethod
    def validate_artifact_url(cls, value: str | None) -> str | None:
        return _normalize_https_url(value) if value is not None else None

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str | None) -> str | None:
        return validate_artifact_sha256(value) if value is not None else None

    @field_validator("released_at")
    @classmethod
    def validate_released_at(cls, value: datetime) -> datetime:
        return _normalize_registry_datetime(value)

    @field_validator("released_at", mode="before")
    @classmethod
    def validate_released_at_text(cls, value: Any) -> Any:
        return _validate_registry_datetime_text(value)

    @model_validator(mode="after")
    def validate_artifact_pair(self) -> "RegistryVersion":
        if (self.artifact_url is None) != (self.sha256 is None):
            raise ValueError("artifact_url 与 sha256 必须同时声明")
        if self.manifest.version != self.version:
            raise ValueError("Registry 版本与 Manifest 版本不一致")
        return self

    @property
    def is_stable(self) -> bool:
        return "-" not in self.version.split("+", 1)[0]


def registry_version_order_key(version: RegistryVersion) -> tuple[Any, ...]:
    """Order reviewed versions by SemVer precedence and deterministic Registry metadata."""
    return (
        _semver_key(version.version),
        version.released_at,
        version.version,
        version.ref,
    )


class RegistryPlugin(_StrictModel):
    repository: str
    status: RegistryStatus
    review_level: Literal["community", "official"]
    updated_at: datetime
    versions: list[RegistryVersion] = Field(min_length=1, max_length=256)

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        return normalize_repository_url(value)

    @field_validator("updated_at")
    @classmethod
    def validate_updated_at(cls, value: datetime) -> datetime:
        return _normalize_registry_datetime(value)

    @field_validator("updated_at", mode="before")
    @classmethod
    def validate_updated_at_text(cls, value: Any) -> Any:
        return _validate_registry_datetime_text(value)

    @model_validator(mode="after")
    def validate_versions(self) -> "RegistryPlugin":
        versions = [version.version for version in self.versions]
        if len(set(versions)) != len(versions):
            raise ValueError("同一插件不能重复声明版本")
        refs = [version.ref for version in self.versions]
        if len(set(refs)) != len(refs):
            raise ValueError("同一插件的审核版本不能复用 Git Tag")
        commits = [version.commit for version in self.versions]
        if len(set(commits)) != len(commits):
            raise ValueError("同一插件的审核版本不能复用 Git commit")
        for version in self.versions:
            if version.manifest.urls.repository != self.repository:
                raise ValueError("Registry 仓库与 Manifest 仓库不一致")
        if max(version.released_at for version in self.versions) > self.updated_at:
            raise ValueError("Registry 更新时间不能早于最新版本发布时间")
        return self


class PluginRegistry(_StrictModel):
    meta: RegistryMeta = Field(alias="$meta")
    plugins: dict[str, RegistryPlugin]

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def normalize_top_level_plugins(cls, value: Any) -> Any:
        """Accept the documented flat Registry shape while using one internal projection."""
        if not isinstance(value, dict) or "plugins" in value or "$meta" not in value:
            return value
        return {
            "$meta": value["$meta"],
            "plugins": {key: item for key, item in value.items() if key != "$meta"},
        }

    @model_validator(mode="after")
    def validate_plugins(self) -> "PluginRegistry":
        if len(self.plugins) > MAX_PLUGIN_REGISTRY_ENTRIES:
            raise ValueError("Registry 插件数量超过限制")
        for plugin_id, plugin in self.plugins.items():
            validate_market_plugin_id(plugin_id)
            if any(version.manifest.id != plugin_id for version in plugin.versions):
                raise ValueError("Registry ID 与 Manifest ID 不一致")
        return self

    def latest_installable_version(self, plugin_id: str) -> RegistryVersion:
        plugin_id = validate_market_plugin_id(plugin_id)
        try:
            plugin = self.plugins[plugin_id]
        except KeyError as exc:
            raise KeyError("Registry 中不存在该插件") from exc
        if plugin.status != "approved":
            raise ValueError("插件当前不可安装")
        candidates = [
            version
            for version in plugin.versions
            if version.status == "approved" and version.is_stable and version.artifact_url is None
        ]
        if not candidates:
            raise ValueError("插件没有可安装的稳定版本")
        return max(candidates, key=registry_version_order_key)

    def installable_version(self, plugin_id: str, version: str) -> RegistryVersion:
        plugin_id = validate_market_plugin_id(plugin_id)
        version = validate_semver(version)
        try:
            plugin = self.plugins[plugin_id]
        except KeyError as exc:
            raise KeyError("Registry 中不存在该插件") from exc
        if plugin.status != "approved":
            raise ValueError("插件当前不可安装")
        for candidate in plugin.versions:
            if candidate.version == version:
                if candidate.status != "approved":
                    raise ValueError("该插件版本当前不可安装")
                if candidate.artifact_url is not None:
                    raise RegistryArtifactNotSupportedError("Registry 制品下载尚未支持")
                return candidate
        raise KeyError("Registry 中不存在该插件版本")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 包含重复字段: {key}")
        result[key] = value
    return result


def parse_json_document(raw: str) -> Any:
    """Parse UTF-8 JSON while rejecting ambiguous duplicate object keys."""
    if not isinstance(raw, str):
        raise ValueError("JSON 必须是文本")
    try:
        raw.encode("utf-8")
        return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except UnicodeEncodeError as exc:
        raise ValueError("JSON 必须使用 UTF-8") from exc
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("JSON 格式无效") from exc


def parse_manifest_v2(raw: str) -> ManifestV2:
    """Parse an author-owned Manifest v2 without accepting ambiguous JSON."""
    document = parse_json_document(raw)
    if not isinstance(document, dict):
        raise ValueError("Manifest JSON 必须是对象")
    return ManifestV2.model_validate(document)


def parse_plugin_registry(raw: str) -> PluginRegistry:
    if not isinstance(raw, str):
        raise ValueError("Registry 响应必须是 JSON 文本")
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Registry 响应必须使用 UTF-8") from exc
    if len(encoded) > MAX_PLUGIN_REGISTRY_BYTES:
        raise ValueError("Registry 响应超过大小限制")
    document = parse_json_document(raw)
    return PluginRegistry.model_validate(document)


__all__ = [
    "MAX_PLUGIN_REGISTRY_BYTES",
    "ManifestV2",
    "PLUGIN_SDK_VERSION",
    "PluginRegistry",
    "RegistryArtifactNotSupportedError",
    "RegistryPlugin",
    "RegistryVersion",
    "parse_manifest_v2",
    "parse_plugin_registry",
    "registry_version_order_key",
    "normalize_repository_url",
    "normalize_registry_url",
    "parse_json_document",
    "validate_artifact_sha256",
    "validate_market_plugin_id",
    "validate_registry_ref",
    "validate_semver",
    "validate_source_commit",
]
