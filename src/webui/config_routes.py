"""
配置管理API路由
"""

import copy
from dataclasses import fields
import errno
import importlib
import ipaddress
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Annotated, Optional

import tomlkit
from fastapi import APIRouter, Body, Cookie, Depends, Header, HTTPException

from src.common.logger import get_logger, redact_secret
from src.webui.auth import verify_auth_token_from_cookie_or_header
from src.common.toml_utils import _update_toml_doc, format_toml_string
from src.config.config import Config, APIAdapterConfig, CONFIG_DIR, PROJECT_ROOT
from src.config.official_configs import (
    BotConfig,
    PersonalityConfig,
    RelationshipConfig,
    ChatConfig,
    MessageReceiveConfig,
    EmojiConfig,
    ExpressionConfig,
    BehaviorConfig,
    KeywordReactionConfig,
    ChineseTypoConfig,
    ResponsePostProcessConfig,
    ResponseSplitterConfig,
    TelemetryConfig,
    LogConfig,
    WebUIConfig,
    ExperimentalConfig,
    MaimMessageConfig,
    LPMMKnowledgeConfig,
    ToolConfig,
    MemoryConfig,
    DebugConfig,
    VoiceConfig,
    UpdateConfig,
)
from src.config.api_ada_configs import (
    ModelTaskConfig,
    ModelInfo,
    APIProvider,
)
from src.webui.config_schema import ConfigSchemaGenerator
from src.webui.token_manager import get_token_manager

logger = get_logger("webui")

# 模块级别的类型别名（解决 B008 ruff 错误）
ConfigBody = Annotated[dict[str, Any], Body()]
SectionBody = Annotated[Any, Body()]
RawContentBody = Annotated[str, Body(embed=True)]
PathBody = Annotated[dict[str, str], Body()]

router = APIRouter(prefix="/config", tags=["config"])

BOT_SECTION_SCHEMAS = {
    "bot": BotConfig,
    "personality": PersonalityConfig,
    "relationship": RelationshipConfig,
    "chat": ChatConfig,
    "message_receive": MessageReceiveConfig,
    "emoji": EmojiConfig,
    "expression": ExpressionConfig,
    "behavior": BehaviorConfig,
    "keyword_reaction": KeywordReactionConfig,
    "chinese_typo": ChineseTypoConfig,
    "response_post_process": ResponsePostProcessConfig,
    "response_splitter": ResponseSplitterConfig,
    "telemetry": TelemetryConfig,
    "log": LogConfig,
    "webui": WebUIConfig,
    "experimental": ExperimentalConfig,
    "maim_message": MaimMessageConfig,
    "lpmm_knowledge": LPMMKnowledgeConfig,
    "tool": ToolConfig,
    "memory": MemoryConfig,
    "debug": DebugConfig,
    "voice": VoiceConfig,
    "update": UpdateConfig,
}

LEGACY_BOT_SECTIONS = {"dream", "jargon", "mood"}
MAX_CONFIG_FILE_BYTES = 2 * 1024 * 1024
MAX_ADAPTER_CONFIG_BYTES = 1024 * 1024
_MAX_ADAPTER_PATH_LENGTH = 4096
_TRUE_VALUES = {"1", "true", "yes"}
_CONFIG_VALIDATION_DETAIL = "配置数据验证失败，请检查字段和值"
_CONFIG_FILENAMES = {"bot_config.toml", "model_config.toml"}
_MISSING_CONFIG_FILE = object()
_MISSING_ADAPTER_CONFIG = object()
_ONEBOT_ADAPTER_ID = "onebot_default"
_ONEBOT_MANAGED_DEFAULTS: dict[str, dict[str, Any]] = {
    "napcat_server": {
        "host": "localhost",
        "port": 8095,
        "token": "",
        "heartbeat_interval": 30,
    },
    "chat": {
        "group_list_type": "blacklist",
        "group_list": [],
        "private_list_type": "blacklist",
        "private_list": [],
        "ban_user_id": [],
        "ban_qq_bot": True,
        "enable_poke": True,
    },
    "voice": {"use_tts": False},
    "forward": {"image_threshold": 3},
    "debug": {"level": "INFO"},
}


def _log_config_failure(action: str, exc: Exception) -> None:
    """记录可诊断但不包含异常文本的配置错误。"""
    logger.error(action, error_type=type(exc).__name__)


def _config_file_path(filename: str, *, create_directory: bool = False) -> Path:
    """返回固定配置文件路径，并拒绝符号链接配置目录。"""
    if filename not in _CONFIG_FILENAMES:
        raise ValueError("配置文件名无效")

    config_directory = Path(CONFIG_DIR)
    try:
        if config_directory.is_symlink():
            raise HTTPException(status_code=400, detail="配置目录路径无效")
        if not config_directory.exists():
            if not create_directory:
                return config_directory / filename
            config_directory.mkdir(parents=True, exist_ok=True)
        if not config_directory.is_dir():
            raise HTTPException(status_code=400, detail="配置目录路径无效")
    except HTTPException:
        raise
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail="配置目录路径无效") from exc
    return config_directory / filename


def _config_fingerprint(path: Path) -> tuple[int, int, int, int, int] | object:
    """读取不跟随链接的文件身份，用于发现并发替换。"""
    try:
        file_stat = os.lstat(path)
    except FileNotFoundError:
        return _MISSING_CONFIG_FILE
    except OSError as exc:
        raise HTTPException(status_code=400, detail="配置文件路径无效") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise HTTPException(status_code=400, detail="配置文件路径无效")
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _read_config_text(
    filename: str,
    *,
    required: bool = True,
) -> tuple[Optional[str], tuple[int, int, int, int, int] | object]:
    """通过 O_NOFOLLOW 有限读取配置，并严格按 UTF-8 解码。"""
    path = _config_file_path(filename)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        if path.is_symlink():
            raise HTTPException(status_code=400, detail="配置文件路径无效")
    except OSError as exc:
        raise HTTPException(status_code=400, detail="配置文件路径无效") from exc

    try:
        file_descriptor = os.open(path, flags)
    except FileNotFoundError:
        if required:
            raise HTTPException(status_code=404, detail="配置文件不存在") from None
        return None, _MISSING_CONFIG_FILE
    except OSError as exc:
        if path.is_symlink() or exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise HTTPException(status_code=400, detail="配置文件路径无效") from None
        raise

    try:
        file_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise HTTPException(status_code=400, detail="配置文件路径无效")
        fingerprint = (
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_size,
            file_stat.st_mtime_ns,
            file_stat.st_ctime_ns,
        )
        with os.fdopen(file_descriptor, "rb") as file:
            file_descriptor = -1
            raw_content = file.read(MAX_CONFIG_FILE_BYTES + 1)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)

    if len(raw_content) > MAX_CONFIG_FILE_BYTES:
        raise HTTPException(status_code=413, detail="配置文件过大")
    try:
        return raw_content.decode("utf-8"), fingerprint
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="配置文件必须使用 UTF-8 编码") from exc


def _load_config_document(
    filename: str,
    *,
    required: bool = True,
) -> tuple[Optional[Any], tuple[int, int, int, int, int] | object]:
    content, fingerprint = _read_config_text(filename, required=required)
    if content is None:
        return None, fingerprint
    return tomlkit.loads(content), fingerprint


def _fingerprint_matches(
    current: tuple[int, int, int, int, int] | object,
    expected: tuple[int, int, int, int, int] | object,
) -> bool:
    if expected is _MISSING_CONFIG_FILE:
        return current is _MISSING_CONFIG_FILE
    return current == expected


def _fsync_directory(directory: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    directory_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_descriptor)
    except OSError as exc:
        if exc.errno not in {errno.EBADF, errno.EINVAL, errno.ENOTSUP}:
            raise
    finally:
        os.close(directory_descriptor)


def _atomic_write_config(
    filename: str,
    content: bytes,
    expected_fingerprint: tuple[int, int, int, int, int] | object,
) -> None:
    """同目录落盘并原子替换，替换前核对目标文件身份。"""
    if len(content) > MAX_CONFIG_FILE_BYTES:
        raise HTTPException(status_code=413, detail="配置内容过大")

    path = _config_file_path(filename, create_directory=True)
    current_fingerprint = _config_fingerprint(path)
    if not _fingerprint_matches(current_fingerprint, expected_fingerprint):
        raise HTTPException(status_code=409, detail="配置文件已发生变化，请重试")

    mode = 0o600
    if current_fingerprint is not _MISSING_CONFIG_FILE:
        mode = stat.S_IMODE(os.lstat(path).st_mode)

    file_descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(file_descriptor, mode)
        with os.fdopen(file_descriptor, "wb") as file:
            file_descriptor = -1
            file.write(content)
            file.flush()
            os.fsync(file.fileno())

        current_fingerprint = _config_fingerprint(path)
        if not _fingerprint_matches(current_fingerprint, expected_fingerprint):
            raise HTTPException(status_code=409, detail="配置文件已发生变化，请重试")

        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        temp_path.unlink(missing_ok=True)


def _render_config_bytes(config_data: Any) -> bytes:
    rendered = format_toml_string(config_data).encode("utf-8")
    if len(rendered) > MAX_CONFIG_FILE_BYTES:
        raise HTTPException(status_code=413, detail="配置内容过大")
    return rendered


def _save_structured_config(filename: str, config_data: Any, *, prune_bot: bool = False) -> None:
    existing_document, fingerprint = _load_config_document(filename, required=False)
    if existing_document is None:
        document_to_save = config_data
    else:
        if filename == "model_config.toml":
            _restore_masked_model_api_keys(config_data, existing_document)
        _update_toml_doc(existing_document, config_data)
        if prune_bot:
            _prune_legacy_bot_config_keys(existing_document)
        document_to_save = existing_document
    _atomic_write_config(filename, _render_config_bytes(document_to_save), fingerprint)


def read_bot_config_section(section_name: str) -> Any:
    """Read one known bot configuration section through the canonical TOML boundary."""
    if section_name not in BOT_SECTION_SCHEMAS:
        raise HTTPException(status_code=404, detail=f"配置节 '{section_name}' 不存在")
    config_data, _ = _load_config_document("bot_config.toml")
    if section_name not in config_data:
        raise HTTPException(status_code=404, detail=f"配置节 '{section_name}' 不存在")
    return copy.deepcopy(config_data[section_name])


def save_bot_config_section(section_name: str, section_data: Any) -> None:
    """Validate and atomically persist one known bot configuration section."""
    if section_name not in BOT_SECTION_SCHEMAS:
        raise HTTPException(status_code=404, detail=f"配置节 '{section_name}' 不存在")

    config_data, fingerprint = _load_config_document("bot_config.toml")
    if section_name not in config_data:
        raise HTTPException(status_code=404, detail=f"配置节 '{section_name}' 不存在")

    if isinstance(section_data, list):
        config_data[section_name] = section_data
    elif isinstance(section_data, dict) and isinstance(config_data[section_name], dict):
        _update_toml_doc(config_data[section_name], section_data)
    else:
        config_data[section_name] = section_data

    _prune_legacy_bot_config_keys(config_data, section_name)
    try:
        Config.from_dict(config_data)
    except Exception as exc:
        _log_config_failure("璃夜主程序配置节数据验证失败", exc)
        raise HTTPException(status_code=400, detail=_CONFIG_VALIDATION_DETAIL) from exc
    _atomic_write_config("bot_config.toml", _render_config_bytes(config_data), fingerprint)


def _model_api_providers(config_data: Any) -> list[dict[str, Any]]:
    if not isinstance(config_data, dict):
        return []
    providers = config_data.get("api_providers")
    if not isinstance(providers, list):
        return []
    return [provider for provider in providers if isinstance(provider, dict)]


def _mask_model_api_keys(config_data: Any) -> Any:
    masked_config = copy.deepcopy(config_data)
    for provider in _model_api_providers(masked_config):
        api_key = provider.get("api_key")
        if isinstance(api_key, str) and api_key:
            provider["api_key"] = redact_secret(api_key)
    return masked_config


def _restore_masked_model_api_keys(config_data: Any, existing_config: Any) -> None:
    existing_keys = []
    existing_keys_by_name = {}
    for provider in _model_api_providers(existing_config):
        name = provider.get("name")
        api_key = provider.get("api_key")
        if not isinstance(api_key, str) or not api_key:
            continue
        existing_keys.append(api_key)
        if isinstance(name, str):
            existing_keys_by_name[name] = api_key

    for provider in _model_api_providers(config_data):
        submitted_key = provider.get("api_key")
        if not isinstance(submitted_key, str) or not submitted_key:
            continue

        provider_name = provider.get("name")
        named_key = existing_keys_by_name.get(provider_name) if isinstance(provider_name, str) else None
        if named_key is not None and submitted_key == redact_secret(named_key):
            provider["api_key"] = named_key
            continue

        matching_keys = {api_key for api_key in existing_keys if submitted_key == redact_secret(api_key)}
        if len(matching_keys) == 1:
            provider["api_key"] = matching_keys.pop()
        elif len(matching_keys) > 1:
            raise HTTPException(status_code=400, detail="脱敏 API Key 无法唯一匹配，请重新输入")


def _has_orphaned_model_providers(section_name: str, section_data: Any, config_data: Any) -> bool:
    if section_name != "api_providers" or not isinstance(section_data, list) or not isinstance(config_data, dict):
        return False

    provider_names = {
        provider.get("name")
        for provider in section_data
        if isinstance(provider, dict) and isinstance(provider.get("name"), str)
    }
    models = config_data.get("models", [])
    return any(
        isinstance(model, dict)
        and isinstance(model.get("api_provider"), str)
        and model.get("api_provider") not in provider_names
        for model in models
    )


def _allowed_field_names(config_class: type) -> set[str]:
    """返回配置类允许保存到 TOML 的字段名。"""
    return {field.name for field in fields(config_class) if not field.name.startswith("_")}


def _prune_legacy_bot_config_keys(config_data: Any, section_name: Optional[str] = None) -> None:
    """清理已知废弃配置键，避免 WebUI 保存后继续保留旧字段。"""
    if not isinstance(config_data, dict):
        return

    if section_name is None:
        for legacy_section in LEGACY_BOT_SECTIONS:
            config_data.pop(legacy_section, None)
        section_names = BOT_SECTION_SCHEMAS.keys()
    else:
        section_names = [section_name]

    for name in section_names:
        section = config_data.get(name)
        config_class = BOT_SECTION_SCHEMAS.get(name)
        if config_class is None or not isinstance(section, dict):
            continue

        allowed_fields = _allowed_field_names(config_class)
        for key in list(section.keys()):
            if key not in allowed_fields:
                section.pop(key, None)


def require_auth(
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> bool:
    """认证依赖：验证用户是否已登录"""
    return verify_auth_token_from_cookie_or_header(maibot_session, authorization)


# ===== 架构获取接口 =====


@router.get("/schema/bot")
async def get_bot_config_schema(_auth: bool = Depends(require_auth)):
    """获取璃夜主程序配置架构"""
    try:
        # Config 类包含所有子配置
        schema = ConfigSchemaGenerator.generate_config_schema(Config)
        return {"success": True, "schema": schema}
    except Exception as e:
        _log_config_failure("获取配置架构失败", e)
        raise HTTPException(status_code=500, detail="获取配置架构失败") from e


@router.get("/schema/model")
async def get_model_config_schema(_auth: bool = Depends(require_auth)):
    """获取模型配置架构（包含提供商和模型任务配置）"""
    try:
        schema = ConfigSchemaGenerator.generate_config_schema(APIAdapterConfig)
        return {"success": True, "schema": schema}
    except Exception as e:
        _log_config_failure("获取模型配置架构失败", e)
        raise HTTPException(status_code=500, detail="获取模型配置架构失败") from e


# ===== 子配置架构获取接口 =====


@router.get("/schema/section/{section_name}")
async def get_config_section_schema(section_name: str, _auth: bool = Depends(require_auth)):
    """
    获取指定配置节的架构

    支持的section_name:
    - bot: BotConfig
    - personality: PersonalityConfig
    - relationship: RelationshipConfig
    - chat: ChatConfig
    - message_receive: MessageReceiveConfig
    - emoji: EmojiConfig
    - expression: ExpressionConfig
    - behavior: BehaviorConfig
    - keyword_reaction: KeywordReactionConfig
    - chinese_typo: ChineseTypoConfig
    - response_post_process: ResponsePostProcessConfig
    - response_splitter: ResponseSplitterConfig
    - telemetry: TelemetryConfig
    - log: LogConfig
    - webui: WebUIConfig
    - experimental: ExperimentalConfig
    - maim_message: MaimMessageConfig
    - lpmm_knowledge: LPMMKnowledgeConfig
    - tool: ToolConfig
    - memory: MemoryConfig
    - debug: DebugConfig
    - voice: VoiceConfig
    - model_task_config: ModelTaskConfig
    - api_provider: APIProvider
    - model_info: ModelInfo
    """
    section_map = {
        **BOT_SECTION_SCHEMAS,
        "model_task_config": ModelTaskConfig,
        "api_provider": APIProvider,
        "model_info": ModelInfo,
    }

    if section_name not in section_map:
        raise HTTPException(status_code=404, detail=f"配置节 '{section_name}' 不存在")

    try:
        config_class = section_map[section_name]
        schema = ConfigSchemaGenerator.generate_schema(config_class, include_nested=False)
        return {"success": True, "schema": schema}
    except Exception as e:
        _log_config_failure("获取配置节架构失败", e)
        raise HTTPException(status_code=500, detail="获取配置节架构失败") from e


# ===== 配置读取接口 =====


@router.get("/bot")
async def get_bot_config(_auth: bool = Depends(require_auth)):
    """获取璃夜主程序配置"""
    try:
        config_data, _ = _load_config_document("bot_config.toml")

        return {"success": True, "config": config_data}
    except HTTPException:
        raise
    except Exception as e:
        _log_config_failure("读取璃夜主程序配置失败", e)
        raise HTTPException(status_code=500, detail="读取配置文件失败") from e


@router.get("/model")
async def get_model_config(_auth: bool = Depends(require_auth)):
    """获取模型配置（包含提供商和模型任务配置）"""
    try:
        config_data, _ = _load_config_document("model_config.toml")

        return {"success": True, "config": _mask_model_api_keys(config_data)}
    except HTTPException:
        raise
    except Exception as e:
        _log_config_failure("读取模型配置失败", e)
        raise HTTPException(status_code=500, detail="读取配置文件失败") from e


# ===== 配置更新接口 =====


@router.post("/bot")
async def update_bot_config(config_data: ConfigBody, _auth: bool = Depends(require_auth)):
    """更新璃夜主程序配置"""
    try:
        _prune_legacy_bot_config_keys(config_data)

        # 验证配置数据
        try:
            Config.from_dict(config_data)
        except Exception as e:
            _log_config_failure("璃夜主程序配置数据验证失败", e)
            raise HTTPException(status_code=400, detail=_CONFIG_VALIDATION_DETAIL) from e

        # 保存配置文件（自动保留注释和格式）
        _save_structured_config("bot_config.toml", config_data, prune_bot=True)

        logger.info("璃夜主程序配置已更新")
        return {"success": True, "message": "配置已保存"}
    except HTTPException:
        raise
    except Exception as e:
        _log_config_failure("保存璃夜主程序配置失败", e)
        raise HTTPException(status_code=500, detail="保存配置文件失败") from e


@router.post("/model")
async def update_model_config(config_data: ConfigBody, _auth: bool = Depends(require_auth)):
    """更新模型配置"""
    try:
        # 验证配置数据
        try:
            APIAdapterConfig.from_dict(config_data)
        except Exception as e:
            _log_config_failure("模型配置数据验证失败", e)
            raise HTTPException(status_code=400, detail=_CONFIG_VALIDATION_DETAIL) from e

        # 保存配置文件（自动保留注释和格式）
        _save_structured_config("model_config.toml", config_data)

        logger.info("模型配置已更新")
        return {"success": True, "message": "配置已保存"}
    except HTTPException:
        raise
    except Exception as e:
        _log_config_failure("保存模型配置失败", e)
        raise HTTPException(status_code=500, detail="保存配置文件失败") from e


# ===== 配置节更新接口 =====


@router.post("/bot/section/{section_name}")
async def update_bot_config_section(section_name: str, section_data: SectionBody, _auth: bool = Depends(require_auth)):
    """更新璃夜主程序配置的指定节（保留注释和格式）"""
    try:
        save_bot_config_section(section_name, section_data)

        logger.info("配置节已更新（保留注释）", section=section_name)
        return {"success": True, "message": f"配置节 '{section_name}' 已保存"}
    except HTTPException:
        raise
    except Exception as e:
        _log_config_failure("更新璃夜主程序配置节失败", e)
        raise HTTPException(status_code=500, detail="更新配置节失败") from e


# ===== 原始 TOML 文件操作接口 =====


@router.get("/bot/raw")
async def get_bot_config_raw(_auth: bool = Depends(require_auth)):
    """获取璃夜主程序配置的原始 TOML 内容"""
    try:
        raw_content, _ = _read_config_text("bot_config.toml")

        return {"success": True, "content": raw_content}
    except HTTPException:
        raise
    except Exception as e:
        _log_config_failure("读取原始配置文件失败", e)
        raise HTTPException(status_code=500, detail="读取配置文件失败") from e


@router.post("/bot/raw")
async def update_bot_config_raw(raw_content: RawContentBody, _auth: bool = Depends(require_auth)):
    """更新璃夜主程序配置（直接保存原始 TOML 内容，会先验证格式）"""
    try:
        if not isinstance(raw_content, str):
            raise HTTPException(status_code=400, detail="配置内容必须是字符串")
        encoded_content = raw_content.encode("utf-8")
        if len(encoded_content) > MAX_CONFIG_FILE_BYTES:
            raise HTTPException(status_code=413, detail="配置内容过大")

        # 验证 TOML 格式
        try:
            config_data = tomlkit.loads(raw_content)
        except Exception as e:
            raise HTTPException(status_code=400, detail="TOML 格式错误，请检查配置语法") from e

        # 验证配置数据结构
        try:
            Config.from_dict(config_data)
        except Exception as e:
            raise HTTPException(status_code=400, detail="配置数据验证失败，请检查字段和值") from e

        # 保存配置文件
        config_path = _config_file_path("bot_config.toml", create_directory=True)
        fingerprint = _config_fingerprint(config_path)
        _atomic_write_config("bot_config.toml", encoded_content, fingerprint)

        logger.info("璃夜主程序配置已更新（原始模式）")
        return {"success": True, "message": "配置已保存"}
    except HTTPException:
        raise
    except Exception as e:
        _log_config_failure("保存原始配置文件失败", e)
        raise HTTPException(status_code=500, detail="保存配置文件失败") from e


@router.post("/model/section/{section_name}")
async def update_model_config_section(
    section_name: str, section_data: SectionBody, _auth: bool = Depends(require_auth)
):
    """更新模型配置的指定节（保留注释和格式）"""
    try:
        # 读取现有配置
        config_data, fingerprint = _load_config_document("model_config.toml")

        # 更新指定节
        if section_name not in config_data:
            raise HTTPException(status_code=404, detail=f"配置节 '{section_name}' 不存在")

        if section_name == "api_providers":
            _restore_masked_model_api_keys({"api_providers": section_data}, config_data)

        # 使用递归合并保留注释（对于字典类型）
        # 对于数组表（如 [[models]], [[api_providers]]），直接替换
        if isinstance(section_data, list):
            # 列表直接替换
            config_data[section_name] = section_data
        elif isinstance(section_data, dict) and isinstance(config_data[section_name], dict):
            # 字典递归合并
            _update_toml_doc(config_data[section_name], section_data)
        else:
            # 其他类型直接替换
            config_data[section_name] = section_data

        if _has_orphaned_model_providers(section_name, section_data, config_data):
            raise HTTPException(
                status_code=400,
                detail="仍有模型引用已删除的提供商，请先重新分配或删除这些模型",
            )

        # 验证完整配置
        try:
            APIAdapterConfig.from_dict(config_data)
        except Exception as e:
            _log_config_failure("模型配置节数据验证失败", e)
            raise HTTPException(status_code=400, detail=_CONFIG_VALIDATION_DETAIL) from e

        # 保存配置（格式化数组为多行，保留注释）
        _atomic_write_config("model_config.toml", _render_config_bytes(config_data), fingerprint)

        logger.info("配置节已更新（保留注释）", section=section_name)
        return {"success": True, "message": f"配置节 '{section_name}' 已保存"}
    except HTTPException:
        raise
    except Exception as e:
        _log_config_failure("更新模型配置节失败", e)
        raise HTTPException(status_code=500, detail="更新配置节失败") from e


# ===== 适配器配置管理接口 =====


class _ManagedAdapterUnavailable(RuntimeError):
    """The optional external adapter plugin is not currently installed."""


def _get_onebot_plugin_path() -> Path:
    from src.plugin_system.apis import plugin_manage_api

    try:
        raw_path = Path(plugin_manage_api.get_plugin_path("onebot_adapter"))
    except ValueError as exc:
        raise _ManagedAdapterUnavailable from exc

    external_plugins_root = (Path(PROJECT_ROOT) / "plugins").resolve()
    try:
        if raw_path.is_symlink():
            raise RuntimeError("OneBot adapter path cannot be a symlink")
        plugin_path = raw_path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise _ManagedAdapterUnavailable from exc
    except OSError as exc:
        raise RuntimeError("OneBot adapter path is invalid") from exc
    if plugin_path.parent != external_plugins_root or not plugin_path.is_dir():
        raise RuntimeError("OneBot adapter path is outside the external plugin directory")
    return plugin_path


def _import_onebot_module(module_suffix: str):
    plugin_path = _get_onebot_plugin_path()
    return importlib.import_module(f"plugins.{plugin_path.name}.{module_suffix}")


def _get_onebot_runtime_status() -> dict[str, Any]:
    adapter_runtime = _import_onebot_module("adapter_core.runtime").adapter_runtime
    return adapter_runtime.get_status()


async def _reload_onebot_adapter_config() -> bool:
    config_manager = _import_onebot_module("adapter_core.config").config_manager
    return await config_manager.reload()


@router.get("/adapters")
async def get_adapter_instances(_auth: bool = Depends(require_auth)):
    """Return public runtime state for managed platform adapter instances."""
    try:
        status = _get_onebot_runtime_status()
        return {
            "success": True,
            "adapters": [
                {
                    "id": "onebot_default",
                    "type": "onebot_v11",
                    "name": "OneBot v11 / NapCat",
                    "platform": "qq",
                    **status,
                }
            ],
        }
    except _ManagedAdapterUnavailable:
        return {"success": True, "adapters": []}
    except Exception as e:
        _log_config_failure("获取适配器实例状态失败", e)
        raise HTTPException(status_code=500, detail="获取适配器状态失败") from e


def _get_managed_adapter_path(adapter_id: str) -> str:
    if adapter_id != _ONEBOT_ADAPTER_ID:
        raise HTTPException(status_code=404, detail="适配器实例不存在") from None
    try:
        return str(_get_onebot_plugin_path() / "config.toml")
    except _ManagedAdapterUnavailable:
        raise HTTPException(status_code=404, detail="适配器实例不存在") from None


@router.get("/adapters/{adapter_id}/config")
async def get_managed_adapter_config(adapter_id: str, _auth: bool = Depends(require_auth)):
    path = Path(_validate_adapter_config_path(_get_managed_adapter_path(adapter_id)))
    document = _read_adapter_toml_document(path)
    return {"success": True, "config": _get_onebot_managed_config(document)}


@router.put("/adapters/{adapter_id}/config")
async def save_managed_adapter_config(
    adapter_id: str,
    data: ConfigBody,
    _auth: bool = Depends(require_auth),
):
    path = Path(_validate_adapter_config_path(_get_managed_adapter_path(adapter_id)))
    document = _read_adapter_toml_document(path)
    current_config = _get_onebot_managed_config(document)
    updated_config = _validate_onebot_managed_update(data, current_config)

    for section_name, section_data in updated_config.items():
        if section_name not in document or not isinstance(document[section_name], dict):
            document[section_name] = tomlkit.table()
        for field_name, value in section_data.items():
            document[section_name][field_name] = value

    content = tomlkit.dumps(document).encode("utf-8")
    if len(content) > MAX_ADAPTER_CONFIG_BYTES:
        raise HTTPException(status_code=413, detail="适配器配置内容过大")
    _atomic_write_adapter_config(path, content)
    try:
        reloaded = await _reload_onebot_adapter_config()
    except Exception as exc:
        _log_config_failure("重载适配器配置失败", exc)
        raise HTTPException(status_code=500, detail="适配器配置已保存，但运行时重载失败") from exc
    if not reloaded:
        logger.error("重载适配器配置失败")
        raise HTTPException(status_code=500, detail="适配器配置已保存，但运行时重载失败")
    logger.info("适配器配置已保存")
    return {"success": True, "message": "适配器配置已保存"}


def _read_adapter_toml_document(path: Path):
    raw_content = _read_adapter_config_bytes(path)
    try:
        content = raw_content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="适配器配置文件必须使用 UTF-8 编码") from exc
    try:
        return tomlkit.loads(content)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="TOML 格式错误，请检查配置语法") from exc


def _get_onebot_managed_config(document) -> dict[str, dict[str, Any]]:
    unwrapped = document.unwrap()
    managed_config = copy.deepcopy(_ONEBOT_MANAGED_DEFAULTS)
    for section_name, defaults in managed_config.items():
        section = unwrapped.get(section_name, {})
        if not isinstance(section, dict):
            continue
        for field_name in defaults:
            if field_name in section:
                managed_config[section_name][field_name] = section[field_name]
    return managed_config


def _validate_onebot_managed_update(
    update: dict[str, Any],
    current_config: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not update:
        raise HTTPException(status_code=400, detail="配置内容不能为空")

    unknown_sections = set(update) - set(_ONEBOT_MANAGED_DEFAULTS)
    if unknown_sections:
        raise HTTPException(status_code=400, detail="包含不支持的适配器配置项")

    merged = copy.deepcopy(current_config)
    for section_name, section_update in update.items():
        if not isinstance(section_update, dict):
            raise HTTPException(status_code=400, detail="适配器配置节格式无效")
        unknown_fields = set(section_update) - set(_ONEBOT_MANAGED_DEFAULTS[section_name])
        if unknown_fields:
            raise HTTPException(status_code=400, detail="包含不支持的适配器配置项")
        merged[section_name].update(section_update)

    _validate_onebot_managed_config(merged)
    return merged


def _validate_onebot_managed_config(config: dict[str, dict[str, Any]]) -> None:
    napcat = config["napcat_server"]
    host = napcat["host"]
    if (
        not isinstance(host, str)
        or not host
        or host != host.strip()
        or len(host) > 255
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in host)
    ):
        raise HTTPException(status_code=400, detail="NapCat 监听地址无效")
    if not _is_plain_int(napcat["port"]) or not 1 <= napcat["port"] <= 65535:
        raise HTTPException(status_code=400, detail="NapCat 监听端口无效")
    token = napcat["token"]
    if not isinstance(token, str) or len(token) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in token):
        raise HTTPException(status_code=400, detail="NapCat 访问令牌无效")
    if not token.strip() and not _is_loopback_bind_host(host):
        raise HTTPException(status_code=400, detail="非本机监听地址必须配置访问令牌")
    heartbeat_interval = napcat["heartbeat_interval"]
    if not _is_plain_int(heartbeat_interval) or not 1 <= heartbeat_interval <= 3600:
        raise HTTPException(status_code=400, detail="NapCat 心跳间隔无效")

    chat = config["chat"]
    for list_type_name in ("group_list_type", "private_list_type"):
        if chat[list_type_name] not in {"whitelist", "blacklist"}:
            raise HTTPException(status_code=400, detail="名单模式无效")
    for list_name in ("group_list", "private_list", "ban_user_id"):
        values = chat[list_name]
        if (
            not isinstance(values, list)
            or len(values) > 4096
            or any(not _is_plain_int(value) or value < 0 for value in values)
        ):
            raise HTTPException(status_code=400, detail="账号或群组名单无效")
    for option_name in ("ban_qq_bot", "enable_poke"):
        if not isinstance(chat[option_name], bool):
            raise HTTPException(status_code=400, detail="聊天开关配置无效")

    if not isinstance(config["voice"]["use_tts"], bool):
        raise HTTPException(status_code=400, detail="语音配置无效")
    image_threshold = config["forward"]["image_threshold"]
    if not _is_plain_int(image_threshold) or not 0 <= image_threshold <= 1000:
        raise HTTPException(status_code=400, detail="转发图片阈值无效")
    if config["debug"]["level"] not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise HTTPException(status_code=400, detail="日志等级无效")


def _is_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_loopback_bind_host(host: str) -> bool:
    normalized = host.strip().lower().rstrip(".")
    if normalized == "localhost":
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    normalized = normalized.split("%", maxsplit=1)[0]
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _normalize_adapter_path(path: str) -> str:
    """将路径转换为绝对路径（如果是相对路径，则相对于项目根目录）"""
    if not isinstance(path, str):
        raise ValueError("适配器配置路径格式无效")
    if not path:
        return path
    if (
        path != path.strip()
        or len(path) > _MAX_ADAPTER_PATH_LENGTH
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    ):
        raise ValueError("适配器配置路径格式无效")

    project_root = Path(PROJECT_ROOT).resolve()
    candidate = Path(path) if Path(path).is_absolute() else project_root / path
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError("适配器配置路径格式无效") from exc

    allow_external = os.getenv("MAIBOT_ALLOW_EXTERNAL_ADAPTER_CONFIG", "").lower() in _TRUE_VALUES
    if not allow_external:
        try:
            resolved.relative_to(project_root)
        except ValueError as exc:
            raise ValueError(
                "适配器配置必须位于项目目录内；如确需外部路径，请显式设置 MAIBOT_ALLOW_EXTERNAL_ADAPTER_CONFIG=1"
            ) from exc
    return str(resolved)


def _validate_adapter_config_path(path: str) -> str:
    abs_path = _normalize_adapter_path(path)
    if Path(abs_path).suffix.lower() != ".toml":
        raise ValueError("只支持 .toml 格式的配置文件")
    return abs_path


def _to_relative_path(path: str) -> str:
    """尝试将绝对路径转换为相对于项目根目录的相对路径，如果无法转换则返回原路径"""
    if not path or not os.path.isabs(path):
        return path

    try:
        return str(Path(path).relative_to(Path(PROJECT_ROOT).resolve()))
    except (ValueError, TypeError):
        pass

    # 无法转换为相对路径，返回绝对路径
    return path


def _adapter_config_fingerprint(path: Path) -> tuple[int, int, int, int, int] | object:
    try:
        file_stat = os.lstat(path)
    except FileNotFoundError:
        return _MISSING_ADAPTER_CONFIG
    except OSError as exc:
        raise HTTPException(status_code=400, detail="适配器配置路径无效") from exc
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
        raise HTTPException(status_code=400, detail="适配器配置路径不是普通文件")
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _prepare_adapter_config_parent(path: Path) -> None:
    parent = path.parent
    try:
        if parent.is_symlink():
            raise HTTPException(status_code=400, detail="适配器配置目录路径无效")
        parent.mkdir(parents=True, exist_ok=True)
        parent_stat = os.lstat(parent)
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise HTTPException(status_code=400, detail="适配器配置目录路径无效")

        project_root = Path(PROJECT_ROOT).resolve()
        try:
            parent.resolve(strict=True).relative_to(project_root)
        except ValueError:
            return
        mode = stat.S_IMODE(parent_stat.st_mode)
        if mode & 0o022:
            os.chmod(parent, mode & ~0o022)
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(status_code=400, detail="适配器配置目录路径无效") from exc


def _read_adapter_config_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        if path.is_symlink():
            raise HTTPException(status_code=400, detail="适配器配置路径无效")
        file_descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="配置文件不存在") from None
    except HTTPException:
        raise
    except OSError as exc:
        if path.is_symlink() or exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise HTTPException(status_code=400, detail="适配器配置路径无效") from None
        raise

    try:
        file_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise HTTPException(status_code=400, detail="适配器配置路径不是普通文件")
        with os.fdopen(file_descriptor, "rb") as file:
            file_descriptor = -1
            raw_content = file.read(MAX_ADAPTER_CONFIG_BYTES + 1)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)

    if len(raw_content) > MAX_ADAPTER_CONFIG_BYTES:
        raise HTTPException(status_code=413, detail="适配器配置文件过大")
    return raw_content


def _atomic_write_adapter_config(path: Path, content: bytes) -> None:
    _prepare_adapter_config_parent(path)
    expected_fingerprint = _adapter_config_fingerprint(path)
    file_descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "wb") as file:
            file_descriptor = -1
            file.write(content)
            file.flush()
            os.fsync(file.fileno())

        if _adapter_config_fingerprint(path) != expected_fingerprint:
            raise HTTPException(status_code=409, detail="适配器配置文件已发生变化，请重试")
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        temp_path.unlink(missing_ok=True)


@router.get("/adapter-config/path")
async def get_adapter_config_path(_auth: bool = Depends(require_auth)):
    """获取保存的适配器配置文件路径"""
    try:
        adapter_config_path = get_token_manager().get_adapter_config_path_preference()
        if not adapter_config_path:
            return {"success": True, "path": None}

        # 旧版本可能保存过项目外路径，读取时也必须重新执行当前安全策略。
        abs_path = _validate_adapter_config_path(adapter_config_path)
        config_path = Path(abs_path)

        # 检查文件是否存在并返回最后修改时间
        fingerprint = _adapter_config_fingerprint(config_path)
        if fingerprint is not _MISSING_ADAPTER_CONFIG:
            import datetime

            mtime = os.lstat(config_path).st_mtime
            last_modified = datetime.datetime.fromtimestamp(mtime).isoformat()
            # 返回相对路径（如果可能）
            display_path = _to_relative_path(abs_path)
            return {"success": True, "path": display_path, "lastModified": last_modified}
        else:
            # 文件不存在时仍返回经过规范化的安全路径。
            return {"success": True, "path": _to_relative_path(abs_path), "lastModified": None}

    except ValueError:
        raise HTTPException(status_code=400, detail="适配器配置路径无效") from None
    except HTTPException:
        raise
    except Exception as e:
        _log_config_failure("获取适配器配置路径失败", e)
        raise HTTPException(status_code=500, detail="获取配置路径失败") from e


@router.post("/adapter-config/path")
async def save_adapter_config_path(data: PathBody, _auth: bool = Depends(require_auth)):
    """保存适配器配置文件路径偏好"""
    try:
        path = data.get("path")
        if not path:
            raise HTTPException(status_code=400, detail="路径不能为空")

        # 先执行路径和扩展名校验，避免把不安全路径持久化到偏好文件。
        abs_path = _validate_adapter_config_path(path)

        # 尝试转换为相对路径保存（如果文件在项目目录内）
        save_path = _to_relative_path(abs_path)
        get_token_manager().set_adapter_config_path_preference(save_path)

        logger.info("适配器配置路径已保存")
        return {"success": True, "message": "路径已保存"}

    except ValueError:
        raise HTTPException(status_code=400, detail="适配器配置路径无效") from None
    except HTTPException:
        raise
    except Exception as e:
        _log_config_failure("保存适配器配置路径失败", e)
        raise HTTPException(status_code=500, detail="保存路径失败") from e


@router.get("/adapter-config")
async def get_adapter_config(path: str, _auth: bool = Depends(require_auth)):
    """从指定路径读取适配器配置文件"""
    try:
        if not path:
            raise HTTPException(status_code=400, detail="路径参数不能为空")

        abs_path = _validate_adapter_config_path(path)
        config_path = Path(abs_path)

        # 二进制限量读取后再严格解码，避免超大文件和非法编码耗尽资源。
        raw_content = _read_adapter_config_bytes(config_path)
        try:
            content = raw_content.decode("utf-8")
        except UnicodeDecodeError as e:
            raise HTTPException(status_code=400, detail="适配器配置文件必须使用 UTF-8 编码") from e

        logger.info("已读取适配器配置")
        return {"success": True, "content": content}

    except ValueError:
        raise HTTPException(status_code=400, detail="适配器配置路径无效") from None
    except HTTPException:
        raise
    except Exception as e:
        _log_config_failure("读取适配器配置失败", e)
        raise HTTPException(status_code=500, detail="读取配置失败") from e


@router.post("/adapter-config")
async def save_adapter_config(data: PathBody, _auth: bool = Depends(require_auth)):
    """保存适配器配置到指定路径"""
    try:
        path = data.get("path")
        content = data.get("content")

        if not path:
            raise HTTPException(status_code=400, detail="路径不能为空")
        if content is None:
            raise HTTPException(status_code=400, detail="配置内容不能为空")
        if not isinstance(content, str):
            raise HTTPException(status_code=400, detail="配置内容必须是字符串")

        abs_path = _validate_adapter_config_path(path)

        encoded_content = content.encode("utf-8")
        if len(encoded_content) > MAX_ADAPTER_CONFIG_BYTES:
            raise HTTPException(status_code=413, detail="适配器配置内容过大")

        # 验证 TOML 格式
        try:
            tomlkit.loads(content)
        except Exception as e:
            raise HTTPException(status_code=400, detail="TOML 格式错误，请检查配置语法") from e

        _atomic_write_adapter_config(Path(abs_path), encoded_content)

        logger.info("适配器配置已保存")
        return {"success": True, "message": "配置已保存"}

    except ValueError:
        raise HTTPException(status_code=400, detail="适配器配置路径无效") from None
    except HTTPException:
        raise
    except Exception as e:
        _log_config_failure("保存适配器配置失败", e)
        raise HTTPException(status_code=500, detail="保存配置失败") from e
