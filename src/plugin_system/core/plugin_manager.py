import os
import stat

from typing import Dict, List, Optional, Tuple, Type, Any
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path


from src.common.logger import get_logger, hash_id
from src.plugin_system.base.plugin_base import PluginBase
from src.plugin_system.marketplace import parse_json_document
from src.plugin_system.utils.manifest_utils import ManifestValidator, VersionComparator
from .component_registry import component_registry

logger = get_logger("plugin_manager")
MAX_PLUGIN_MANIFEST_BYTES = 256 * 1024


class PluginManager:
    """
    插件管理器类

    负责加载，重载和卸载插件，同时管理插件的所有组件
    """

    def __init__(self):
        self.plugin_directories: List[str] = []  # 插件根目录列表
        self.plugin_classes: Dict[str, Type[PluginBase]] = {}  # 全局插件类注册表，插件名 -> 插件类
        self.plugin_paths: Dict[str, str] = {}  # 记录插件名到目录路径的映射，插件名 -> 目录路径

        self.loaded_plugins: Dict[str, PluginBase] = {}  # 已加载的插件类实例注册表，插件名 -> 插件类实例
        self.failed_plugins: Dict[str, str] = {}  # 记录加载失败的插件文件及其错误信息，插件名 -> 错误信息

        # 确保插件目录存在
        self._ensure_plugin_directories()
        logger.info("插件管理器初始化完成", event_code="plugin.manager.initialized")

    # === 插件目录管理 ===

    def add_plugin_directory(self, directory: str) -> bool:
        """添加插件目录"""
        if os.path.exists(directory):
            if directory not in self.plugin_directories:
                self.plugin_directories.append(directory)
                logger.debug("插件目录已添加", event_code="plugin.directory.added", directory=directory)
                return True
            else:
                logger.warning("插件目录已存在，跳过添加", event_code="plugin.directory.duplicate", directory=directory)
        else:
            logger.warning("插件目录不存在", event_code="plugin.directory.missing", directory=directory)
        return False

    # === 插件加载管理 ===

    def load_all_plugins(self) -> Tuple[int, int]:
        """加载所有插件

        Returns:
            tuple[int, int]: (插件数量, 组件数量)
        """
        logger.debug("插件加载开始", event_code="plugin.load_all.started")

        # 第一阶段：加载所有插件模块（注册插件类）
        total_loaded_modules = 0
        total_failed_modules = 0

        for directory in self.plugin_directories:
            loaded, failed = self._load_plugin_modules_from_directory(directory)
            total_loaded_modules += loaded
            total_failed_modules += failed

        logger.debug(
            "插件模块扫描完成",
            event_code="plugin.modules.scanned",
            loaded_count=total_loaded_modules,
            failed_count=total_failed_modules,
        )

        total_registered = 0
        total_failed_registration = 0

        for plugin_name in self.plugin_classes.keys():
            load_status, count = self.load_registered_plugin_classes(plugin_name)
            if load_status:
                total_registered += 1
            else:
                total_failed_registration += count

        self._show_stats(total_registered, total_failed_registration)

        return total_registered, total_failed_registration

    def load_registered_plugin_classes(self, plugin_name: str) -> Tuple[bool, int]:
        # sourcery skip: extract-duplicate-method, extract-method
        """
        加载已经注册的插件类
        """
        plugin_class = self.plugin_classes.get(plugin_name)
        if not plugin_class:
            logger.error("插件类未注册", event_code="plugin.class_missing", plugin_name=plugin_name)
            return False, 1
        try:
            # 使用记录的插件目录路径
            plugin_dir = self.plugin_paths.get(plugin_name)

            # 如果没有记录，直接返回失败
            if not plugin_dir:
                return False, 1

            plugin_instance = plugin_class(plugin_dir=plugin_dir)  # 实例化插件（可能因为缺少manifest而失败）
            if not plugin_instance:
                logger.error("插件实例化失败", event_code="plugin.instantiate_failed", plugin_name=plugin_name)
                return False, 1
            # 检查插件是否启用
            if not plugin_instance.enable_plugin:
                logger.info("插件已禁用，跳过加载", event_code="plugin.load_skipped_disabled", plugin_name=plugin_name)
                return False, 0

            # 检查版本兼容性
            is_compatible, compatibility_error = self._check_plugin_version_compatibility(
                plugin_name, plugin_instance.manifest_data
            )
            if not is_compatible:
                self.failed_plugins[plugin_name] = compatibility_error
                logger.error(
                    "插件版本兼容性检查失败",
                    event_code="plugin.version_incompatible",
                    plugin_name=plugin_name,
                    reason=compatibility_error,
                )
                return False, 1
            if plugin_instance.register_plugin():
                self.loaded_plugins[plugin_name] = plugin_instance
                self._show_plugin_components(plugin_name)
                return True, 1
            else:
                self.failed_plugins[plugin_name] = "插件注册失败"
                logger.error("插件注册失败", event_code="plugin.register_failed", plugin_name=plugin_name)
                return False, 1

        except FileNotFoundError as e:
            # manifest文件缺失
            error_msg = "缺少manifest文件"
            self.failed_plugins[plugin_name] = error_msg
            logger.error(
                "插件加载失败，manifest 文件缺失",
                event_code="plugin.manifest_missing",
                plugin_name_hash=hash_id(plugin_name),
                error_type=type(e).__name__,
            )
            return False, 1

        except ValueError as e:
            # manifest文件格式错误或验证失败
            error_msg = "manifest验证失败"
            self.failed_plugins[plugin_name] = error_msg
            logger.error(
                "插件加载失败，manifest 验证失败",
                event_code="plugin.manifest_invalid",
                plugin_name_hash=hash_id(plugin_name),
                error_type=type(e).__name__,
            )
            return False, 1

        except Exception as e:
            # 其他错误
            error_msg = "未知错误"
            self.failed_plugins[plugin_name] = error_msg
            logger.error(
                "插件加载失败",
                event_code="plugin.load_failed",
                plugin_name_hash=hash_id(plugin_name),
                error_type=type(e).__name__,
            )
            return False, 1

    async def remove_registered_plugin(self, plugin_name: str) -> bool:
        """
        禁用插件模块
        """
        if not plugin_name:
            raise ValueError("插件名称不能为空")
        if plugin_name not in self.loaded_plugins:
            logger.warning("插件未加载，无法移除", event_code="plugin.remove_not_loaded", plugin_name=plugin_name)
            return False
        plugin_instance = self.loaded_plugins[plugin_name]
        plugin_info = plugin_instance.plugin_info
        success = True
        for component in plugin_info.components:
            success &= await component_registry.remove_component(component.name, component.component_type, plugin_name)
        success &= component_registry.remove_plugin_registry(plugin_name)
        del self.loaded_plugins[plugin_name]
        return success

    async def reload_registered_plugin(self, plugin_name: str) -> bool:
        """
        重载插件模块
        """
        if not await self.remove_registered_plugin(plugin_name):
            return False
        if not self.load_registered_plugin_classes(plugin_name)[0]:
            return False
        logger.debug("插件重载完成", event_code="plugin.reload_completed", plugin_name=plugin_name)
        return True

    def rescan_plugin_directory(self) -> Tuple[int, int]:
        """
        重新扫描插件根目录
        """
        total_success = 0
        total_fail = 0
        for directory in self.plugin_directories:
            if os.path.exists(directory):
                logger.debug(
                    "插件根目录开始重新扫描", event_code="plugin.directory.rescan_started", directory=directory
                )
                success, fail = self._load_plugin_modules_from_directory(directory)
                total_success += success
                total_fail += fail
            else:
                logger.warning("插件根目录不存在", event_code="plugin.directory.missing", directory=directory)
        return total_success, total_fail

    def get_plugin_instance(self, plugin_name: str) -> Optional["PluginBase"]:
        """获取插件实例

        Args:
            plugin_name: 插件名称

        Returns:
            Optional[BasePlugin]: 插件实例或None
        """
        return self.loaded_plugins.get(plugin_name)

    # === 查询方法 ===
    def list_loaded_plugins(self) -> List[str]:
        """
        列出所有当前加载的插件。

        Returns:
            list: 当前加载的插件名称列表。
        """
        return list(self.loaded_plugins.keys())

    def list_registered_plugins(self) -> List[str]:
        """
        列出所有已注册的插件类。

        Returns:
            list: 已注册的插件类名称列表。
        """
        return list(self.plugin_classes.keys())

    def get_plugin_path(self, plugin_name: str) -> Optional[str]:
        """
        获取指定插件的路径。

        Args:
            plugin_name: 插件名称

        Returns:
            Optional[str]: 插件目录的绝对路径，如果插件不存在则返回None。
        """
        return self.plugin_paths.get(plugin_name)

    # === 私有方法 ===
    # == 目录管理 ==
    def _ensure_plugin_directories(self) -> None:
        """确保所有插件根目录存在，如果不存在则创建"""
        default_directories = ["src/plugins/built_in", "plugins"]

        for directory in default_directories:
            if not os.path.exists(directory):
                os.makedirs(directory, exist_ok=True)
                logger.info("插件根目录已创建", event_code="plugin.directory.created", directory=directory)
            if directory not in self.plugin_directories:
                self.plugin_directories.append(directory)
                logger.debug("插件根目录已添加", event_code="plugin.directory.added", directory=directory)
            else:
                logger.warning(
                    "插件根目录已存在，跳过添加", event_code="plugin.directory.duplicate", directory=directory
                )

    # == 插件加载 ==

    def _load_plugin_modules_from_directory(self, directory: str) -> tuple[int, int]:
        """从指定目录加载插件模块"""
        loaded_count = 0
        failed_count = 0
        directory_path = Path(directory)

        if directory_path.is_symlink() or not directory_path.is_dir():
            logger.warning("插件根目录不存在", event_code="plugin.directory.missing", directory=directory)
            return 0, 1

        logger.debug("插件根目录开始扫描", event_code="plugin.directory.scan_started", directory=directory)

        # 遍历目录中的所有包
        for item_path in sorted(directory_path.iterdir(), key=lambda path: path.name):
            if item_path.name.startswith((".", "__")):
                continue
            try:
                if item_path.is_symlink() or not item_path.is_dir():
                    continue
                plugin_file = item_path / "plugin.py"
                if plugin_file.is_symlink() or not plugin_file.is_file():
                    continue
            except OSError:
                logger.warning(
                    "插件目录项检查失败",
                    event_code="plugin.directory.entry_check_failed",
                    entry=item_path.name,
                )
                failed_count += 1
                continue

            if self._load_plugin_module_file(str(plugin_file)):
                loaded_count += 1
            else:
                failed_count += 1

        return loaded_count, failed_count

    def _load_plugin_module_file(self, plugin_file: str) -> bool:
        # sourcery skip: extract-method
        """加载单个插件模块文件

        Args:
            plugin_file: 插件文件路径
            plugin_name: 插件名称
            plugin_dir: 插件目录路径
        """
        # 生成模块名
        plugin_path = Path(plugin_file)
        module_name = ".".join(plugin_path.parent.parts)

        try:
            self._preflight_plugin_module(plugin_path)

            # 动态导入插件模块
            spec = spec_from_file_location(module_name, plugin_file)
            if spec is None or spec.loader is None:
                logger.error(
                    "插件模块规范创建失败",
                    event_code="plugin.module.spec_failed",
                    plugin_file_hash=hash_id(plugin_file),
                )
                return False

            module = module_from_spec(spec)
            module.__package__ = module_name  # 设置模块包名
            spec.loader.exec_module(module)

            logger.debug(
                "插件模块加载完成",
                event_code="plugin.module.loaded",
                plugin_file_hash=hash_id(plugin_file),
                module_name_hash=hash_id(module_name),
            )
            return True

        except Exception as e:
            error_msg = "插件预检或加载失败"
            logger.error(
                "插件模块加载失败",
                event_code="plugin.module.load_failed",
                plugin_file_hash=hash_id(plugin_file),
                error_type=type(e).__name__,
            )
            self.failed_plugins[module_name] = error_msg
            return False

    def _preflight_plugin_module(self, plugin_path: Path) -> None:
        """在执行插件代码前校验目录、清单和主程序版本兼容性。"""
        plugin_dir = plugin_path.parent
        if plugin_dir.is_symlink() or plugin_path.is_symlink():
            raise ValueError("插件路径不能是符号链接")

        try:
            plugin_stat = plugin_path.stat()
        except OSError as exc:
            raise ValueError("插件入口文件不可访问") from exc
        if not stat.S_ISREG(plugin_stat.st_mode):
            raise ValueError("插件入口路径不是普通文件")

        manifest_path = plugin_dir / "_manifest.json"
        if manifest_path.is_symlink():
            raise ValueError("插件清单不能是符号链接")

        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            file_descriptor = os.open(manifest_path, flags)
        except OSError as exc:
            raise ValueError("插件缺少有效清单") from exc

        try:
            manifest_stat = os.fstat(file_descriptor)
            if not stat.S_ISREG(manifest_stat.st_mode):
                raise ValueError("插件清单路径不是普通文件")
            with os.fdopen(file_descriptor, "rb") as file:
                file_descriptor = -1
                raw_manifest = file.read(MAX_PLUGIN_MANIFEST_BYTES + 1)
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)

        if len(raw_manifest) > MAX_PLUGIN_MANIFEST_BYTES:
            raise ValueError("插件清单过大")
        try:
            manifest_data = parse_json_document(raw_manifest.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("插件清单格式无效") from exc
        if not isinstance(manifest_data, dict):
            raise ValueError("插件清单格式无效")

        validator = ManifestValidator()
        if not validator.validate_manifest(manifest_data):
            raise ValueError("插件清单验证失败")

        compatible, _ = self._check_plugin_version_compatibility(plugin_dir.name, manifest_data)
        if not compatible:
            raise ValueError("插件版本与主程序不兼容")

    # == 兼容性检查 ==

    def _check_plugin_version_compatibility(self, plugin_name: str, manifest_data: Dict[str, Any]) -> Tuple[bool, str]:
        """检查插件版本兼容性

        Args:
            plugin_name: 插件名称
            manifest_data: manifest数据

        Returns:
            Tuple[bool, str]: (是否兼容, 错误信息)
        """
        if "host_application" not in manifest_data:
            return True, ""  # 没有版本要求，默认兼容

        host_app = manifest_data["host_application"]
        if not isinstance(host_app, dict):
            return True, ""

        min_version = host_app.get("min_version", "")
        max_version = host_app.get("max_version", "")

        if not min_version and not max_version:
            return True, ""  # 没有版本要求，默认兼容

        try:
            current_version = VersionComparator.get_current_host_version()
            is_compatible, error_msg = VersionComparator.is_version_in_range(current_version, min_version, max_version)
            if not is_compatible:
                return False, f"版本不兼容: {error_msg}"
            logger.debug(
                "插件版本兼容性检查通过",
                event_code="plugin.version_compatible",
                plugin_name=plugin_name,
                current_version=current_version,
                min_version=min_version,
                max_version=max_version,
            )
            return True, ""

        except Exception as e:
            logger.warning(
                "插件版本兼容性检查异常",
                event_code="plugin.version_check_failed",
                plugin_name_hash=hash_id(plugin_name),
                error_type=type(e).__name__,
            )
            return False, "插件版本兼容性检查失败"  # 检查失败时默认不允许加载

    # == 显示统计与插件信息 ==

    def _show_stats(self, total_registered: int, total_failed_registration: int):
        # sourcery skip: low-code-quality
        # 获取组件统计信息
        stats = component_registry.get_registry_stats()
        action_count = stats.get("action_components", 0)
        command_count = stats.get("command_components", 0)
        tool_count = stats.get("tool_components", 0)
        event_handler_count = stats.get("event_handlers", 0)
        total_components = stats.get("total_components", 0)

        if total_registered > 0:
            logger.info(
                "插件系统加载完成",
                event_code="plugin.system.loaded",
                plugin_count=total_registered,
                failed_plugin_count=total_failed_registration,
                total_components=total_components,
                action_count=action_count,
                command_count=command_count,
                tool_count=tool_count,
                event_handler_count=event_handler_count,
            )

            for plugin_name in self.loaded_plugins.keys():
                if plugin_info := component_registry.get_plugin_info(plugin_name):
                    component_counts = {}
                    for component in plugin_info.components:
                        component_counts[component.component_type.value] = (
                            component_counts.get(component.component_type.value, 0) + 1
                        )
                    logger.debug(
                        "已加载插件详情",
                        event_code="plugin.loaded.detail",
                        plugin_name=plugin_name,
                        display_name=plugin_info.display_name,
                        version=plugin_info.version,
                        author=plugin_info.author,
                        license=plugin_info.license,
                        homepage_url=plugin_info.homepage_url,
                        component_counts=component_counts,
                        dependency_count=len(plugin_info.dependencies),
                        config_file=plugin_info.config_file,
                    )

            root_path = Path(__file__)

            # 查找项目根目录
            while not (root_path / "pyproject.toml").exists() and root_path.parent != root_path:
                root_path = root_path.parent

            for directory in self.plugin_directories:
                if os.path.exists(directory):
                    plugins_in_dir = []
                    for plugin_name in self.loaded_plugins.keys():
                        plugin_path = self.plugin_paths.get(plugin_name, "")
                        if (
                            Path(plugin_path)
                            .resolve()
                            .is_relative_to(Path(os.path.join(str(root_path), directory)).resolve())
                        ):
                            plugins_in_dir.append(plugin_name)

                    logger.debug(
                        "插件目录加载统计",
                        event_code="plugin.directory.stats",
                        directory=directory,
                        plugin_count=len(plugins_in_dir),
                        plugins=plugins_in_dir,
                    )

            if total_failed_registration > 0:
                logger.warning(
                    "部分插件加载失败",
                    event_code="plugin.system.partial_failure",
                    failed_plugin_count=total_failed_registration,
                )
                for failed_plugin, error in self.failed_plugins.items():
                    logger.warning(
                        "插件加载失败详情",
                        event_code="plugin.load_failure.detail",
                        plugin_name=failed_plugin,
                        error=error,
                    )
        else:
            logger.warning("没有插件加载成功", event_code="plugin.system.no_plugins_loaded")

    def _show_plugin_components(self, plugin_name: str) -> None:
        if plugin_info := component_registry.get_plugin_info(plugin_name):
            component_types = {}
            for comp in plugin_info.components:
                comp_type = comp.component_type.name
                component_types[comp_type] = component_types.get(comp_type, 0) + 1

            components_str = ", ".join([f"{count}个{ctype}" for ctype, count in component_types.items()])

            logger.info(
                "插件加载成功",
                event_code="plugin.loaded",
                plugin_name=plugin_name,
                version=plugin_info.version,
                component_summary=components_str,
                license=plugin_info.license,
                keyword_count=len(plugin_info.keywords),
                description=plugin_info.description,
            )
        else:
            logger.info("插件加载成功", event_code="plugin.loaded", plugin_name=plugin_name)


# 全局插件管理器实例
plugin_manager = PluginManager()
