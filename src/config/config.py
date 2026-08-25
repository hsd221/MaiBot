import os
import json
import tomlkit
import stat
import tempfile

from datetime import datetime
from tomlkit import TOMLDocument
from tomlkit.items import Table, KeyType
from dataclasses import field, dataclass, fields
from rich.traceback import install
from typing import Any, Callable, ClassVar, List, Optional

from src.common.logger import get_logger
from src.common.toml_utils import format_toml_string
from src.config.config_base import ConfigBase
from src.config.config_generation import render_config_toml
from src.config.official_configs import (
    BotConfig,
    PersonalityConfig,
    ExpressionConfig,
    BehaviorConfig,
    ChatConfig,
    EmojiConfig,
    KeywordReactionConfig,
    ChineseTypoConfig,
    ResponsePostProcessConfig,
    ResponseSplitterConfig,
    TelemetryConfig,
    LogConfig,
    ExperimentalConfig,
    MessageReceiveConfig,
    MaimMessageConfig,
    LPMMKnowledgeConfig,
    RelationshipConfig,
    ToolConfig,
    VoiceConfig,
    MemoryConfig,
    DebugConfig,
    WebUIConfig,
    UpdateConfig,
)

from .api_ada_configs import (
    ModelTaskConfig,
    ModelInfo,
    APIProvider,
    TaskConfig,
)


install(extra_lines=3)


# 配置主程序日志格式
logger = get_logger("config")

# 获取当前文件所在目录的父目录的父目录（即RiyaBot项目根目录）
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")

# 考虑到，实际上配置文件中的mai_version是不会自动更新的,所以采用硬编码
# 对该字段的更新，请严格参照语义化版本规范：https://semver.org/lang/zh-CN/
MMC_VERSION = "0.14.1-dev.1"
BOT_CONFIG_VERSION = "7.9.0"
MODEL_CONFIG_VERSION = "1.12.0"


@dataclass(frozen=True)
class DefaultValueMigration:
    """仅当用户仍保留旧默认值时，才随配置版本更新该字段。"""

    target_version: str
    path: tuple[str, ...]
    old_value: Any
    new_value: Any


DEFAULT_VALUE_MIGRATIONS: dict[str, tuple[DefaultValueMigration, ...]] = {
    "bot_config": (),
    "model_config": (),
}

_CREATED_CONFIG_FILES: list[str] = []
_MAX_CONFIG_FILE_BYTES = 8 * 1024 * 1024
_MAX_WEBUI_CONFIG_BYTES = 1024 * 1024


def get_created_config_files() -> list[str]:
    """返回本次启动期间由 Python 默认定义新创建的配置文件。"""
    return list(_CREATED_CONFIG_FILES)


def _secure_directory(path: str) -> None:
    """创建固定目录并移除组/其他用户的写权限。"""
    if os.path.islink(path):
        raise RuntimeError(f"目录不能是符号链接: {path}")
    os.makedirs(path, exist_ok=True)
    directory_stat = os.lstat(path)
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise RuntimeError(f"目录路径无效: {path}")
    mode = stat.S_IMODE(directory_stat.st_mode)
    if mode & 0o022:
        os.chmod(path, mode & ~0o022)


def _secure_regular_file(path: str, *, mode: Optional[int] = None) -> bool:
    """不跟随链接地校验普通文件，可选收紧权限。"""
    try:
        file_stat = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
        raise RuntimeError(f"文件路径无效: {path}")
    if mode is not None:
        os.chmod(path, mode)
    return True


def _read_limited_bytes(path: str, max_bytes: int) -> bytes:
    if os.path.islink(path):
        raise RuntimeError(f"文件不能是符号链接: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    file_descriptor = os.open(path, flags)
    try:
        file_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise RuntimeError(f"文件路径无效: {path}")
        if file_stat.st_size > max_bytes:
            raise RuntimeError(f"文件过大: {path}")
        with os.fdopen(file_descriptor, "rb") as file:
            file_descriptor = -1
            content = file.read(max_bytes + 1)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
    if len(content) > max_bytes:
        raise RuntimeError(f"文件过大: {path}")
    return content


def _read_toml_file(path: str) -> TOMLDocument:
    try:
        content = _read_limited_bytes(path, _MAX_CONFIG_FILE_BYTES).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"TOML 文件必须使用 UTF-8: {path}") from exc
    return tomlkit.loads(content)


def _atomic_write_bytes(path: str, content: bytes, *, mode: int) -> None:
    parent = os.path.dirname(path) or "."
    _secure_directory(parent)
    if os.path.lexists(path):
        _secure_regular_file(path)

    file_descriptor, temp_name = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(file_descriptor, mode)
        with os.fdopen(file_descriptor, "wb") as file:
            file_descriptor = -1
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
        os.chmod(path, mode)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _atomic_write_text(path: str, content: str, *, mode: int = 0o600) -> None:
    encoded = content.encode("utf-8")
    if len(encoded) > _MAX_CONFIG_FILE_BYTES:
        raise RuntimeError(f"配置内容过大: {path}")
    _atomic_write_bytes(path, encoded, mode=mode)


def _copy_file_secure(source: str, destination: str, *, mode: int) -> None:
    _secure_regular_file(source)
    _atomic_write_bytes(destination, _read_limited_bytes(source, _MAX_CONFIG_FILE_BYTES), mode=mode)


def _prepare_config_storage(config_dir: str) -> None:
    _secure_directory(config_dir)
    old_dir = os.path.join(config_dir, "old")
    _secure_directory(old_dir)
    for directory in (config_dir, old_dir):
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.endswith(".toml"):
                    _secure_regular_file(entry.path, mode=0o600)


def _mark_webui_setup_required(reason: str) -> None:
    """配置文件被重新创建时，要求 WebUI 重新进入首次配置。"""
    webui_config_path = os.path.join(PROJECT_ROOT, "data", "webui.json")
    if not os.path.lexists(webui_config_path):
        return

    try:
        _secure_directory(os.path.dirname(webui_config_path))
        _secure_regular_file(webui_config_path, mode=0o600)
        raw_config = _read_limited_bytes(webui_config_path, _MAX_WEBUI_CONFIG_BYTES)
        webui_config = json.loads(raw_config.decode("utf-8"))
        if not isinstance(webui_config, dict):
            raise ValueError("WebUI 配置必须是 JSON 对象")

        webui_config["first_setup_completed"] = False
        webui_config["setup_required_reason"] = reason
        webui_config.pop("setup_completed_at", None)

        encoded_config = (json.dumps(webui_config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(encoded_config) > _MAX_WEBUI_CONFIG_BYTES:
            raise ValueError("WebUI 配置文件过大")
        _atomic_write_bytes(webui_config_path, encoded_config, mode=0o600)

        logger.info("已标记 WebUI 需要重新进行首次配置", event_code="webui.setup.required", reason=reason)
    except Exception as e:
        logger.error(
            "标记 WebUI 首次配置状态失败",
            event_code="webui.setup.mark_required_failed",
            error_type=type(e).__name__,
        )


def get_key_comment(toml_table, key):
    # 获取key的注释（如果有）
    if hasattr(toml_table, "trivia") and hasattr(toml_table.trivia, "comment"):
        return toml_table.trivia.comment
    if hasattr(toml_table, "value") and isinstance(toml_table.value, dict):
        item = toml_table.value.get(key)
        if item is not None and hasattr(item, "trivia"):
            return item.trivia.comment
    if hasattr(toml_table, "keys"):
        for k in toml_table.keys():
            if isinstance(k, KeyType) and k.key == key:  # type: ignore
                return k.trivia.comment  # type: ignore
    return None


def compare_dicts(new, old, path=None, logs=None):
    # 递归比较两个dict，找出新增和删减项，收集注释
    if path is None:
        path = []
    if logs is None:
        logs = []
    # 新增项
    for key in new:
        if key == "version":
            continue
        if key not in old:
            comment = get_key_comment(new, key)
            logs.append(f"新增: {'.'.join(path + [str(key)])}  注释: {comment or '无'}")
        elif isinstance(new[key], (dict, Table)) and isinstance(old.get(key), (dict, Table)):
            compare_dicts(new[key], old[key], path + [str(key)], logs)
    # 删减项
    for key in old:
        if key == "version":
            continue
        if key not in new:
            comment = get_key_comment(old, key)
            logs.append(f"删减: {'.'.join(path + [str(key)])}  注释: {comment or '无'}")
    return logs


def get_value_by_path(d, path):
    for k in path:
        if isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return None
    return d


def set_value_by_path(d, path, value):
    """设置嵌套字典中指定路径的值"""
    for k in path[:-1]:
        if k not in d or not isinstance(d[k], dict):
            d[k] = {}
        d = d[k]

    # 使用 tomlkit.item 来保持 TOML 格式
    try:
        d[path[-1]] = tomlkit.item(value)
    except (TypeError, ValueError):
        # 如果转换失败，直接赋值
        d[path[-1]] = value


def _version_tuple(v):
    """将版本字符串转换为元组以便比较"""
    if v is None:
        return (0,)
    return tuple(int(x) if x.isdigit() else 0 for x in str(v).replace("v", "").split("-")[0].split("."))


def _update_dict(target: TOMLDocument | dict | Table, source: TOMLDocument | dict):
    """
    将source字典的值更新到target字典中（如果target中存在相同的键）
    """
    for key, value in source.items():
        # 跳过version字段的更新
        if key == "version":
            continue
        if key in target:
            target_value = target[key]
            if isinstance(value, dict) and isinstance(target_value, (dict, Table)):
                _update_dict(target_value, value)
            else:
                try:
                    # 统一使用 tomlkit.item 来保持原生类型与转义，不对列表做字符串化处理
                    target[key] = tomlkit.item(value)
                except (TypeError, ValueError):
                    # 如果转换失败，直接赋值
                    target[key] = value


def _get_version_from_document(config: TOMLDocument | dict) -> Optional[str]:
    inner = config.get("inner")
    if isinstance(inner, dict) and "version" in inner:
        return str(inner["version"])
    return None


def _apply_default_value_migrations(
    config_name: str,
    config: TOMLDocument,
    old_version: Optional[str],
    new_version: str,
) -> None:
    for migration in sorted(
        DEFAULT_VALUE_MIGRATIONS.get(config_name, ()),
        key=lambda item: _version_tuple(item.target_version),
    ):
        target_version = _version_tuple(migration.target_version)
        if not (_version_tuple(old_version) < target_version <= _version_tuple(new_version)):
            continue
        if get_value_by_path(config, migration.path) != migration.old_value:
            continue
        set_value_by_path(config, migration.path, migration.new_value)
        logger.info(
            f"已自动将{config_name}配置 {'.'.join(migration.path)} "
            f"从旧默认值 {migration.old_value} 更新为 {migration.new_value}"
        )


def _update_config_generic(config_name: str, generate_default: Callable[[], str]) -> None:
    """用 Python 默认定义创建或升级一份运行时 TOML 配置。"""
    generated_text = generate_default()
    if not isinstance(generated_text, str):
        raise TypeError("配置生成器必须返回 TOML 字符串")
    generated_config = tomlkit.loads(generated_text)
    new_version = _get_version_from_document(generated_config)
    if not new_version:
        raise RuntimeError(f"{config_name} 的 Python 默认配置缺少 inner.version")

    config_path = os.path.join(CONFIG_DIR, f"{config_name}.toml")
    old_config_dir = os.path.join(CONFIG_DIR, "old")
    _prepare_config_storage(CONFIG_DIR)

    if not os.path.lexists(config_path):
        logger.info(f"{config_name}.toml配置文件不存在，根据 Python 默认定义创建新配置")
        _atomic_write_text(config_path, generated_text)
        created_file = f"{config_name}.toml"
        _CREATED_CONFIG_FILES.append(created_file)
        _mark_webui_setup_required(f"{created_file} 已根据 Python 默认配置创建")
        logger.info(f"已创建新{config_name}配置文件，可在 WebUI 首次配置向导中继续填写: {config_path}")
        return

    _secure_regular_file(config_path, mode=0o600)
    old_config = _read_toml_file(config_path)
    old_version = _get_version_from_document(old_config)
    if old_version == new_version:
        logger.info(f"检测到{config_name}配置文件版本号相同 (v{old_version})，跳过更新")
        return
    if old_version and _version_tuple(old_version) > _version_tuple(new_version):
        logger.warning(f"检测到{config_name}配置文件版本 v{old_version} 高于当前程序定义 v{new_version}，跳过降级")
        return

    if old_version:
        logger.info(f"检测到{config_name}版本号不同: 旧版本 v{old_version} -> 新版本 v{new_version}")
    else:
        logger.info(f"已有{config_name}配置文件未检测到版本号，可能是旧版本，将进行更新")

    _apply_default_value_migrations(config_name, old_config, old_version, new_version)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = os.path.join(old_config_dir, f"{config_name}_{timestamp}.toml")
    _copy_file_secure(config_path, backup_path, mode=0o600)
    logger.info(f"已备份旧{config_name}配置文件到: {backup_path}")

    logger.info(f"{config_name}配置项变动如下：")
    if logs := compare_dicts(generated_config, old_config):
        for log in logs:
            logger.info(log)
    else:
        logger.info("无新增或删减项")

    logger.info(f"开始合并{config_name}新旧配置...")
    _update_dict(generated_config, old_config)
    _atomic_write_text(config_path, format_toml_string(generated_config))
    logger.info(f"{config_name}配置文件更新完成，建议检查新配置文件中的内容，以免丢失重要信息")


def update_config() -> None:
    """更新 bot_config.toml 配置文件。"""
    _update_config_generic("bot_config", generate_default_bot_config)


def update_model_config() -> None:
    """更新 model_config.toml 配置文件。"""
    _update_config_generic("model_config", generate_default_model_config)


@dataclass
class Config(ConfigBase):
    """总配置类"""

    MMC_VERSION: str = field(default=MMC_VERSION, repr=False, init=False)  # 硬编码的版本信息

    bot: BotConfig
    personality: PersonalityConfig
    relationship: RelationshipConfig
    chat: ChatConfig
    message_receive: MessageReceiveConfig
    emoji: EmojiConfig
    expression: ExpressionConfig
    keyword_reaction: KeywordReactionConfig
    chinese_typo: ChineseTypoConfig
    response_post_process: ResponsePostProcessConfig
    response_splitter: ResponseSplitterConfig
    telemetry: TelemetryConfig
    log: LogConfig
    webui: WebUIConfig
    experimental: ExperimentalConfig
    maim_message: MaimMessageConfig
    lpmm_knowledge: LPMMKnowledgeConfig
    tool: ToolConfig
    memory: MemoryConfig
    debug: DebugConfig
    voice: VoiceConfig
    update: UpdateConfig = field(default_factory=UpdateConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)


@dataclass
class APIAdapterConfig(ConfigBase):
    """API Adapter配置类"""

    RUNTIME_REQUIRED_TASKS: ClassVar[tuple[str, ...]] = ("utils", "tool_use", "replyer", "planner")

    models: List[ModelInfo]
    """模型列表"""

    model_task_config: ModelTaskConfig
    """模型任务配置"""

    api_providers: List[APIProvider] = field(default_factory=list)
    """API提供商列表"""

    def __post_init__(self):
        self.validate_integrity()

    def validate_integrity(self, require_complete: bool = False) -> None:
        """校验模型配置结构。

        首次配置阶段允许 api_providers/models 为空；真正运行前可通过
        require_complete=True 要求至少配置一个提供商和一个模型。
        """
        if require_complete and not self.models:
            raise ValueError("模型列表不能为空，请在 WebUI 中添加至少一个模型。")
        if require_complete and not self.api_providers:
            raise ValueError("API提供商列表不能为空，请在 WebUI 中添加至少一个API提供商。")

        # 检查API提供商名称是否重复
        provider_names = [provider.name for provider in self.api_providers]
        if len(provider_names) != len(set(provider_names)):
            raise ValueError("API提供商名称存在重复，请检查配置文件。")

        # 检查模型名称是否重复
        model_names = [model.name for model in self.models]
        if len(model_names) != len(set(model_names)):
            raise ValueError("模型名称存在重复，请检查配置文件。")

        self.api_providers_dict = {provider.name: provider for provider in self.api_providers}
        self.models_dict = {model.name: model for model in self.models}

        for model in self.models:
            if not model.model_identifier:
                raise ValueError(f"模型 '{model.name}' 的 model_identifier 不能为空")
            if require_complete and (not model.api_provider or model.api_provider not in self.api_providers_dict):
                raise ValueError(f"模型 '{model.name}' 的 api_provider '{model.api_provider}' 不存在")

        if require_complete:
            for task_name, unknown_models in self.get_unknown_task_models().items():
                if unknown_models:
                    raise ValueError(
                        f"任务 '{task_name}' 引用了不存在的模型: {', '.join(unknown_models)}。"
                        "请先在模型管理中添加这些模型，或从任务配置中移除。"
                    )

            missing_tasks = self.get_missing_runtime_tasks()
            if missing_tasks:
                raise ValueError(
                    f"以下运行必需任务尚未分配模型: {', '.join(missing_tasks)}。请在 WebUI 的模型管理与分配中完成配置。"
                )

    def get_unknown_task_models(self) -> dict[str, list[str]]:
        """返回任务配置中引用但未定义的模型名称。"""
        if not hasattr(self, "models_dict"):
            self.models_dict = {model.name: model for model in self.models}

        unknown: dict[str, list[str]] = {}
        for config_field in fields(self.model_task_config):
            task_config = getattr(self.model_task_config, config_field.name, None)
            if not isinstance(task_config, TaskConfig):
                continue
            missing = [model_name for model_name in task_config.model_list if model_name not in self.models_dict]
            if missing:
                unknown[config_field.name] = missing
        return unknown

    def is_runtime_ready(self) -> bool:
        """判断模型配置是否具备启动主系统的最低条件。"""
        try:
            self.validate_integrity(require_complete=True)
        except ValueError:
            return False
        return True

    def get_runtime_readiness_error(self) -> Optional[str]:
        """返回模型配置无法启动主系统的原因。"""
        try:
            self.validate_integrity(require_complete=True)
        except ValueError as e:
            return str(e)
        return None

    def get_missing_runtime_tasks(self) -> list[str]:
        """返回尚未分配模型的运行必需任务。"""
        missing: list[str] = []
        for task_name in self.RUNTIME_REQUIRED_TASKS:
            task_config = getattr(self.model_task_config, task_name, None)
            if not isinstance(task_config, TaskConfig) or not task_config.model_list:
                missing.append(task_name)
        return missing

    def get_model_info(self, model_name: str) -> ModelInfo:
        """根据模型名称获取模型信息"""
        if not model_name:
            raise ValueError("模型名称不能为空")
        if model_name not in self.models_dict:
            raise KeyError(f"模型 '{model_name}' 不存在")
        return self.models_dict[model_name]

    def get_provider(self, provider_name: str) -> APIProvider:
        """根据提供商名称获取API提供商信息"""
        if not provider_name:
            raise ValueError("API提供商名称不能为空")
        if provider_name not in self.api_providers_dict:
            raise KeyError(f"API提供商 '{provider_name}' 不存在")
        return self.api_providers_dict[provider_name]


def _create_default_bot_config() -> Config:
    return Config(
        bot=BotConfig(platform="qq", qq_account="1145141919810", nickname="璃夜"),
        personality=PersonalityConfig(
            personality="是一个大二在读女大学生，现在正在上网和群友聊天，有时有点攻击性，有时比较温柔"
        ),
        relationship=RelationshipConfig(),
        chat=ChatConfig(),
        message_receive=MessageReceiveConfig(),
        emoji=EmojiConfig(),
        expression=ExpressionConfig(),
        keyword_reaction=KeywordReactionConfig(),
        chinese_typo=ChineseTypoConfig(),
        response_post_process=ResponsePostProcessConfig(),
        response_splitter=ResponseSplitterConfig(),
        telemetry=TelemetryConfig(),
        log=LogConfig(),
        webui=WebUIConfig(),
        experimental=ExperimentalConfig(),
        maim_message=MaimMessageConfig(),
        lpmm_knowledge=LPMMKnowledgeConfig(),
        tool=ToolConfig(),
        memory=MemoryConfig(),
        debug=DebugConfig(),
        voice=VoiceConfig(),
        update=UpdateConfig(),
    )


def _create_default_model_task_config() -> ModelTaskConfig:
    return ModelTaskConfig(
        utils=TaskConfig(max_tokens=4096, temperature=0.2, selection_strategy="random"),
        replyer=TaskConfig(max_tokens=2048, slow_threshold=25.0, selection_strategy="random"),
        vlm=TaskConfig(max_tokens=256, selection_strategy="random"),
        voice=TaskConfig(slow_threshold=12.0, selection_strategy="random"),
        tool_use=TaskConfig(temperature=0.7, slow_threshold=10.0, selection_strategy="random"),
        planner=TaskConfig(max_tokens=800, slow_threshold=12.0, selection_strategy="random"),
        embedding=TaskConfig(slow_threshold=5.0, selection_strategy="random"),
        memory_encoder=TaskConfig(max_tokens=800, temperature=0.2, slow_threshold=20.0, selection_strategy="random"),
        memory_weaver=TaskConfig(max_tokens=800, temperature=0.2, slow_threshold=20.0, selection_strategy="random"),
    )


def generate_default_bot_config() -> str:
    return render_config_toml(_create_default_bot_config(), BOT_CONFIG_VERSION)


def generate_default_model_config() -> str:
    default_config = APIAdapterConfig(models=[], model_task_config=_create_default_model_task_config())
    return render_config_toml(default_config, MODEL_CONFIG_VERSION)


def load_config(config_path: str) -> Config:
    """
    加载配置文件
    Args:
        config_path: 配置文件路径
    Returns:
        Config对象
    """
    config_data = _read_toml_file(config_path)

    # 创建Config对象
    try:
        return Config.from_dict(config_data)
    except Exception as e:
        logger.critical("配置文件解析失败")
        raise e


def api_ada_load_config(config_path: str) -> APIAdapterConfig:
    """
    加载API适配器配置文件
    Args:
        config_path: 配置文件路径
    Returns:
        APIAdapterConfig对象
    """
    config_data = _read_toml_file(config_path)

    # 创建APIAdapterConfig对象
    try:
        return APIAdapterConfig.from_dict(config_data)
    except Exception as e:
        logger.critical("API适配器配置文件解析失败")
        raise e


# 获取配置文件路径
logger.info("应用版本已加载", event_code="config.version.loaded", version=MMC_VERSION)
update_config()
update_model_config()

logger.info("配置文件开始加载", event_code="config.load.started")
global_config = load_config(config_path=os.path.join(CONFIG_DIR, "bot_config.toml"))
model_config = api_ada_load_config(config_path=os.path.join(CONFIG_DIR, "model_config.toml"))
logger.info("配置文件加载完成", event_code="config.load.completed")
