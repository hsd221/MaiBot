import datetime
import json
import os
import shutil
import stat
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, get_origin

from fastapi import APIRouter, Cookie, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from src.common.logger import get_logger, hash_id
from src.common.toml_utils import save_toml_with_format
from src.config.config import MMC_VERSION, global_config
from src.plugin_system.base.config_types import ConfigField
from src.plugin_system.core.installation_store import InstallationSource, PluginInstallationStore
from src.plugin_system.marketplace import (
    ManifestV2,
    PluginRegistry,
    RegistryPlugin,
    RegistryVersion,
    RegistryArtifactNotSupportedError,
    parse_json_document,
    parse_manifest_v2,
    registry_version_order_key,
    validate_market_plugin_id,
    validate_semver,
)
from src.plugin_system.utils.manifest_utils import ManifestValidator
from src.webui.error_utils import internal_server_error, log_exception_type
from src.webui.path_utils import resolve_path_within
from src.webui.plugin_market_service import (
    PLUGIN_REGISTRY_CATALOG_CACHE_SECONDS,
    PluginRegistryFetchError,
    PluginRegistryFormatError,
    fetch_plugin_registry,
)
from src.webui.plugin_runtime_state import (
    DEFAULT_PLUGIN_RUNTIME_CONFIG_FILES,
    collect_plugin_runtime_entries,
    move_plugin_runtime_entries,
    restore_plugin_runtime_entries,
)
from .git_mirror_service import (
    MAX_RAW_FILE_BYTES,
    get_git_mirror_service,
    parse_repository_url,
    set_update_progress_callback,
    validate_clone_url,
    validate_raw_url,
)
from .token_manager import get_token_manager
from .plugin_progress_ws import update_progress

logger = get_logger("webui.plugin_routes")
MAX_PLUGIN_ID_CHARS = 128
MAX_PLUGIN_MANIFEST_BYTES = 256 * 1024
MAX_PLUGIN_CONFIG_BYTES = 1024 * 1024
MAX_PLUGIN_README_BYTES = 2 * 1024 * 1024
MAX_PLUGIN_CONFIG_BACKUPS = 5
MAX_PLUGIN_NAME_CHARS = 256
MAX_PLUGIN_VERSION_CHARS = 128
MAX_PLUGIN_AUTHOR_CHARS = 256
MAX_PLUGIN_INDEX_ENTRIES = 10_000
MAX_MARKET_PLUGIN_ENTRIES = 2_000
MAX_MARKET_PLUGIN_BYTES = 16 * 1024 * 1024

# 创建路由器
router = APIRouter(prefix="/plugins", tags=["插件管理"])

# 设置进度更新回调
set_update_progress_callback(update_progress)


def get_token_from_cookie_or_header(
    maibot_session: Optional[str] = None,
    authorization: Optional[str] = None,
) -> Optional[str]:
    """从 Cookie 或 Header 获取 token"""
    # 优先从 Cookie 获取
    if maibot_session:
        return maibot_session
    # 其次从 Header 获取
    if authorization and authorization.startswith("Bearer "):
        return authorization.replace("Bearer ", "")
    return None


def validate_safe_path(user_path: str, base_path: Path) -> Path:
    """
    验证用户提供的路径是否安全，防止路径遍历攻击

    Args:
        user_path: 用户输入的路径（相对路径）
        base_path: 允许的基础目录

    Returns:
        安全的绝对路径

    Raises:
        HTTPException: 如果检测到路径遍历攻击
    """
    # 规范化基础路径
    base_resolved = base_path.resolve()

    # 检查用户路径是否包含可疑字符
    # 禁止: .., 绝对路径开头, 空字节等
    if any(pattern in user_path for pattern in ["..", "\x00"]):
        logger.warning("检测到包含非法字符的插件路径")
        raise HTTPException(status_code=400, detail="路径包含非法字符")

    # 检查是否为绝对路径（Windows 和 Unix）
    if user_path.startswith("/") or user_path.startswith("\\") or (len(user_path) > 1 and user_path[1] == ":"):
        logger.warning("检测到绝对插件路径")
        raise HTTPException(status_code=400, detail="不允许使用绝对路径")

    # 构建目标路径并解析
    target_path = (base_path / user_path).resolve()

    # 验证解析后的路径仍在基础目录内
    try:
        target_path.relative_to(base_resolved)
    except ValueError as e:
        logger.warning("检测到插件路径越界")
        raise HTTPException(status_code=400, detail="路径超出允许范围") from e

    return target_path


def validate_plugin_id(plugin_id: str) -> str:
    """
    验证插件 ID 格式是否安全

    Args:
        plugin_id: 插件 ID (支持 author.name 格式，允许中文)

    Returns:
        验证通过的插件 ID

    Raises:
        HTTPException: 如果插件 ID 格式不安全
    """
    # 禁止空字符串
    if (
        not isinstance(plugin_id, str)
        or not plugin_id
        or plugin_id != plugin_id.strip()
        or len(plugin_id) > MAX_PLUGIN_ID_CHARS
    ):
        logger.warning("非法插件 ID: 类型、长度或首尾空白无效")
        raise HTTPException(status_code=400, detail="插件 ID 格式无效")

    # 禁止危险字符: 路径分隔符、空字节、控制字符等
    dangerous_patterns = ["/", "\\", "\x00", ".."]
    for pattern in dangerous_patterns:
        if pattern in plugin_id:
            logger.warning("非法插件 ID 格式: 包含危险字符")
            raise HTTPException(status_code=400, detail="插件 ID 包含非法字符")
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in plugin_id):
        logger.warning("非法插件 ID 格式: 包含控制字符")
        raise HTTPException(status_code=400, detail="插件 ID 包含非法字符")

    # 禁止以点开头或结尾（防止隐藏文件和路径问题）
    if plugin_id.startswith(".") or plugin_id.endswith("."):
        logger.warning("非法插件 ID: 不能以点开头或结尾")
        raise HTTPException(status_code=400, detail="插件 ID 不能以点开头或结尾")

    # 禁止特殊名称
    if plugin_id in (".", ".."):
        logger.warning("非法插件 ID: 特殊目录名")
        raise HTTPException(status_code=400, detail="插件 ID 不能为特殊目录名")

    return plugin_id


def _plugins_directory(*, create: bool = False) -> Optional[Path]:
    plugins_dir = Path("plugins")
    if plugins_dir.is_symlink():
        raise HTTPException(status_code=400, detail="插件目录路径无效")
    if not plugins_dir.exists():
        if not create:
            return None
        plugins_dir.mkdir(exist_ok=True)
    if not plugins_dir.is_dir():
        raise HTTPException(status_code=400, detail="插件目录路径无效")
    try:
        return plugins_dir.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail="插件目录路径无效") from exc


def _require_regular_file(path: Path, label: str) -> None:
    try:
        if path.is_symlink() or not path.is_file():
            raise HTTPException(status_code=400, detail=f"{label}路径不是普通文件")
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"{label}路径无效") from exc


def _read_limited_bytes(path: Path, max_bytes: int, label: str) -> bytes:
    _require_regular_file(path, label)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    file_descriptor = os.open(path, flags)
    try:
        file_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise HTTPException(status_code=400, detail=f"{label}路径不是普通文件")
        with os.fdopen(file_descriptor, "rb") as file:
            file_descriptor = -1
            content = file.read(max_bytes + 1)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"{label}过大")
    return content


def _read_limited_utf8(path: Path, max_bytes: int, label: str) -> str:
    content = _read_limited_bytes(path, max_bytes, label)
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"{label}必须使用 UTF-8 编码") from exc


def _load_plugin_manifest(plugin_path: Path) -> Optional[Dict[str, Any]]:
    manifest_path = _plugin_file_path(plugin_path, "_manifest.json")
    if not manifest_path.exists():
        return None
    manifest_content = _read_limited_utf8(manifest_path, MAX_PLUGIN_MANIFEST_BYTES, "插件清单")
    try:
        manifest = parse_json_document(manifest_content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="插件清单格式无效") from exc
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=400, detail="插件清单格式无效")
    return manifest


def _find_plugin_path(plugin_id: str) -> Optional[Path]:
    """按 ID 查找插件，并拒绝逃逸到 plugins 目录外的符号链接。"""
    plugin_id = validate_plugin_id(plugin_id)
    plugins_dir = _plugins_directory()
    if plugins_dir is None:
        return None

    for candidate in sorted(plugins_dir.iterdir(), key=lambda path: path.name):
        try:
            if candidate.is_symlink():
                continue
            plugin_path = candidate.resolve(strict=True)
            if not plugin_path.is_relative_to(plugins_dir) or not plugin_path.is_dir():
                continue
            manifest = _load_plugin_manifest(plugin_path)
            if manifest is None:
                continue
            declared_id = manifest.get("id")
            if declared_id == plugin_id or (declared_id is None and candidate.name == plugin_id):
                return plugin_path
        except HTTPException:
            if candidate.name == plugin_id:
                raise
        except (json.JSONDecodeError, OSError, RuntimeError, ValueError):
            continue
    return None


def _plugin_file_path(plugin_path: Path, filename: str) -> Path:
    if Path(filename).name != filename or Path(filename).is_absolute():
        raise HTTPException(status_code=400, detail="插件文件路径无效")
    candidate = plugin_path / filename
    if candidate.is_symlink():
        raise HTTPException(status_code=400, detail="插件文件路径不能是符号链接")
    try:
        return resolve_path_within(plugin_path, filename)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="插件文件路径无效") from exc


def _atomic_write_bytes(path: Path, content: bytes, max_bytes: int, label: str) -> None:
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"{label}过大")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise HTTPException(status_code=400, detail=f"{label}路径不是普通文件")

    file_descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "wb") as file:
            file_descriptor = -1
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        temp_path.unlink(missing_ok=True)


def _safe_manifest_text(value: Any, label: str, max_chars: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_chars
        or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value)
    ):
        raise HTTPException(status_code=400, detail=f"插件清单中的{label}格式无效")
    return value


def _count_raw_plugin_entries(data: Any) -> int:
    """验证 Raw 响应体资源上限，并在 JSON 数组时返回条目数。"""
    if not isinstance(data, str):
        raise HTTPException(status_code=502, detail="Raw 文件服务响应无效")
    try:
        encoded_data = data.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise HTTPException(status_code=502, detail="Raw 文件服务响应无效") from exc
    if len(encoded_data) > MAX_RAW_FILE_BYTES:
        raise HTTPException(status_code=413, detail="Raw 文件过大")

    try:
        parsed_data = json.loads(data)
    except (json.JSONDecodeError, RecursionError):
        return 0
    if not isinstance(parsed_data, list):
        return 0
    if len(parsed_data) > MAX_PLUGIN_INDEX_ENTRIES:
        raise HTTPException(status_code=413, detail="插件列表条目过多")
    return len(parsed_data)


async def _report_raw_fetch_failure(detail: str) -> None:
    """推送不含内部异常文本的 Raw 获取失败状态。"""
    try:
        await update_progress(
            stage="error",
            progress=0,
            message="加载失败",
            error=detail,
            total_plugins=0,
            loaded_plugins=0,
        )
    except Exception as exc:
        logger.warning("推送 Raw 文件失败进度失败", error_type=type(exc).__name__)


async def _report_market_operation_failure(operation: str, plugin_id: str, detail: str) -> None:
    message = "市场插件安装失败" if operation == "install" else "市场插件版本切换失败"
    try:
        await update_progress(
            stage="error",
            progress=0,
            message=message,
            error=detail,
            operation=operation,
            plugin_id=plugin_id,
        )
    except Exception as exc:
        logger.warning("推送市场插件失败进度失败", error_type=type(exc).__name__)


def _prepare_plugin_manifest(plugin_path: Path, plugin_id: str) -> Dict[str, Any]:
    manifest = _load_plugin_manifest(plugin_path)
    if manifest is None:
        raise HTTPException(status_code=400, detail="无效的插件：缺少 _manifest.json")
    manifest_version = manifest.get("manifest_version")
    if manifest_version not in {1, 2}:
        raise HTTPException(status_code=400, detail="插件清单版本无效")

    if manifest_version == 2:
        manifest_path = _plugin_file_path(plugin_path, "_manifest.json")
        manifest_content = _read_limited_utf8(manifest_path, MAX_PLUGIN_MANIFEST_BYTES, "插件清单")
        try:
            parsed_manifest = parse_manifest_v2(manifest_content)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="插件清单格式无效") from exc
        if parsed_manifest.id != plugin_id:
            raise HTTPException(status_code=400, detail="插件 ID 与 Manifest 不一致")
        if not ManifestValidator().validate_manifest(parsed_manifest.model_dump(mode="json")):
            raise HTTPException(status_code=400, detail="插件与当前版本不兼容")
        return parsed_manifest.model_dump(mode="json")

    _safe_manifest_text(manifest.get("name"), "名称", MAX_PLUGIN_NAME_CHARS)
    _safe_manifest_text(manifest.get("version"), "版本", MAX_PLUGIN_VERSION_CHARS)
    author = manifest.get("author")
    if isinstance(author, dict):
        _safe_manifest_text(author.get("name"), "作者", MAX_PLUGIN_AUTHOR_CHARS)
    else:
        _safe_manifest_text(author, "作者", MAX_PLUGIN_AUTHOR_CHARS)

    declared_id = manifest.get("id")
    if declared_id is not None and declared_id != plugin_id:
        raise HTTPException(status_code=400, detail="插件 ID 与 Manifest 不一致")
    manifest["id"] = plugin_id
    try:
        manifest_content = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="插件清单格式无效") from exc
    manifest_path = _plugin_file_path(plugin_path, "_manifest.json")
    _atomic_write_bytes(manifest_path, manifest_content, MAX_PLUGIN_MANIFEST_BYTES, "插件清单")
    return manifest


def _prepare_market_plugin_candidate(
    plugin_path: Path,
    reviewed_manifest: ManifestV2,
) -> ManifestV2:
    plugin_file = _plugin_file_path(plugin_path, "plugin.py")
    manifest_path = _plugin_file_path(plugin_path, "_manifest.json")
    _require_regular_file(plugin_file, "插件入口")
    manifest_content = _read_limited_utf8(manifest_path, MAX_PLUGIN_MANIFEST_BYTES, "插件清单")
    try:
        manifest = parse_manifest_v2(manifest_content)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="插件清单格式无效") from exc
    if manifest != reviewed_manifest:
        raise HTTPException(status_code=400, detail="插件清单与市场审核记录不一致")

    git_path = plugin_path / ".git"
    if os.path.lexists(git_path):
        if git_path.is_symlink() or not git_path.is_dir():
            raise HTTPException(status_code=400, detail="插件 Git 元数据路径无效")
        _remove_plugin_tree(git_path)

    entry_count = 0
    total_bytes = 0
    for current_root, directory_names, file_names in os.walk(plugin_path, topdown=True, followlinks=False):
        current_path = Path(current_root)
        for name in directory_names + file_names:
            entry_path = current_path / name
            try:
                entry_stat = os.lstat(entry_path)
            except OSError as exc:
                raise HTTPException(status_code=400, detail="插件目录内容无效") from exc
            entry_count += 1
            if entry_count > MAX_MARKET_PLUGIN_ENTRIES:
                raise HTTPException(status_code=413, detail="插件文件数量超过限制")
            if stat.S_ISLNK(entry_stat.st_mode):
                raise HTTPException(status_code=400, detail="市场插件不能包含符号链接")
            if stat.S_ISREG(entry_stat.st_mode):
                total_bytes += entry_stat.st_size
                if total_bytes > MAX_MARKET_PLUGIN_BYTES:
                    raise HTTPException(status_code=413, detail="插件文件总大小超过限制")
            elif not stat.S_ISDIR(entry_stat.st_mode):
                raise HTTPException(status_code=400, detail="市场插件只能包含普通文件和目录")

    validator = ManifestValidator()
    if not validator.validate_manifest(manifest.model_dump(mode="json")):
        raise HTTPException(status_code=400, detail="插件与当前版本不兼容")
    return manifest


def _is_market_manifest_compatible(manifest: ManifestV2) -> bool:
    return ManifestValidator().validate_manifest(manifest.model_dump(mode="json"))


def _require_installed_market_source(
    plugin_path: Path,
    plugin_id: str,
    source: InstallationSource,
) -> ManifestV2:
    manifest_path = _plugin_file_path(plugin_path, "_manifest.json")
    manifest_content = _read_limited_utf8(manifest_path, MAX_PLUGIN_MANIFEST_BYTES, "插件清单")
    try:
        manifest = parse_manifest_v2(manifest_content)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail="本地插件与市场安装来源不一致") from exc

    if (
        manifest.id != plugin_id
        or manifest.version != source.installed_version
        or source.repository_url is None
        or manifest.urls.repository != source.repository_url
    ):
        raise HTTPException(status_code=409, detail="本地插件与市场安装来源不一致")
    return manifest


def _verified_installation_source(
    plugin_id: str,
    source: InstallationSource | None,
    *,
    plugin_path: Path | None = None,
) -> InstallationSource | None:
    if source is None or source.install_method != "market":
        return source

    try:
        if plugin_path is None:
            plugins_dir = _plugins_directory()
            plugin_path = _lifecycle_plugin_path(plugin_id, plugins_dir) if plugins_dir is not None else None
        if plugin_path is None:
            raise HTTPException(status_code=404, detail="插件未安装")
        _require_installed_market_source(plugin_path, plugin_id, source)
    except HTTPException:
        logger.warning(
            "本地插件与市场安装来源不一致，已隐藏来源信息",
            event_code="plugin.market.local_source_drift",
            plugin_hash=hash_id(plugin_id),
        )
        return None
    return source


def _verified_registry_installation_source(
    plugin_id: str,
    source: InstallationSource | None,
    *,
    registry_url: str,
    plugin: RegistryPlugin,
) -> InstallationSource | None:
    """Return a market binding only when it is verified against this Registry."""
    if source is None or source.install_method != "market":
        return source
    if source.registry_url != registry_url:
        return None

    matching_release = next(
        (
            release
            for release in plugin.versions
            if release.version == source.installed_version
            and release.ref == source.source_ref
            and release.commit == source.source_commit
            and release.sha256 == source.artifact_sha256
        ),
        None,
    )
    if source.plugin_id == plugin_id and source.repository_url == plugin.repository and matching_release is not None:
        return source

    logger.warning(
        "插件安装来源与 Registry 审核坐标不一致，已隐藏来源信息",
        event_code="plugin.market.registry_source_drift",
        plugin_hash=hash_id(plugin_id),
    )
    return None


def _existing_plugin_metadata(plugin_path: Path, plugin_id: str) -> tuple[str, str]:
    try:
        manifest = _load_plugin_manifest(plugin_path)
        if manifest is None:
            return plugin_id, "unknown"
        name = _safe_manifest_text(manifest.get("name"), "名称", MAX_PLUGIN_NAME_CHARS)
        version = _safe_manifest_text(manifest.get("version"), "版本", MAX_PLUGIN_VERSION_CHARS)
        return name, version
    except (HTTPException, OSError, RuntimeError, TypeError, ValueError):
        return plugin_id, "unknown"


def _lifecycle_plugin_path(plugin_id: str, plugins_dir: Path) -> Optional[Path]:
    for folder_name in (plugin_id.replace(".", "_"), plugin_id):
        candidate = plugins_dir / folder_name
        if not os.path.lexists(candidate):
            continue
        if candidate.is_symlink():
            raise HTTPException(status_code=400, detail="插件目录路径无效")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail="插件目录路径无效") from exc
        if not resolved.is_relative_to(plugins_dir) or not resolved.is_dir():
            raise HTTPException(status_code=400, detail="插件目录路径无效")
        manifest = _load_plugin_manifest(resolved)
        if manifest is not None and manifest.get("id") not in {None, plugin_id}:
            continue
        return resolved
    return _find_plugin_path(plugin_id)


def _remove_readonly(func, path: str, _error: Any) -> None:
    try:
        os.chmod(path, stat.S_IWRITE, follow_symlinks=False)
    except (NotImplementedError, TypeError):
        if not os.path.islink(path):
            os.chmod(path, stat.S_IWRITE)
    func(path)


def _remove_plugin_tree(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise HTTPException(status_code=400, detail="插件目录路径无效")
    shutil.rmtree(path, onerror=_remove_readonly)


def _cleanup_staging_root(path: Optional[Path]) -> None:
    if path is None or not os.path.lexists(path):
        return
    try:
        if path.is_symlink():
            path.unlink()
        else:
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        logger.warning("清理插件临时目录失败")


def _reserve_hidden_path(parent: Path, prefix: str) -> Path:
    reserved = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    reserved.rmdir()
    return reserved


def _directory_identity(path: Path) -> tuple[int, int]:
    file_stat = os.lstat(path)
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISDIR(file_stat.st_mode):
        raise HTTPException(status_code=400, detail="插件目录路径无效")
    return file_stat.st_dev, file_stat.st_ino


def _staged_plugin_identity(staged_path: Path, staging_root: Path) -> tuple[int, int]:
    if staging_root.is_symlink() or staged_path.is_symlink():
        raise HTTPException(status_code=400, detail="插件临时目录路径无效")
    try:
        root_resolved = staging_root.resolve(strict=True)
        staged_resolved = staged_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail="插件临时目录路径无效") from exc
    if not staged_resolved.is_relative_to(root_resolved) or not staged_resolved.is_dir():
        raise HTTPException(status_code=400, detail="插件临时目录路径无效")
    return _directory_identity(staged_resolved)


def _require_directory_identity(path: Path, expected: tuple[int, int]) -> None:
    try:
        current = _directory_identity(path)
    except (FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=409, detail="插件目录已发生变化，请重试") from exc
    if current != expected:
        raise HTTPException(status_code=409, detail="插件目录已发生变化，请重试")


def _plugin_runtime_config_files(plugin_id: str) -> frozenset[str]:
    config_files = set(DEFAULT_PLUGIN_RUNTIME_CONFIG_FILES)
    instance = find_plugin_instance(plugin_id)
    if instance is None:
        return frozenset(config_files)

    try:
        config_file_name = instance.config_file_name
    except Exception as exc:
        logger.warning("读取插件运行配置文件名失败", plugin_id=plugin_id, error_type=type(exc).__name__)
        return frozenset(config_files)
    if not config_file_name:
        return frozenset(config_files)
    if (
        not isinstance(config_file_name, str)
        or Path(config_file_name).name != config_file_name
        or Path(config_file_name).is_absolute()
        or Path(config_file_name).suffix.lower() != ".toml"
    ):
        logger.warning("忽略无效的插件运行配置文件名", plugin_id=plugin_id)
        return frozenset(config_files)
    config_files.add(config_file_name)
    return frozenset(config_files)


def _install_staged_plugin(staged_path: Path, target_path: Path, staged_identity: tuple[int, int]) -> None:
    _require_directory_identity(staged_path, staged_identity)
    if os.path.lexists(target_path):
        raise HTTPException(status_code=409, detail="插件目录已存在")
    try:
        os.replace(staged_path, target_path)
    except OSError as exc:
        if os.path.lexists(target_path):
            raise HTTPException(status_code=409, detail="插件目录已存在") from exc
        raise


def _activate_plugin_replacement(
    plugin_path: Path,
    staged_path: Path,
    plugins_dir: Path,
    expected_identity: tuple[int, int],
    staged_identity: tuple[int, int],
    config_files: frozenset[str] = DEFAULT_PLUGIN_RUNTIME_CONFIG_FILES,
) -> tuple[Path, tuple[str, ...]]:
    _require_directory_identity(plugin_path, expected_identity)
    _require_directory_identity(staged_path, staged_identity)
    runtime_entries = collect_plugin_runtime_entries(plugin_path, staged_path, config_files)
    backup_path = _reserve_hidden_path(plugins_dir, ".plugin-backup-")
    os.replace(plugin_path, backup_path)
    try:
        os.replace(staged_path, plugin_path)
    except BaseException:
        try:
            os.replace(backup_path, plugin_path)
        except OSError as rollback_error:
            logger.critical("插件更新回滚失败", error_type=type(rollback_error).__name__)
        raise
    try:
        move_plugin_runtime_entries(backup_path, plugin_path, runtime_entries)
    except BaseException:
        try:
            _restore_plugin_replacement(plugin_path, backup_path, plugins_dir, runtime_entries)
        except Exception as rollback_error:
            logger.critical("插件运行数据迁移回滚失败", error_type=type(rollback_error).__name__)
        raise
    return backup_path, runtime_entries


def _restore_plugin_replacement(
    plugin_path: Path,
    backup_path: Path,
    plugins_dir: Path,
    runtime_entries: tuple[str, ...],
) -> None:
    restore_plugin_runtime_entries(plugin_path, backup_path, runtime_entries)
    failed_path: Path | None = None
    try:
        if os.path.lexists(plugin_path):
            failed_path = _reserve_hidden_path(plugins_dir, ".plugin-failed-")
            os.replace(plugin_path, failed_path)
        os.replace(backup_path, plugin_path)
    except BaseException:
        if failed_path is not None and os.path.lexists(failed_path):
            try:
                os.replace(failed_path, plugin_path)
            except OSError as rollback_error:
                logger.critical("恢复插件更新候选失败", error_type=type(rollback_error).__name__)
        if os.path.lexists(plugin_path) and os.path.lexists(backup_path):
            try:
                move_plugin_runtime_entries(backup_path, plugin_path, runtime_entries)
            except Exception as rollback_error:
                logger.critical("恢复插件运行数据失败", error_type=type(rollback_error).__name__)
        raise

    if failed_path is not None:
        try:
            _remove_plugin_tree(failed_path)
        except Exception as cleanup_error:
            logger.error("清理插件失败版本失败", error_type=type(cleanup_error).__name__)


def _finalize_plugin_replacement(backup_path: Path) -> None:
    try:
        _remove_plugin_tree(backup_path)
    except Exception as cleanup_error:
        logger.error("清理插件旧版本失败", error_type=type(cleanup_error).__name__)


def _replace_plugin_directory(
    plugin_path: Path,
    staged_path: Path,
    plugins_dir: Path,
    expected_identity: tuple[int, int],
    staged_identity: tuple[int, int],
    config_files: frozenset[str] = DEFAULT_PLUGIN_RUNTIME_CONFIG_FILES,
) -> None:
    backup_path, _runtime_entries = _activate_plugin_replacement(
        plugin_path,
        staged_path,
        plugins_dir,
        expected_identity,
        staged_identity,
        config_files,
    )
    _finalize_plugin_replacement(backup_path)


def _stage_plugin_uninstall(
    plugin_path: Path,
    plugins_dir: Path,
    expected_identity: tuple[int, int],
) -> Path:
    _require_directory_identity(plugin_path, expected_identity)
    trash_path = _reserve_hidden_path(plugins_dir, ".plugin-uninstall-")
    os.replace(plugin_path, trash_path)
    return trash_path


def _restore_plugin_uninstall(plugin_path: Path, trash_path: Path) -> None:
    if os.path.lexists(plugin_path):
        raise HTTPException(status_code=409, detail="插件目录已发生变化，无法恢复")
    os.replace(trash_path, plugin_path)


def _uninstall_plugin_directory(
    plugin_path: Path,
    plugins_dir: Path,
    expected_identity: tuple[int, int],
) -> None:
    trash_path = _stage_plugin_uninstall(plugin_path, plugins_dir, expected_identity)
    try:
        _remove_plugin_tree(trash_path)
    except BaseException:
        try:
            _restore_plugin_uninstall(plugin_path, trash_path)
        except OSError as rollback_error:
            logger.critical("插件卸载回滚失败", error_type=type(rollback_error).__name__)
        raise


def _prune_config_backups(plugin_path: Path) -> None:
    backups = [
        path
        for path in plugin_path.iterdir()
        if path.name.startswith(("config.toml.backup.", "config.toml.reset.")) and (path.is_file() or path.is_symlink())
    ]
    backups.sort(key=lambda path: (path.name.rsplit(".", 1)[-1], path.name), reverse=True)
    for obsolete_backup in backups[MAX_PLUGIN_CONFIG_BACKUPS:]:
        try:
            obsolete_backup.unlink()
        except OSError as e:
            log_exception_type(logger, "清理旧插件配置备份失败", e, level="warning")


def _create_config_backup(plugin_path: Path, config_path: Path, kind: str) -> Path:
    content = _read_limited_bytes(config_path, MAX_PLUGIN_CONFIG_BYTES, "插件配置")
    timestamp = f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}.{time.time_ns()}"
    backup_path = _plugin_file_path(plugin_path, f"config.toml.{kind}.{timestamp}")
    _atomic_write_bytes(backup_path, content, MAX_PLUGIN_CONFIG_BYTES, "插件配置备份")
    _prune_config_backups(plugin_path)
    return backup_path


def _move_config_to_reset_backup(plugin_path: Path, config_path: Path) -> Path:
    _read_limited_bytes(config_path, MAX_PLUGIN_CONFIG_BYTES, "插件配置")
    timestamp = f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}.{time.time_ns()}"
    backup_path = _plugin_file_path(plugin_path, f"config.toml.reset.{timestamp}")
    os.replace(config_path, backup_path)
    _prune_config_backups(plugin_path)
    return backup_path


def _render_plugin_toml(config_path: Path, data: Any) -> bytes:
    existing_content = ""
    if config_path.exists():
        existing_content = _read_limited_utf8(config_path, MAX_PLUGIN_CONFIG_BYTES, "插件配置")
        try:
            import tomlkit

            tomlkit.loads(existing_content)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="现有插件配置格式无效") from exc

    file_descriptor, temp_name = tempfile.mkstemp(prefix=".config-render.", dir=config_path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "wb") as file:
            file_descriptor = -1
            file.write(existing_content.encode("utf-8"))
        try:
            save_toml_with_format(data, str(temp_path))
        except OSError:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail="插件配置格式无效") from exc
        rendered = _read_limited_bytes(temp_path, MAX_PLUGIN_CONFIG_BYTES, "插件配置")
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        temp_path.unlink(missing_ok=True)
    return rendered


def parse_version(version_str: str) -> tuple[int, int, int]:
    """
    解析版本号字符串

    支持格式:
    - 0.11.2 -> (0, 11, 2)
    - 0.11.2.snapshot.2 -> (0, 11, 2)

    Returns:
        (major, minor, patch) 三元组
    """
    # 移除 snapshot、dev、alpha、beta 等后缀（支持 - 和 . 分隔符）
    import re

    # 匹配 -snapshot.X, .snapshot, -dev, .dev, -alpha, .alpha, -beta, .beta 等后缀
    base_version = re.split(r"[-.](?:snapshot|dev|alpha|beta|rc)", version_str, flags=re.IGNORECASE)[0]

    parts = base_version.split(".")
    if len(parts) < 3:
        # 补齐到 3 位
        parts.extend(["0"] * (3 - len(parts)))

    try:
        major = int(parts[0])
        minor = int(parts[1])
        patch = int(parts[2])
        return (major, minor, patch)
    except (ValueError, IndexError):
        logger.warning("无法解析版本号，返回默认值 (0, 0, 0)")
        return (0, 0, 0)


# ============ 工具函数（避免在请求内重复定义） ============


def _deep_merge(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    """深度合并两个字典，src 的值会覆盖或合并到 dst 中。"""
    for k, v in src.items():
        if k in dst and isinstance(dst[k], dict) and isinstance(v, dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


def normalize_dotted_keys(obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    将形如 {'a.b': 1} 的键展开为嵌套结构 {'a': {'b': 1}}。
    若遇到中间节点已存在且非字典，记录日志并覆盖为字典。
    """
    result: Dict[str, Any] = {}
    dotted_items = []

    # 先处理非点号键，避免后续展开覆盖已有结构
    for k, v in obj.items():
        if "." in k:
            dotted_items.append((k, v))
        else:
            result[k] = normalize_dotted_keys(v) if isinstance(v, dict) else v

    # 再处理点号键
    for dotted_key, v in dotted_items:
        value = normalize_dotted_keys(v) if isinstance(v, dict) else v
        parts = dotted_key.split(".")
        if "" in parts:
            logger.warning("插件配置键路径包含空段", key_hash=hash_id(dotted_key))
            parts = [p for p in parts if p]
        if not parts:
            logger.warning("忽略空插件配置键路径", key_hash=hash_id(dotted_key))
            continue
        current = result
        # 中间层
        for idx, part in enumerate(parts[:-1]):
            if part in current and not isinstance(current[part], dict):
                path_ctx = ".".join(parts[: idx + 1])
                logger.warning(
                    "插件配置键冲突，覆盖非字典中间节点",
                    key_hash=hash_id(dotted_key),
                    path_hash=hash_id(path_ctx),
                )
                current[part] = {}
            current = current.setdefault(part, {})
        # 最后一层
        last_part = parts[-1]
        if last_part in current and isinstance(current[last_part], dict) and isinstance(value, dict):
            _deep_merge(current[last_part], value)
        else:
            current[last_part] = value

    return result


def coerce_types(schema_part: Dict[str, Any], config_part: Dict[str, Any]) -> None:
    """
    根据 schema 将配置中的类型纠正（目前只纠正 list-from-str）。
    """

    def _is_list_type(tp: Any) -> bool:
        origin = get_origin(tp)
        return tp is list or origin is list

    for key, schema_val in schema_part.items():
        if key not in config_part:
            continue
        value = config_part[key]
        if isinstance(schema_val, ConfigField):
            if _is_list_type(schema_val.type) and isinstance(value, str):
                config_part[key] = [item.strip() for item in value.split(",") if item.strip()]
        elif isinstance(schema_val, dict) and isinstance(value, dict):
            coerce_types(schema_val, value)


def find_plugin_instance(plugin_id: str) -> Optional[Any]:
    """
    按 plugin_id 或 plugin_name 查找已加载的插件实例。
    局部导入 plugin_manager 以规避循环依赖。
    """
    from src.plugin_system.core.plugin_manager import plugin_manager

    for loaded_plugin_name in plugin_manager.list_loaded_plugins():
        instance = plugin_manager.get_plugin_instance(loaded_plugin_name)
        if instance and (instance.plugin_name == plugin_id or instance.get_manifest_info("id", "") == plugin_id):
            return instance
    return None


# ============ 请求/响应模型 ============


class FetchRawFileRequest(BaseModel):
    """获取 Raw 文件请求"""

    owner: str = Field(..., description="仓库所有者", example="MaiM-with-u")
    repo: str = Field(..., description="仓库名称", example="plugin-repo")
    branch: str = Field(..., description="分支名称", example="main")
    file_path: str = Field(..., description="文件路径", example="plugin_details.json")
    mirror_id: Optional[str] = Field(None, description="指定镜像源 ID")
    custom_url: Optional[str] = Field(None, description="自定义完整 URL")


class FetchRawFileResponse(BaseModel):
    """获取 Raw 文件响应"""

    success: bool = Field(..., description="是否成功")
    data: Optional[str] = Field(None, description="文件内容")
    error: Optional[str] = Field(None, description="错误信息")
    mirror_used: Optional[str] = Field(None, description="使用的镜像源")
    attempts: int = Field(..., description="尝试次数")
    url: Optional[str] = Field(None, description="实际请求的 URL")


class CloneRepositoryRequest(BaseModel):
    """克隆仓库请求"""

    owner: str = Field(..., description="仓库所有者", example="MaiM-with-u")
    repo: str = Field(..., description="仓库名称", example="plugin-repo")
    target_path: str = Field(..., description="目标路径（相对于插件目录）")
    branch: Optional[str] = Field(None, description="分支名称", example="main")
    mirror_id: Optional[str] = Field(None, description="指定镜像源 ID")
    custom_url: Optional[str] = Field(None, description="自定义克隆 URL")
    depth: Optional[int] = Field(None, description="克隆深度（浅克隆）", ge=1, le=1000)


class CloneRepositoryResponse(BaseModel):
    """克隆仓库响应"""

    success: bool = Field(..., description="是否成功")
    path: Optional[str] = Field(None, description="克隆路径")
    error: Optional[str] = Field(None, description="错误信息")
    mirror_used: Optional[str] = Field(None, description="使用的镜像源")
    attempts: int = Field(..., description="尝试次数")
    url: Optional[str] = Field(None, description="实际克隆的 URL")
    message: Optional[str] = Field(None, description="附加信息")


class MirrorConfigResponse(BaseModel):
    """镜像源配置响应"""

    id: str = Field(..., description="镜像源 ID")
    name: str = Field(..., description="镜像源名称")
    raw_prefix: str = Field(..., description="Raw 文件前缀")
    clone_prefix: str = Field(..., description="克隆前缀")
    enabled: bool = Field(..., description="是否启用")
    priority: int = Field(..., description="优先级（数字越小优先级越高）")


class AvailableMirrorsResponse(BaseModel):
    """可用镜像源列表响应"""

    mirrors: List[MirrorConfigResponse] = Field(..., description="镜像源列表")
    default_priority: List[str] = Field(..., description="默认优先级顺序（ID 列表）")


class AddMirrorRequest(BaseModel):
    """添加镜像源请求"""

    id: str = Field(..., description="镜像源 ID", example="custom-mirror")
    name: str = Field(..., description="镜像源名称", example="自定义镜像源")
    raw_prefix: str = Field(..., description="Raw 文件前缀", example="https://example.com/raw")
    clone_prefix: str = Field(..., description="克隆前缀", example="https://example.com/clone")
    enabled: bool = Field(True, description="是否启用")
    priority: Optional[int] = Field(None, description="优先级")


class UpdateMirrorRequest(BaseModel):
    """更新镜像源请求"""

    name: Optional[str] = Field(None, description="镜像源名称")
    raw_prefix: Optional[str] = Field(None, description="Raw 文件前缀")
    clone_prefix: Optional[str] = Field(None, description="克隆前缀")
    enabled: Optional[bool] = Field(None, description="是否启用")
    priority: Optional[int] = Field(None, description="优先级")


class GitStatusResponse(BaseModel):
    """Git 安装状态响应"""

    installed: bool = Field(..., description="是否已安装 Git")
    version: Optional[str] = Field(None, description="Git 版本号")
    path: Optional[str] = Field(None, description="Git 可执行文件路径")
    error: Optional[str] = Field(None, description="错误信息")


class InstallPluginRequest(BaseModel):
    """安装插件请求"""

    plugin_id: str = Field(..., description="插件 ID")
    repository_url: str = Field(..., description="插件仓库 URL")
    branch: Optional[str] = Field("main", description="分支名称")
    mirror_id: Optional[str] = Field(None, description="指定镜像源 ID")


class MarketPluginRequest(BaseModel):
    """Install or switch to a reviewed market version."""

    model_config = ConfigDict(extra="forbid")

    plugin_id: str = Field(..., min_length=1, max_length=MAX_PLUGIN_ID_CHARS)
    version: str = Field(..., min_length=1, max_length=MAX_PLUGIN_VERSION_CHARS)


class VersionResponse(BaseModel):
    """璃夜版本响应"""

    version: str = Field(..., description="璃夜版本号")
    version_major: int = Field(..., description="主版本号")
    version_minor: int = Field(..., description="次版本号")
    version_patch: int = Field(..., description="补丁版本号")


class UninstallPluginRequest(BaseModel):
    """卸载插件请求"""

    plugin_id: str = Field(..., description="插件 ID")


class UpdatePluginRequest(BaseModel):
    """更新插件请求"""

    plugin_id: str = Field(..., description="插件 ID")
    repository_url: str = Field(..., description="插件仓库 URL")
    branch: Optional[str] = Field("main", description="分支名称")
    mirror_id: Optional[str] = Field(None, description="指定镜像源 ID")


class PluginMarketRegistryResponse(BaseModel):
    name: str
    url: str
    repository_url: str
    fetched_at: datetime.datetime


class PluginMarketAuthorResponse(BaseModel):
    name: str
    url: str | None


class PluginMarketCompatibilityResponse(BaseModel):
    min_version: str
    max_version: str | None


class PluginMarketVersionResponse(BaseModel):
    version: str
    ref: str
    commit: str
    status: str
    released_at: datetime.datetime
    stable: bool
    installable: bool
    host_application: PluginMarketCompatibilityResponse


class PluginMarketInstallationResponse(BaseModel):
    install_method: str
    installed_version: str
    registry_url: str | None
    repository_url: str | None
    source_ref: str | None
    source_commit: str | None
    updated_at: datetime.datetime


class PluginMarketItemResponse(BaseModel):
    id: str
    name: str
    description: str
    author: PluginMarketAuthorResponse
    license: str
    repository_url: str
    homepage_url: str | None
    status: str
    review_level: str
    updated_at: datetime.datetime
    capabilities: list[str]
    categories: list[str]
    keywords: list[str]
    host_application: PluginMarketCompatibilityResponse
    latest_version: str | None
    versions: list[PluginMarketVersionResponse]
    installation: PluginMarketInstallationResponse | None


class PluginMarketPaginationResponse(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int


class PluginMarketResponse(BaseModel):
    registry: PluginMarketRegistryResponse
    plugins: list[PluginMarketItemResponse]
    pagination: PluginMarketPaginationResponse


class PluginMarketDetailResponse(BaseModel):
    registry: PluginMarketRegistryResponse
    plugin: PluginMarketItemResponse


def _validate_market_request(request: MarketPluginRequest) -> tuple[str, str]:
    try:
        return validate_market_plugin_id(request.plugin_id), validate_semver(request.version)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="市场插件 ID 或版本无效") from exc


async def _resolve_market_release(
    registry_url: str,
    plugin_id: str,
    version: str,
) -> tuple[str, RegistryPlugin, RegistryVersion]:
    try:
        snapshot = await fetch_plugin_registry(registry_url)
    except PluginRegistryFetchError:
        raise HTTPException(status_code=502, detail="插件市场暂时不可用") from None
    except PluginRegistryFormatError:
        raise HTTPException(status_code=502, detail="插件市场数据无效") from None

    try:
        plugin = snapshot.registry.plugins[plugin_id]
        release = snapshot.registry.installable_version(plugin_id, version)
    except KeyError:
        raise HTTPException(status_code=404, detail="市场中不存在该插件或版本") from None
    except RegistryArtifactNotSupportedError:
        raise HTTPException(status_code=501, detail="市场制品下载暂未支持") from None
    except ValueError:
        raise HTTPException(status_code=409, detail="该插件或版本当前不可安装") from None
    if not _is_market_manifest_compatible(release.manifest):
        raise HTTPException(status_code=400, detail="插件与当前版本不兼容")
    return snapshot.registry_url, plugin, release


async def _clone_market_candidate(
    plugin: RegistryPlugin,
    release: RegistryVersion,
    staged_path: Path,
    staging_root: Path,
) -> tuple[ManifestV2, tuple[int, int]]:
    try:
        owner, repo, is_github, repository_url = parse_repository_url(plugin.repository)
    except ValueError:
        raise HTTPException(status_code=502, detail="插件市场仓库地址无效") from None

    clone_kwargs: dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "target_path": staged_path,
        "branch": release.ref,
        "depth": 1,
        "expected_commit": release.commit,
    }
    if not is_github:
        clone_kwargs["custom_url"] = repository_url

    result = await get_git_mirror_service().clone_repository(**clone_kwargs)
    if not isinstance(result, dict) or result.get("success") is not True:
        raise HTTPException(status_code=502, detail="插件文件下载失败")
    actual_commit = result.get("commit")
    if not isinstance(actual_commit, str) or actual_commit.lower() != release.commit:
        raise HTTPException(status_code=409, detail="插件来源与市场审核记录不一致")

    staged_identity = _staged_plugin_identity(staged_path, staging_root)
    manifest = _prepare_market_plugin_candidate(staged_path, release.manifest)
    _require_directory_identity(staged_path, staged_identity)
    return manifest, staged_identity


def _market_installation_source(
    *,
    plugin_id: str,
    registry_url: str,
    plugin: RegistryPlugin,
    release: RegistryVersion,
    installed_at: datetime.datetime | None = None,
) -> InstallationSource:
    now = datetime.datetime.now(datetime.timezone.utc)
    return InstallationSource(
        plugin_id=plugin_id,
        install_method="market",
        registry_url=registry_url,
        repository_url=plugin.repository,
        source_ref=release.ref,
        source_commit=release.commit,
        artifact_sha256=None,
        installed_version=release.version,
        installed_at=installed_at or now,
        updated_at=now,
        last_checked_at=now,
    )


def _clone_result_commit(result: dict[str, Any]) -> str:
    commit = result.get("commit")
    if (
        not isinstance(commit, str)
        or len(commit) not in {40, 64}
        or any(character not in "0123456789abcdefABCDEF" for character in commit)
    ):
        raise HTTPException(status_code=502, detail="无法确认插件源码版本")
    return commit.lower()


def _git_installation_source(
    *,
    plugin_id: str,
    repository_url: str,
    source_ref: str | None,
    source_commit: str,
    installed_version: str,
    installed_at: datetime.datetime | None = None,
) -> InstallationSource:
    now = datetime.datetime.now(datetime.timezone.utc)
    if repository_url.endswith(".git"):
        repository_url = repository_url[:-4]
    return InstallationSource(
        plugin_id=plugin_id,
        install_method="git",
        registry_url=None,
        repository_url=repository_url,
        source_ref=source_ref,
        source_commit=source_commit,
        artifact_sha256=None,
        installed_version=installed_version,
        installed_at=installed_at or now,
        updated_at=now,
        last_checked_at=now,
    )


def _market_installation_response(source: InstallationSource | None) -> PluginMarketInstallationResponse | None:
    if source is None:
        return None
    return PluginMarketInstallationResponse(
        install_method=source.install_method,
        installed_version=source.installed_version,
        registry_url=source.registry_url,
        repository_url=source.repository_url,
        source_ref=source.source_ref,
        source_commit=source.source_commit,
        updated_at=source.updated_at,
    )


def _is_current_registry_installation(
    installation: InstallationSource | None,
    *,
    plugin_id: str,
    registry_url: str,
) -> bool:
    return bool(
        installation is not None
        and installation.plugin_id == plugin_id
        and installation.install_method == "market"
        and installation.registry_url == registry_url
    )


def _market_plugin_matches_query(plugin_id: str, plugin: RegistryPlugin, normalized_query: str) -> bool:
    if not normalized_query:
        return True
    for version in plugin.versions:
        manifest = version.manifest
        searchable = " ".join((plugin_id, manifest.name, manifest.description, manifest.author.name)).casefold()
        if normalized_query in searchable:
            return True
    return False


def _market_item_response(
    registry: PluginRegistry,
    plugin_id: str,
    plugin: RegistryPlugin,
    installation: InstallationSource | None,
) -> PluginMarketItemResponse:
    version_compatibility = {
        version.version: _is_market_manifest_compatible(version.manifest) for version in plugin.versions
    }
    stable_candidates = [
        version
        for version in plugin.versions
        if (
            plugin.status == "approved"
            and version.status == "approved"
            and version.is_stable
            and version.artifact_url is None
            and version_compatibility[version.version]
        )
    ]
    latest = max(
        stable_candidates,
        key=registry_version_order_key,
        default=None,
    )
    metadata_version = latest or max(plugin.versions, key=registry_version_order_key)
    manifest = metadata_version.manifest
    versions = [
        PluginMarketVersionResponse(
            version=version.version,
            ref=version.ref,
            commit=version.commit,
            status=version.status,
            released_at=version.released_at,
            stable=version.is_stable,
            installable=(
                plugin.status == "approved"
                and version.status == "approved"
                and version.artifact_url is None
                and version_compatibility[version.version]
            ),
            host_application=PluginMarketCompatibilityResponse(
                min_version=version.manifest.host_application.min_version,
                max_version=version.manifest.host_application.max_version,
            ),
        )
        for version in sorted(
            plugin.versions,
            key=lambda item: (item.released_at, item.version, item.ref),
            reverse=True,
        )
    ]
    return PluginMarketItemResponse(
        id=plugin_id,
        name=manifest.name,
        description=manifest.description,
        author=PluginMarketAuthorResponse(name=manifest.author.name, url=manifest.author.url),
        license=manifest.license,
        repository_url=plugin.repository,
        homepage_url=manifest.urls.homepage,
        status=plugin.status,
        review_level=plugin.review_level,
        updated_at=plugin.updated_at,
        capabilities=list(manifest.capabilities),
        categories=list(manifest.categories),
        keywords=list(manifest.keywords),
        host_application=PluginMarketCompatibilityResponse(
            min_version=manifest.host_application.min_version,
            max_version=manifest.host_application.max_version,
        ),
        latest_version=latest.version if latest is not None else None,
        versions=versions,
        installation=_market_installation_response(installation),
    )


# ============ API 路由 ============


@router.get("/version", response_model=VersionResponse)
async def get_maimai_version() -> VersionResponse:
    """
    获取璃夜版本信息

    此接口无需认证，用于前端检查插件兼容性
    """
    major, minor, patch = parse_version(MMC_VERSION)

    return VersionResponse(version=MMC_VERSION, version_major=major, version_minor=minor, version_patch=patch)


@router.get("/market", response_model=PluginMarketResponse)
async def get_plugin_market(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    query: str | None = Query(None, max_length=128),
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> PluginMarketResponse:
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    token_manager = get_token_manager()
    if not token or not token_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    try:
        snapshot = await fetch_plugin_registry(
            global_config.webui.plugin_registry_url,
            cache_ttl_seconds=PLUGIN_REGISTRY_CATALOG_CACHE_SECONDS,
        )
        installations = PluginInstallationStore().list_all()
    except PluginRegistryFetchError:
        raise HTTPException(status_code=502, detail="插件市场暂时不可用") from None
    except PluginRegistryFormatError:
        raise HTTPException(status_code=502, detail="插件市场数据无效") from None
    except Exception as exc:
        raise internal_server_error(logger, "加载插件市场失败", exc) from None

    normalized_query = query.strip().casefold() if query else ""
    eligible_plugins: list[tuple[str, RegistryPlugin, InstallationSource | None]] = []
    for plugin_id, plugin in sorted(snapshot.registry.plugins.items()):
        installation = _verified_installation_source(plugin_id, installations.get(plugin_id))
        installation = _verified_registry_installation_source(
            plugin_id,
            installation,
            registry_url=snapshot.registry_url,
            plugin=plugin,
        )
        if plugin.status != "approved" and not _is_current_registry_installation(
            installation,
            plugin_id=plugin_id,
            registry_url=snapshot.registry_url,
        ):
            continue
        eligible_plugins.append((plugin_id, plugin, installation))

    if normalized_query:
        eligible_plugins = [
            item for item in eligible_plugins if _market_plugin_matches_query(item[0], item[1], normalized_query)
        ]
    total = len(eligible_plugins)
    start = (page - 1) * page_size
    paginated = [
        _market_item_response(snapshot.registry, plugin_id, plugin, installation)
        for plugin_id, plugin, installation in eligible_plugins[start : start + page_size]
    ]
    return PluginMarketResponse(
        registry=PluginMarketRegistryResponse(
            name=snapshot.registry.meta.name,
            url=snapshot.registry_url,
            repository_url=snapshot.registry.meta.repository,
            fetched_at=snapshot.fetched_at,
        ),
        plugins=paginated,
        pagination=PluginMarketPaginationResponse(
            page=page,
            page_size=page_size,
            total=total,
            total_pages=(total + page_size - 1) // page_size,
        ),
    )


@router.get("/market/{plugin_id}", response_model=PluginMarketDetailResponse)
async def get_bound_market_plugin(
    plugin_id: str,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> PluginMarketDetailResponse:
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    if not token or not get_token_manager().verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    try:
        plugin_id = validate_market_plugin_id(plugin_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="市场插件 ID 无效") from exc

    try:
        source = PluginInstallationStore().get(plugin_id)
        if source is None:
            raise HTTPException(status_code=404, detail="插件没有市场安装记录")
        if source.install_method != "market" or source.registry_url is None:
            raise HTTPException(status_code=409, detail="该插件不是市场安装")

        plugins_dir = _plugins_directory()
        plugin_path = _lifecycle_plugin_path(plugin_id, plugins_dir) if plugins_dir is not None else None
        if plugin_path is None:
            raise HTTPException(status_code=404, detail="插件未安装")
        _require_installed_market_source(plugin_path, plugin_id, source)

        snapshot = await fetch_plugin_registry(
            source.registry_url,
            cache_ttl_seconds=PLUGIN_REGISTRY_CATALOG_CACHE_SECONDS,
        )
        plugin = snapshot.registry.plugins.get(plugin_id)
        if plugin is None:
            raise HTTPException(status_code=404, detail="绑定的插件市场中不存在该插件")
        source = _verified_registry_installation_source(
            plugin_id,
            source,
            registry_url=snapshot.registry_url,
            plugin=plugin,
        )
        if source is None:
            raise HTTPException(status_code=409, detail="插件来源与市场审核记录不一致")
    except HTTPException:
        raise
    except PluginRegistryFetchError:
        raise HTTPException(status_code=502, detail="绑定的插件市场暂时不可用") from None
    except PluginRegistryFormatError:
        raise HTTPException(status_code=502, detail="绑定的插件市场数据无效") from None
    except Exception as exc:
        raise internal_server_error(logger, "加载绑定的插件市场版本失败", exc) from None

    return PluginMarketDetailResponse(
        registry=PluginMarketRegistryResponse(
            name=snapshot.registry.meta.name,
            url=snapshot.registry_url,
            repository_url=snapshot.registry.meta.repository,
            fetched_at=snapshot.fetched_at,
        ),
        plugin=_market_item_response(snapshot.registry, plugin_id, plugin, source),
    )


@router.post("/market/install")
async def install_market_plugin(
    request: MarketPluginRequest,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    if not token or not get_token_manager().verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    plugin_id, version = _validate_market_request(request)
    staging_root: Path | None = None
    try:
        await update_progress(
            stage="loading",
            progress=5,
            message=f"开始安装市场插件: {plugin_id}",
            operation="install",
            plugin_id=plugin_id,
        )
        registry_url, plugin, release = await _resolve_market_release(
            global_config.webui.plugin_registry_url,
            plugin_id,
            version,
        )

        plugins_dir = _plugins_directory(create=True)
        if plugins_dir is None:
            raise RuntimeError("插件目录创建失败")
        if _lifecycle_plugin_path(plugin_id, plugins_dir) is not None:
            raise HTTPException(status_code=409, detail="插件已安装")

        target_path = plugins_dir / plugin_id.replace(".", "_")
        staging_root = Path(tempfile.mkdtemp(prefix=".plugin-market-install-", dir=plugins_dir))
        staged_path = staging_root / "candidate"
        manifest, staged_identity = await _clone_market_candidate(plugin, release, staged_path, staging_root)

        if _lifecycle_plugin_path(plugin_id, plugins_dir) is not None:
            raise HTTPException(status_code=409, detail="插件目录已存在")
        _install_staged_plugin(staged_path, target_path, staged_identity)
        try:
            PluginInstallationStore().save(
                _market_installation_source(
                    plugin_id=plugin_id,
                    registry_url=registry_url,
                    plugin=plugin,
                    release=release,
                )
            )
        except Exception:
            try:
                _remove_plugin_tree(target_path)
            except Exception as rollback_error:
                logger.critical("市场插件安装回滚失败", error_type=type(rollback_error).__name__)
            raise

        await update_progress(
            stage="success",
            progress=100,
            message=f"成功安装插件: {manifest.name} v{manifest.version}",
            operation="install",
            plugin_id=plugin_id,
        )
        return {
            "success": True,
            "message": "插件安装成功",
            "plugin_id": plugin_id,
            "plugin_name": manifest.name,
            "version": manifest.version,
            "path": str(Path("plugins") / target_path.name),
        }
    except HTTPException as exc:
        await _report_market_operation_failure("install", plugin_id, str(exc.detail))
        raise
    except Exception as exc:
        logger.error("安装市场插件失败", plugin_id=plugin_id, error_type=type(exc).__name__)
        await _report_market_operation_failure("install", plugin_id, "插件安装失败")
        raise HTTPException(status_code=500, detail="插件安装失败") from None
    finally:
        _cleanup_staging_root(staging_root)


@router.post("/market/update")
async def update_market_plugin(
    request: MarketPluginRequest,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    if not token or not get_token_manager().verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    plugin_id, version = _validate_market_request(request)
    staging_root: Path | None = None
    try:
        store = PluginInstallationStore()
        source = store.get(plugin_id)
        if source is None:
            raise HTTPException(status_code=404, detail="插件没有市场安装记录")
        if source.install_method != "market" or source.registry_url is None:
            raise HTTPException(status_code=409, detail="该插件不是市场安装")

        plugins_dir = _plugins_directory()
        plugin_path = _lifecycle_plugin_path(plugin_id, plugins_dir) if plugins_dir is not None else None
        if plugin_path is None or plugins_dir is None:
            raise HTTPException(status_code=404, detail="插件未安装")
        installed_manifest = _require_installed_market_source(plugin_path, plugin_id, source)
        if installed_manifest.version == version:
            raise HTTPException(status_code=409, detail="当前已安装此版本")
        plugin_identity = _directory_identity(plugin_path)

        await update_progress(
            stage="loading",
            progress=5,
            message=f"开始更新市场插件: {plugin_id}",
            operation="update",
            plugin_id=plugin_id,
        )
        registry_url, plugin, release = await _resolve_market_release(source.registry_url, plugin_id, version)
        verified_source = _verified_registry_installation_source(
            plugin_id,
            source,
            registry_url=registry_url,
            plugin=plugin,
        )
        if verified_source is None:
            raise HTTPException(status_code=409, detail="插件来源与市场审核记录不一致")
        old_version = installed_manifest.version

        staging_root = Path(tempfile.mkdtemp(prefix=".plugin-market-update-", dir=plugins_dir))
        staged_path = staging_root / "candidate"
        manifest, staged_identity = await _clone_market_candidate(plugin, release, staged_path, staging_root)
        backup_path, runtime_entries = _activate_plugin_replacement(
            plugin_path,
            staged_path,
            plugins_dir,
            plugin_identity,
            staged_identity,
            _plugin_runtime_config_files(plugin_id),
        )
        try:
            store.save(
                _market_installation_source(
                    plugin_id=plugin_id,
                    registry_url=registry_url,
                    plugin=plugin,
                    release=release,
                    installed_at=source.installed_at,
                )
            )
        except Exception:
            try:
                _restore_plugin_replacement(plugin_path, backup_path, plugins_dir, runtime_entries)
            except Exception as rollback_error:
                logger.critical("市场插件更新回滚失败", error_type=type(rollback_error).__name__)
            raise
        _finalize_plugin_replacement(backup_path)

        await update_progress(
            stage="success",
            progress=100,
            message=f"成功更新 {manifest.name}: {old_version} -> {manifest.version}",
            operation="update",
            plugin_id=plugin_id,
        )
        return {
            "success": True,
            "message": "插件更新成功",
            "plugin_id": plugin_id,
            "plugin_name": manifest.name,
            "old_version": old_version,
            "new_version": manifest.version,
        }
    except HTTPException as exc:
        await _report_market_operation_failure("update", plugin_id, str(exc.detail))
        raise
    except Exception as exc:
        logger.error("更新市场插件失败", plugin_id=plugin_id, error_type=type(exc).__name__)
        await _report_market_operation_failure("update", plugin_id, "插件更新失败")
        raise HTTPException(status_code=500, detail="插件更新失败") from None
    finally:
        _cleanup_staging_root(staging_root)


@router.get("/git-status", response_model=GitStatusResponse)
async def check_git_status(
    maibot_session: Optional[str] = Cookie(None), authorization: Optional[str] = Header(None)
) -> GitStatusResponse:
    """
    检查本机 Git 安装状态
    """
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    token_manager = get_token_manager()
    if not token or not token_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    service = get_git_mirror_service()
    result = service.check_git_installed()

    return GitStatusResponse(**result)


@router.get("/mirrors", response_model=AvailableMirrorsResponse)
async def get_available_mirrors(
    maibot_session: Optional[str] = Cookie(None), authorization: Optional[str] = Header(None)
) -> AvailableMirrorsResponse:
    """
    获取所有可用的镜像源配置
    """
    # Token 验证
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    token_manager = get_token_manager()
    if not token or not token_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    service = get_git_mirror_service()
    config = service.get_mirror_config()

    all_mirrors = config.get_all_mirrors()
    mirrors = [
        MirrorConfigResponse(
            id=m["id"],
            name=m["name"],
            raw_prefix=m["raw_prefix"],
            clone_prefix=m["clone_prefix"],
            enabled=m["enabled"],
            priority=m["priority"],
        )
        for m in all_mirrors
    ]

    return AvailableMirrorsResponse(mirrors=mirrors, default_priority=config.get_default_priority_list())


@router.post("/mirrors", response_model=MirrorConfigResponse)
async def add_mirror(
    request: AddMirrorRequest, maibot_session: Optional[str] = Cookie(None), authorization: Optional[str] = Header(None)
) -> MirrorConfigResponse:
    """
    添加新的镜像源
    """
    # Token 验证
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    token_manager = get_token_manager()
    if not token or not token_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    try:
        validate_raw_url(request.raw_prefix)
        validate_clone_url(request.clone_prefix)
        service = get_git_mirror_service()
        config = service.get_mirror_config()

        mirror = config.add_mirror(
            mirror_id=request.id,
            name=request.name,
            raw_prefix=request.raw_prefix,
            clone_prefix=request.clone_prefix,
            enabled=request.enabled,
            priority=request.priority,
        )

        return MirrorConfigResponse(
            id=mirror["id"],
            name=mirror["name"],
            raw_prefix=mirror["raw_prefix"],
            clone_prefix=mirror["clone_prefix"],
            enabled=mirror["enabled"],
            priority=mirror["priority"],
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="镜像源配置无效") from None
    except Exception as e:
        logger.error("添加镜像源失败", error_type=type(e).__name__)
        raise HTTPException(status_code=500, detail="添加镜像源失败") from None


@router.put("/mirrors/{mirror_id}", response_model=MirrorConfigResponse)
async def update_mirror(
    mirror_id: str,
    request: UpdateMirrorRequest,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> MirrorConfigResponse:
    """
    更新镜像源配置
    """
    # Token 验证
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    token_manager = get_token_manager()
    if not token or not token_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    try:
        if request.raw_prefix is not None:
            validate_raw_url(request.raw_prefix)
        if request.clone_prefix is not None:
            validate_clone_url(request.clone_prefix)
        service = get_git_mirror_service()
        config = service.get_mirror_config()

        mirror = config.update_mirror(
            mirror_id=mirror_id,
            name=request.name,
            raw_prefix=request.raw_prefix,
            clone_prefix=request.clone_prefix,
            enabled=request.enabled,
            priority=request.priority,
        )

        if not mirror:
            raise HTTPException(status_code=404, detail="未找到指定镜像源")

        return MirrorConfigResponse(
            id=mirror["id"],
            name=mirror["name"],
            raw_prefix=mirror["raw_prefix"],
            clone_prefix=mirror["clone_prefix"],
            enabled=mirror["enabled"],
            priority=mirror["priority"],
        )
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="镜像源配置无效") from None
    except Exception as e:
        logger.error("更新镜像源失败", error_type=type(e).__name__)
        raise HTTPException(status_code=500, detail="更新镜像源失败") from None


@router.delete("/mirrors/{mirror_id}")
async def delete_mirror(
    mirror_id: str, maibot_session: Optional[str] = Cookie(None), authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    """
    删除镜像源
    """
    # Token 验证
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    token_manager = get_token_manager()
    if not token or not token_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    service = get_git_mirror_service()
    config = service.get_mirror_config()

    success = config.delete_mirror(mirror_id)

    if not success:
        raise HTTPException(status_code=404, detail="未找到指定镜像源")

    return {"success": True, "message": "镜像源已删除"}


@router.post("/fetch-raw", response_model=FetchRawFileResponse)
async def fetch_raw_file(
    request: FetchRawFileRequest,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> FetchRawFileResponse:
    """
    获取 GitHub 仓库的 Raw 文件内容

    支持多镜像源自动切换和错误重试

    需要认证才能访问，防止被滥用作为 SSRF 跳板
    """
    # Token 验证（强制）
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    token_manager = get_token_manager()
    if not token or not token_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    logger.info(
        "收到获取 Raw 文件请求",
        repository_hash=hash_id(f"{request.owner}/{request.repo}"),
        branch_hash=hash_id(request.branch),
        file_path_hash=hash_id(request.file_path),
    )

    try:
        # 发送开始加载进度
        await update_progress(
            stage="loading",
            progress=10,
            message="正在获取插件列表",
            total_plugins=0,
            loaded_plugins=0,
        )

        service = get_git_mirror_service()

        # git_mirror_service 会自动推送 30%-70% 的详细镜像源尝试进度
        result = await service.fetch_raw_file(
            owner=request.owner,
            repo=request.repo,
            branch=request.branch,
            file_path=request.file_path,
            mirror_id=request.mirror_id,
            custom_url=request.custom_url,
        )

        if result.get("success"):
            total = _count_raw_plugin_entries(result.get("data"))

            # 更新进度：成功获取
            await update_progress(
                stage="loading", progress=70, message="正在解析插件数据...", total_plugins=0, loaded_plugins=0
            )

            # 发送成功状态
            await update_progress(
                stage="success",
                progress=100,
                message=f"成功加载 {total} 个插件" if total else "加载完成",
                total_plugins=total,
                loaded_plugins=total,
            )

        return FetchRawFileResponse(**result)

    except HTTPException as e:
        safe_detail = (
            e.detail
            if isinstance(e.detail, str) and e.detail in {"Raw 文件过大", "插件列表条目过多", "Raw 文件服务响应无效"}
            else "获取 Raw 文件失败"
        )
        await _report_raw_fetch_failure(safe_detail)
        raise HTTPException(status_code=e.status_code, detail=safe_detail) from None
    except Exception as e:
        logger.error("获取 Raw 文件失败", error_type=type(e).__name__)
        await _report_raw_fetch_failure("获取 Raw 文件失败")
        raise HTTPException(status_code=500, detail="获取 Raw 文件失败") from None


@router.post("/clone", response_model=CloneRepositoryResponse)
async def clone_repository(
    request: CloneRepositoryRequest,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> CloneRepositoryResponse:
    """
    克隆 GitHub 仓库到本地

    支持多镜像源自动切换和错误重试
    """
    # Token 验证
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    token_manager = get_token_manager()
    if not token or not token_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    logger.info(
        "收到克隆仓库请求",
        repository_hash=hash_id(f"{request.owner}/{request.repo}"),
        target_path_hash=hash_id(request.target_path),
    )

    try:
        # 验证 target_path 的安全性，防止路径遍历攻击
        base_plugin_path = Path("./plugins").resolve()
        base_plugin_path.mkdir(exist_ok=True)
        target_path = validate_safe_path(request.target_path, base_plugin_path)

        service = get_git_mirror_service()
        result = await service.clone_repository(
            owner=request.owner,
            repo=request.repo,
            target_path=target_path,
            branch=request.branch,
            mirror_id=request.mirror_id,
            custom_url=request.custom_url,
            depth=request.depth,
        )

        return CloneRepositoryResponse(**result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("克隆仓库失败", error_type=type(e).__name__)
        raise HTTPException(status_code=500, detail="克隆仓库失败") from None


@router.post("/install")
async def install_plugin(
    request: InstallPluginRequest,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """
    安装插件

    从 Git 仓库克隆插件到本地插件目录
    """
    # Token 验证
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    token_manager = get_token_manager()
    if not token or not token_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    plugin_id = validate_plugin_id(request.plugin_id)
    logger.info("收到安装插件请求", plugin_id=plugin_id)
    staging_root: Optional[Path] = None

    try:
        await update_progress(
            stage="loading",
            progress=5,
            message=f"开始安装插件: {plugin_id}",
            operation="install",
            plugin_id=plugin_id,
        )

        try:
            owner, repo, is_github, repo_url = parse_repository_url(request.repository_url)
        except ValueError:
            raise HTTPException(status_code=400, detail="仓库 URL 不安全或无效") from None

        await update_progress(
            stage="loading",
            progress=10,
            message=f"解析仓库信息: {owner}/{repo}",
            operation="install",
            plugin_id=plugin_id,
        )

        plugins_dir = _plugins_directory(create=True)
        if plugins_dir is None:
            raise RuntimeError("插件目录创建失败")
        folder_name = plugin_id.replace(".", "_")
        target_path = plugins_dir / folder_name

        if _lifecycle_plugin_path(plugin_id, plugins_dir) is not None:
            await update_progress(
                stage="error",
                progress=0,
                message="插件已存在",
                operation="install",
                plugin_id=plugin_id,
                error="插件已安装，请先卸载",
            )
            raise HTTPException(status_code=400, detail="插件已安装")

        await update_progress(
            stage="loading",
            progress=15,
            message="正在准备下载插件文件...",
            operation="install",
            plugin_id=plugin_id,
        )

        staging_root = Path(tempfile.mkdtemp(prefix=".plugin-install-", dir=plugins_dir))
        staged_path = staging_root / "candidate"
        service = get_git_mirror_service()

        if is_github:
            result = await service.clone_repository(
                owner=owner,
                repo=repo,
                target_path=staged_path,
                branch=request.branch,
                mirror_id=request.mirror_id,
                depth=1,
            )
        else:
            result = await service.clone_repository(
                owner=owner,
                repo=repo,
                target_path=staged_path,
                branch=request.branch,
                custom_url=repo_url,
                depth=1,
            )

        if not result.get("success"):
            await update_progress(
                stage="error",
                progress=0,
                message="克隆仓库失败",
                operation="install",
                plugin_id=plugin_id,
                error="插件文件下载失败",
            )
            raise HTTPException(status_code=500, detail="插件安装失败")

        await update_progress(
            stage="loading", progress=85, message="验证插件文件...", operation="install", plugin_id=plugin_id
        )

        staged_identity = _staged_plugin_identity(staged_path, staging_root)
        try:
            manifest = _prepare_plugin_manifest(staged_path, plugin_id)
        except HTTPException:
            await update_progress(
                stage="error",
                progress=0,
                message="插件清单校验失败",
                operation="install",
                plugin_id=plugin_id,
                error="无效的插件格式",
            )
            raise
        source_commit = _clone_result_commit(result)

        if _lifecycle_plugin_path(plugin_id, plugins_dir) is not None:
            raise HTTPException(status_code=409, detail="插件目录已存在")
        _install_staged_plugin(staged_path, target_path, staged_identity)
        try:
            PluginInstallationStore().save(
                _git_installation_source(
                    plugin_id=plugin_id,
                    repository_url=repo_url,
                    source_ref=request.branch,
                    source_commit=source_commit,
                    installed_version=manifest["version"],
                )
            )
        except Exception:
            try:
                _remove_plugin_tree(target_path)
            except Exception as rollback_error:
                logger.critical("Git 插件安装回滚失败", error_type=type(rollback_error).__name__)
            raise

        await update_progress(
            stage="success",
            progress=100,
            message=f"成功安装插件: {manifest['name']} v{manifest['version']}",
            operation="install",
            plugin_id=plugin_id,
        )

        return {
            "success": True,
            "message": "插件安装成功",
            "plugin_id": plugin_id,
            "plugin_name": manifest["name"],
            "version": manifest["version"],
            "path": str(Path("plugins") / folder_name),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("安装插件失败", error_type=type(e).__name__)

        await update_progress(
            stage="error",
            progress=0,
            message="安装失败",
            operation="install",
            plugin_id=plugin_id,
            error="插件安装失败",
        )

        raise HTTPException(status_code=500, detail="插件安装失败") from e
    finally:
        _cleanup_staging_root(staging_root)


@router.post("/uninstall")
async def uninstall_plugin(
    request: UninstallPluginRequest,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """
    卸载插件

    删除插件目录及其所有文件
    """
    # Token 验证
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    token_manager = get_token_manager()
    if not token or not token_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    plugin_id = validate_plugin_id(request.plugin_id)
    logger.info("收到卸载插件请求", plugin_id=plugin_id)

    try:
        await update_progress(
            stage="loading",
            progress=10,
            message=f"开始卸载插件: {plugin_id}",
            operation="uninstall",
            plugin_id=plugin_id,
        )

        plugins_dir = _plugins_directory()
        plugin_path = _lifecycle_plugin_path(plugin_id, plugins_dir) if plugins_dir is not None else None
        if plugin_path is None or plugins_dir is None:
            await update_progress(
                stage="error",
                progress=0,
                message="插件不存在",
                operation="uninstall",
                plugin_id=plugin_id,
                error="插件未安装或已被删除",
            )
            raise HTTPException(status_code=404, detail="插件未安装")

        await update_progress(
            stage="loading",
            progress=30,
            message="正在准备删除插件文件...",
            operation="uninstall",
            plugin_id=plugin_id,
        )

        plugin_name, _old_version = _existing_plugin_metadata(plugin_path, plugin_id)

        await update_progress(
            stage="loading",
            progress=50,
            message=f"正在删除 {plugin_name}...",
            operation="uninstall",
            plugin_id=plugin_id,
        )

        plugin_identity = _directory_identity(plugin_path)
        trash_path = _stage_plugin_uninstall(plugin_path, plugins_dir, plugin_identity)
        installation_store = PluginInstallationStore()
        installation_source = installation_store.get(plugin_id)
        source_deleted = False
        try:
            source_deleted = bool(installation_store.delete(plugin_id))
            _remove_plugin_tree(trash_path)
        except BaseException:
            if source_deleted and installation_source is not None:
                try:
                    installation_store.save(installation_source)
                except Exception as restore_source_error:
                    logger.critical("插件卸载来源恢复失败", error_type=type(restore_source_error).__name__)
            try:
                if os.path.lexists(trash_path):
                    _restore_plugin_uninstall(plugin_path, trash_path)
            except Exception as rollback_error:
                logger.critical("插件卸载来源回滚失败", error_type=type(rollback_error).__name__)
            raise

        logger.info("成功卸载插件", plugin_id=plugin_id)

        await update_progress(
            stage="success",
            progress=100,
            message=f"成功卸载插件: {plugin_name}",
            operation="uninstall",
            plugin_id=plugin_id,
        )

        return {"success": True, "message": "插件卸载成功", "plugin_id": plugin_id, "plugin_name": plugin_name}

    except HTTPException:
        raise
    except PermissionError as e:
        logger.error("卸载插件失败（权限错误）", error_type=type(e).__name__)

        await update_progress(
            stage="error",
            progress=0,
            message="卸载失败",
            operation="uninstall",
            plugin_id=plugin_id,
            error="权限不足，无法删除插件文件",
        )

        raise HTTPException(status_code=500, detail="权限不足，无法删除插件文件") from e
    except Exception as e:
        logger.error("卸载插件失败", error_type=type(e).__name__)

        await update_progress(
            stage="error",
            progress=0,
            message="卸载失败",
            operation="uninstall",
            plugin_id=plugin_id,
            error="插件卸载失败",
        )

        raise HTTPException(status_code=500, detail="插件卸载失败") from e


@router.post("/update")
async def update_plugin(
    request: UpdatePluginRequest,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """
    更新插件

    删除旧版本，重新克隆新版本
    """
    # Token 验证
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    token_manager = get_token_manager()
    if not token or not token_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    plugin_id = validate_plugin_id(request.plugin_id)
    logger.info("收到更新插件请求", plugin_id=plugin_id)
    staging_root: Optional[Path] = None

    try:
        store = PluginInstallationStore()
        existing_source = store.get(plugin_id)
        if existing_source is not None and existing_source.install_method == "market":
            raise HTTPException(status_code=409, detail="市场插件必须通过市场版本接口更新")

        try:
            owner, repo, is_github, repo_url = parse_repository_url(request.repository_url)
        except ValueError:
            raise HTTPException(status_code=400, detail="仓库 URL 不安全或无效") from None

        # 推送进度：开始更新
        await update_progress(
            stage="loading",
            progress=5,
            message=f"开始更新插件: {plugin_id}",
            operation="update",
            plugin_id=plugin_id,
        )

        plugins_dir = _plugins_directory()
        plugin_path = _lifecycle_plugin_path(plugin_id, plugins_dir) if plugins_dir is not None else None
        if plugin_path is None or plugins_dir is None:
            await update_progress(
                stage="error",
                progress=0,
                message="插件不存在",
                operation="update",
                plugin_id=plugin_id,
                error="插件未安装，请先安装",
            )
            raise HTTPException(status_code=404, detail="插件未安装")

        plugin_identity = _directory_identity(plugin_path)
        _old_name, old_version = _existing_plugin_metadata(plugin_path, plugin_id)

        await update_progress(
            stage="loading",
            progress=10,
            message=f"当前版本: {old_version}，准备更新...",
            operation="update",
            plugin_id=plugin_id,
        )

        await update_progress(
            stage="loading", progress=20, message="正在下载并验证新版本...", operation="update", plugin_id=plugin_id
        )

        staging_root = Path(tempfile.mkdtemp(prefix=".plugin-update-", dir=plugins_dir))
        staged_path = staging_root / "candidate"
        service = get_git_mirror_service()

        if is_github:
            result = await service.clone_repository(
                owner=owner,
                repo=repo,
                target_path=staged_path,
                branch=request.branch,
                mirror_id=request.mirror_id,
                depth=1,
            )
        else:
            result = await service.clone_repository(
                owner=owner,
                repo=repo,
                target_path=staged_path,
                branch=request.branch,
                custom_url=repo_url,
                depth=1,
            )

        if not result.get("success"):
            await update_progress(
                stage="error",
                progress=0,
                message="下载新版本失败",
                operation="update",
                plugin_id=plugin_id,
                error="插件新版本下载失败",
            )
            raise HTTPException(status_code=500, detail="插件更新失败")

        await update_progress(
            stage="loading", progress=90, message="验证新版本...", operation="update", plugin_id=plugin_id
        )

        staged_identity = _staged_plugin_identity(staged_path, staging_root)
        try:
            new_manifest = _prepare_plugin_manifest(staged_path, plugin_id)
        except HTTPException:
            await update_progress(
                stage="error",
                progress=0,
                message="新版本插件清单校验失败",
                operation="update",
                plugin_id=plugin_id,
                error="无效的插件格式",
            )
            raise

        source_commit = _clone_result_commit(result)
        new_version = new_manifest["version"]
        new_name = new_manifest["name"]
        backup_path, runtime_entries = _activate_plugin_replacement(
            plugin_path,
            staged_path,
            plugins_dir,
            plugin_identity,
            staged_identity,
            _plugin_runtime_config_files(plugin_id),
        )
        try:
            store.save(
                _git_installation_source(
                    plugin_id=plugin_id,
                    repository_url=repo_url,
                    source_ref=request.branch,
                    source_commit=source_commit,
                    installed_version=new_version,
                    installed_at=existing_source.installed_at if existing_source is not None else None,
                )
            )
        except Exception:
            try:
                _restore_plugin_replacement(plugin_path, backup_path, plugins_dir, runtime_entries)
            except Exception as rollback_error:
                logger.critical("Git 插件更新回滚失败", error_type=type(rollback_error).__name__)
            raise
        _finalize_plugin_replacement(backup_path)

        logger.info("成功更新插件", plugin_id=plugin_id)
        await update_progress(
            stage="success",
            progress=100,
            message=f"成功更新 {new_name}: {old_version} → {new_version}",
            operation="update",
            plugin_id=plugin_id,
        )

        return {
            "success": True,
            "message": "插件更新成功",
            "plugin_id": plugin_id,
            "plugin_name": new_name,
            "old_version": old_version,
            "new_version": new_version,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("更新插件失败", error_type=type(e).__name__)

        await update_progress(
            stage="error",
            progress=0,
            message="更新失败",
            operation="update",
            plugin_id=plugin_id,
            error="插件更新失败",
        )

        raise HTTPException(status_code=500, detail="插件更新失败") from e
    finally:
        _cleanup_staging_root(staging_root)


@router.get("/installed")
async def get_installed_plugins(
    maibot_session: Optional[str] = Cookie(None), authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    """
    获取已安装的插件列表

    扫描 plugins 目录，返回所有已安装插件的 ID 和基本信息
    """
    # Token 验证
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    token_manager = get_token_manager()
    if not token or not token_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    logger.info("收到获取已安装插件列表请求")

    try:
        plugins_dir = _plugins_directory()
        if plugins_dir is None:
            logger.info("插件目录不存在，创建目录")
            _plugins_directory(create=True)
            return {"success": True, "plugins": []}

        installed_plugins = []
        installation_sources = PluginInstallationStore().list_all()

        for plugin_entry in sorted(plugins_dir.iterdir(), key=lambda path: path.name):
            if plugin_entry.is_symlink():
                continue
            try:
                plugin_path = plugin_entry.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not plugin_path.is_relative_to(plugins_dir) or not plugin_path.is_dir():
                continue

            folder_name = plugin_entry.name
            if folder_name.startswith(".") or folder_name.startswith("__"):
                continue

            try:
                validate_plugin_id(folder_name)
                manifest = _load_plugin_manifest(plugin_path)
                if manifest is None:
                    logger.warning("插件文件夹缺少清单，已跳过")
                    continue
                if not isinstance(manifest.get("name"), str) or not isinstance(manifest.get("version"), str):
                    logger.warning("插件清单缺少有效的名称或版本，已跳过")
                    continue

                if "id" in manifest:
                    plugin_id = validate_plugin_id(manifest["id"])
                else:
                    author_name = None
                    repo_name = None

                    author = manifest.get("author")
                    if isinstance(author, dict) and isinstance(author.get("name"), str):
                        author_name = author["name"]
                    elif isinstance(author, str):
                        author_name = author

                    repository_url = manifest.get("repository_url")
                    if isinstance(repository_url, str):
                        repo_url = repository_url.rstrip("/")
                        if repo_url.endswith(".git"):
                            repo_url = repo_url[:-4]
                        repo_name = repo_url.split("/")[-1]

                    if author_name and repo_name:
                        plugin_id = f"{author_name}.{repo_name}"
                    elif author_name:
                        plugin_id = f"{author_name}.{folder_name}"
                    elif "_" in folder_name and "." not in folder_name:
                        plugin_id = folder_name.replace("_", ".", 1)
                    else:
                        plugin_id = folder_name

                    plugin_id = validate_plugin_id(plugin_id)
                    logger.info("已为缺少 ID 的插件清单生成安全 ID")
                    manifest["id"] = plugin_id
                    try:
                        manifest_content = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
                        manifest_path = _plugin_file_path(plugin_path, "_manifest.json")
                        _atomic_write_bytes(
                            manifest_path,
                            manifest_content,
                            MAX_PLUGIN_MANIFEST_BYTES,
                            "插件清单",
                        )
                    except HTTPException:
                        logger.warning("无法安全写入插件清单 ID")
                    except OSError as e:
                        log_exception_type(logger, "写入插件清单 ID 失败", e, level="warning")

                installation_source = _verified_installation_source(
                    plugin_id,
                    installation_sources.get(plugin_id),
                    plugin_path=plugin_path,
                )
                installation = _market_installation_response(installation_source)
                installed_plugins.append(
                    {
                        "id": plugin_id,
                        "manifest": manifest,
                        "path": str(Path("plugins") / folder_name),
                        "installation": installation.model_dump(mode="json") if installation is not None else None,
                    }
                )
            except HTTPException:
                logger.warning("插件清单未通过安全校验，已跳过")
                continue
            except (OSError, RuntimeError, ValueError, TypeError) as e:
                log_exception_type(logger, "读取插件清单失败，已跳过", e, level="warning")
                continue

        # 去重：如果有重复的 plugin_id，只保留第一个（按路径）
        seen_ids = {}  # 记录 ID -> 路径的映射
        unique_plugins = []
        duplicates = []

        for plugin in installed_plugins:
            plugin_id = plugin["id"]
            plugin_path = plugin["path"]

            if plugin_id not in seen_ids:
                seen_ids[plugin_id] = plugin_path
                unique_plugins.append(plugin)
            else:
                duplicates.append(plugin)
                logger.warning("检测到重复插件，已保留首个目录", plugin_id=plugin_id)

        if duplicates:
            logger.warning("重复插件已去重", duplicate_count=len(duplicates))

        logger.info("已扫描安装插件", plugin_count=len(unique_plugins))

        return {"success": True, "plugins": unique_plugins, "total": len(unique_plugins)}

    except HTTPException:
        raise
    except Exception as e:
        log_exception_type(logger, "获取已安装插件列表失败", e)
        raise HTTPException(status_code=500, detail="服务器错误") from e


@router.get("/local-readme/{plugin_id}")
async def get_local_plugin_readme(
    plugin_id: str, maibot_session: Optional[str] = Cookie(None), authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    """
    获取本地已安装插件的 README 文件内容

    Args:
        plugin_id: 插件 ID

    Returns:
        包含 success 和 data(README 内容) 的字典，如果文件不存在则返回 success=False
    """
    # Token 验证
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    token_manager = get_token_manager()
    if not token or not token_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    plugin_id = validate_plugin_id(plugin_id)
    logger.info("获取本地插件 README", plugin_id=plugin_id)

    try:
        plugin_path = _find_plugin_path(plugin_id)

        if not plugin_path:
            return {"success": False, "error": "插件未安装"}

        # 查找 README 文件（支持多种命名）
        readme_files = ["README.md", "readme.md", "Readme.md", "README.MD"]
        readme_content = None

        for readme_name in readme_files:
            readme_path = _plugin_file_path(plugin_path, readme_name)
            if readme_path.is_file():
                try:
                    readme_content = _read_limited_utf8(readme_path, MAX_PLUGIN_README_BYTES, "插件 README")
                    logger.info("成功读取本地插件 README")
                    break
                except HTTPException:
                    raise
                except Exception:
                    logger.warning("读取本地插件 README 失败")
                    continue

        if readme_content:
            return {"success": True, "data": readme_content}
        else:
            return {"success": False, "error": "本地未找到 README 文件"}

    except HTTPException:
        raise
    except Exception as e:
        log_exception_type(logger, "获取本地 README 失败", e)
        return {"success": False, "error": "读取本地 README 失败"}


# ============ 插件配置管理 API ============


class UpdatePluginConfigRequest(BaseModel):
    """更新插件配置请求"""

    config: Dict[str, Any] | str = Field(..., description="配置数据或原始 TOML 字符串")


@router.get("/config/{plugin_id}/schema")
async def get_plugin_config_schema(
    plugin_id: str, maibot_session: Optional[str] = Cookie(None), authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    """
    获取插件配置 Schema

    返回插件的完整配置 schema，包含所有 section、字段定义和布局信息。
    用于前端动态生成配置表单。
    """
    # Token 验证
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    token_manager = get_token_manager()
    if not token or not token_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    plugin_id = validate_plugin_id(plugin_id)
    logger.info("获取插件配置 Schema", plugin_id=plugin_id)

    try:
        # 尝试从已加载的插件中获取
        from src.plugin_system.core.plugin_manager import plugin_manager

        # 查找插件实例
        plugin_instance = None

        # 遍历所有已加载的插件
        for loaded_plugin_name in plugin_manager.list_loaded_plugins():
            instance = plugin_manager.get_plugin_instance(loaded_plugin_name)
            if instance:
                # 匹配 plugin_name 或 manifest 中的 id
                if instance.plugin_name == plugin_id:
                    plugin_instance = instance
                    break
                # 也尝试匹配 manifest 中的 id
                manifest_id = instance.get_manifest_info("id", "")
                if manifest_id == plugin_id:
                    plugin_instance = instance
                    break

        if plugin_instance and hasattr(plugin_instance, "get_webui_config_schema"):
            # 从插件实例获取 schema
            schema = plugin_instance.get_webui_config_schema()
            return {"success": True, "schema": schema}

        # 如果插件未加载，尝试从文件系统读取。
        plugin_path = _find_plugin_path(plugin_id)

        if not plugin_path:
            raise HTTPException(status_code=404, detail=f"未找到插件: {plugin_id}")

        # 读取配置文件获取当前配置
        config_path = _plugin_file_path(plugin_path, "config.toml")
        current_config = {}
        if config_path.exists():
            import tomlkit

            try:
                current_config = tomlkit.loads(_read_limited_utf8(config_path, MAX_PLUGIN_CONFIG_BYTES, "插件配置"))
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=400, detail="插件配置格式无效") from exc

        # 构建基础 schema（无法获取完整的 ConfigField 信息）
        schema = {
            "plugin_id": plugin_id,
            "plugin_info": {
                "name": plugin_id,
                "version": "",
                "description": "",
                "author": "",
            },
            "sections": {},
            "layout": {"type": "auto", "tabs": []},
            "_note": "插件未加载，仅返回当前配置结构",
        }

        # 从当前配置推断 schema
        for section_name, section_data in current_config.items():
            if isinstance(section_data, dict):
                schema["sections"][section_name] = {
                    "name": section_name,
                    "title": section_name,
                    "description": None,
                    "icon": None,
                    "collapsed": False,
                    "order": 0,
                    "fields": {},
                }
                for field_name, field_value in section_data.items():
                    # 推断字段类型
                    field_type = type(field_value).__name__
                    ui_type = "text"
                    item_type = None
                    item_fields = None

                    if isinstance(field_value, bool):
                        ui_type = "switch"
                    elif isinstance(field_value, (int, float)):
                        ui_type = "number"
                    elif isinstance(field_value, list):
                        ui_type = "list"
                        # 推断数组元素类型
                        if field_value:
                            first_item = field_value[0]
                            if isinstance(first_item, dict):
                                item_type = "object"
                                # 从第一个元素推断字段结构
                                item_fields = {}
                                for k, v in first_item.items():
                                    item_fields[k] = {
                                        "type": "number" if isinstance(v, (int, float)) else "string",
                                        "label": k,
                                        "default": "" if isinstance(v, str) else 0,
                                    }
                            elif isinstance(first_item, (int, float)):
                                item_type = "number"
                            else:
                                item_type = "string"
                        else:
                            item_type = "string"
                    elif isinstance(field_value, dict):
                        ui_type = "json"

                    schema["sections"][section_name]["fields"][field_name] = {
                        "name": field_name,
                        "type": field_type,
                        "default": field_value,
                        "description": field_name,
                        "label": field_name,
                        "ui_type": ui_type,
                        "required": False,
                        "hidden": False,
                        "disabled": False,
                        "order": 0,
                        "item_type": item_type,
                        "item_fields": item_fields,
                        "min_items": None,
                        "max_items": None,
                        # 补充缺失的字段
                        "placeholder": None,
                        "hint": None,
                        "icon": None,
                        "example": None,
                        "choices": None,
                        "min": None,
                        "max": None,
                        "step": None,
                        "pattern": None,
                        "max_length": None,
                        "input_type": None,
                        "rows": 3,
                        "group": None,
                        "depends_on": None,
                        "depends_value": None,
                    }

        return {"success": True, "schema": schema}

    except HTTPException:
        raise
    except Exception as e:
        log_exception_type(logger, "获取插件配置 Schema 失败", e)
        raise HTTPException(status_code=500, detail="服务器错误") from e


@router.get("/config/{plugin_id}/raw")
async def get_plugin_config_raw(
    plugin_id: str,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """
    获取插件原始 TOML 配置文件内容
    """
    # Token 验证
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    token_manager = get_token_manager()
    if not token or not token_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    plugin_id = validate_plugin_id(plugin_id)
    logger.info("获取插件原始配置", plugin_id=plugin_id)

    try:
        plugin_path = _find_plugin_path(plugin_id)

        if not plugin_path:
            raise HTTPException(status_code=404, detail=f"未找到插件: {plugin_id}")

        # 读取配置文件
        config_path = _plugin_file_path(plugin_path, "config.toml")
        if not config_path.exists():
            return {"success": True, "config": "", "message": "配置文件不存在"}
        if not config_path.is_file():
            raise HTTPException(status_code=400, detail="插件配置路径不是普通文件")

        config_content = _read_limited_utf8(config_path, MAX_PLUGIN_CONFIG_BYTES, "插件配置")

        return {"success": True, "config": config_content}

    except HTTPException:
        raise
    except Exception as e:
        log_exception_type(logger, "获取插件原始配置失败", e)
        raise HTTPException(status_code=500, detail="服务器错误") from e


@router.put("/config/{plugin_id}/raw")
async def update_plugin_config_raw(
    plugin_id: str,
    request: UpdatePluginConfigRequest,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """
    更新插件原始 TOML 配置文件

    直接保存 TOML 字符串到配置文件。
    """
    # Token 验证
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    token_manager = get_token_manager()
    if not token or not token_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    plugin_id = validate_plugin_id(plugin_id)
    logger.info("更新插件原始配置", plugin_id=plugin_id)

    try:
        plugin_path = _find_plugin_path(plugin_id)

        if not plugin_path:
            raise HTTPException(status_code=404, detail=f"未找到插件: {plugin_id}")

        config_path = _plugin_file_path(plugin_path, "config.toml")

        # 验证 TOML 格式
        import tomlkit

        if not isinstance(request.config, str):
            raise HTTPException(status_code=400, detail="配置必须是字符串格式的 TOML 内容")
        encoded_config = request.config.encode("utf-8")
        if len(encoded_config) > MAX_PLUGIN_CONFIG_BYTES:
            raise HTTPException(status_code=413, detail="插件配置过大")

        try:
            tomlkit.loads(request.config)
        except Exception as e:
            raise HTTPException(status_code=400, detail="TOML 格式错误，请检查配置语法") from e

        if config_path.exists():
            _create_config_backup(plugin_path, config_path, "backup")
            logger.info("已备份插件配置文件")

        _atomic_write_bytes(config_path, encoded_config, MAX_PLUGIN_CONFIG_BYTES, "插件配置")

        logger.info("已更新插件原始配置", plugin_id=plugin_id)

        return {"success": True, "message": "配置已保存", "note": "配置更改将在插件重新加载后生效"}

    except HTTPException:
        raise
    except Exception as e:
        log_exception_type(logger, "更新插件原始配置失败", e)
        raise HTTPException(status_code=500, detail="服务器错误") from e


@router.get("/config/{plugin_id}")
async def get_plugin_config(
    plugin_id: str, maibot_session: Optional[str] = Cookie(None), authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    """
    获取插件当前配置值

    返回插件的当前配置值。
    """
    # Token 验证
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    token_manager = get_token_manager()
    if not token or not token_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    plugin_id = validate_plugin_id(plugin_id)
    logger.info("获取插件配置", plugin_id=plugin_id)

    try:
        plugin_path = _find_plugin_path(plugin_id)
        if not plugin_path:
            raise HTTPException(status_code=404, detail=f"未找到插件: {plugin_id}")

        config_path = _plugin_file_path(plugin_path, "config.toml")
        if not config_path.exists():
            return {"success": True, "config": {}, "message": "配置文件不存在"}

        import tomlkit

        config_content = _read_limited_utf8(config_path, MAX_PLUGIN_CONFIG_BYTES, "插件配置")
        try:
            config = tomlkit.loads(config_content)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="插件配置格式无效") from exc

        return {"success": True, "config": dict(config)}

    except HTTPException:
        raise
    except Exception as e:
        log_exception_type(logger, "获取插件配置失败", e)
        raise HTTPException(status_code=500, detail="服务器错误") from e


@router.put("/config/{plugin_id}")
async def update_plugin_config(
    plugin_id: str,
    request: UpdatePluginConfigRequest,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """
    更新插件配置

    保存新的配置值到插件的配置文件。
    """
    # Token 验证
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    token_manager = get_token_manager()
    if not token or not token_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    plugin_id = validate_plugin_id(plugin_id)
    logger.info("更新插件配置", plugin_id=plugin_id)

    try:
        if not isinstance(request.config, dict):
            raise HTTPException(status_code=400, detail="配置必须是对象格式")

        plugin_instance = find_plugin_instance(plugin_id)

        # 纠正 WebUI 提交的数据结构（扁平键与字符串列表）
        if plugin_instance:
            request.config = normalize_dotted_keys(request.config)
            if isinstance(plugin_instance.config_schema, dict):
                coerce_types(plugin_instance.config_schema, request.config)

        plugin_path = _find_plugin_path(plugin_id)
        if not plugin_path:
            raise HTTPException(status_code=404, detail=f"未找到插件: {plugin_id}")

        config_path = _plugin_file_path(plugin_path, "config.toml")
        rendered_config = _render_plugin_toml(config_path, request.config)

        if config_path.exists():
            _create_config_backup(plugin_path, config_path, "backup")
            logger.info("已备份插件配置文件")

        _atomic_write_bytes(config_path, rendered_config, MAX_PLUGIN_CONFIG_BYTES, "插件配置")

        logger.info("已更新插件配置", plugin_id=plugin_id)

        return {"success": True, "message": "配置已保存", "note": "配置更改将在插件重新加载后生效"}

    except HTTPException:
        raise
    except Exception as e:
        log_exception_type(logger, "更新插件配置失败", e)
        raise HTTPException(status_code=500, detail="服务器错误") from e


@router.post("/config/{plugin_id}/reset")
async def reset_plugin_config(
    plugin_id: str, maibot_session: Optional[str] = Cookie(None), authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    """
    重置插件配置为默认值

    删除当前配置文件，下次加载插件时将使用默认配置。
    """
    # Token 验证
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    token_manager = get_token_manager()
    if not token or not token_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    plugin_id = validate_plugin_id(plugin_id)
    logger.info("重置插件配置", plugin_id=plugin_id)

    try:
        plugin_path = _find_plugin_path(plugin_id)
        if not plugin_path:
            raise HTTPException(status_code=404, detail=f"未找到插件: {plugin_id}")

        config_path = _plugin_file_path(plugin_path, "config.toml")

        if not config_path.exists():
            return {"success": True, "message": "配置文件不存在，无需重置"}

        backup_path = _move_config_to_reset_backup(plugin_path, config_path)

        logger.info("已重置插件配置", plugin_id=plugin_id)

        return {
            "success": True,
            "message": "配置已重置，下次加载插件时将使用默认配置",
            "backup": backup_path.name,
        }

    except HTTPException:
        raise
    except Exception as e:
        log_exception_type(logger, "重置插件配置失败", e)
        raise HTTPException(status_code=500, detail="服务器错误") from e


@router.post("/config/{plugin_id}/toggle")
async def toggle_plugin(
    plugin_id: str, maibot_session: Optional[str] = Cookie(None), authorization: Optional[str] = Header(None)
) -> Dict[str, Any]:
    """
    切换插件启用状态

    切换插件配置中的 enabled 字段。
    """
    # Token 验证
    token = get_token_from_cookie_or_header(maibot_session, authorization)
    token_manager = get_token_manager()
    if not token or not token_manager.verify_token(token):
        raise HTTPException(status_code=401, detail="未授权：无效的访问令牌")

    plugin_id = validate_plugin_id(plugin_id)
    logger.info("切换插件状态", plugin_id=plugin_id)

    try:
        plugin_path = _find_plugin_path(plugin_id)
        if not plugin_path:
            raise HTTPException(status_code=404, detail=f"未找到插件: {plugin_id}")

        config_path = _plugin_file_path(plugin_path, "config.toml")

        import tomlkit

        config = tomlkit.document()
        if config_path.exists():
            config_content = _read_limited_utf8(config_path, MAX_PLUGIN_CONFIG_BYTES, "插件配置")
            try:
                config = tomlkit.loads(config_content)
            except Exception as exc:
                raise HTTPException(status_code=400, detail="插件配置格式无效") from exc

        if "plugin" not in config:
            config["plugin"] = tomlkit.table()
        elif not hasattr(config["plugin"], "get"):
            raise HTTPException(status_code=400, detail="插件配置格式无效")

        current_enabled = config["plugin"].get("enabled", True)
        if not isinstance(current_enabled, bool):
            raise HTTPException(status_code=400, detail="插件启用状态格式无效")
        new_enabled = not current_enabled
        config["plugin"]["enabled"] = new_enabled

        rendered_config = _render_plugin_toml(config_path, config)
        _atomic_write_bytes(config_path, rendered_config, MAX_PLUGIN_CONFIG_BYTES, "插件配置")

        status = "启用" if new_enabled else "禁用"
        logger.info("已切换插件状态", plugin_id=plugin_id, enabled=new_enabled)

        return {
            "success": True,
            "enabled": new_enabled,
            "message": f"插件已{status}",
            "note": "状态更改将在下次加载插件时生效",
        }

    except HTTPException:
        raise
    except Exception as e:
        log_exception_type(logger, "切换插件状态失败", e)
        raise HTTPException(status_code=500, detail="服务器错误") from e
