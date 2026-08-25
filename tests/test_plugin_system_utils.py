import importlib.util
import sys
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_module_from_path(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(module_name, PROJECT_ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load {relative_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


component_types = load_module_from_path(
    "tests._plugin_component_types",
    "src/plugin_system/base/component_types.py",
)
config_types = load_module_from_path(
    "tests._plugin_config_types",
    "src/plugin_system/base/config_types.py",
)
manifest_utils = load_module_from_path(
    "tests._plugin_manifest_utils",
    "src/plugin_system/utils/manifest_utils.py",
)

ActionInfo = component_types.ActionInfo
ActionActivationType = component_types.ActionActivationType
ChatMode = component_types.ChatMode
CommandInfo = component_types.CommandInfo
ComponentType = component_types.ComponentType
EventHandlerInfo = component_types.EventHandlerInfo
EventType = component_types.EventType
MaiMessages = component_types.MaiMessages
PluginInfo = component_types.PluginInfo
PythonDependency = component_types.PythonDependency
ToolInfo = component_types.ToolInfo

ConfigField = config_types.ConfigField
ConfigLayout = config_types.ConfigLayout
ConfigTab = config_types.ConfigTab
section_meta = config_types.section_meta

ManifestValidator = manifest_utils.ManifestValidator
VersionComparator = manifest_utils.VersionComparator


class VersionComparatorTest(unittest.TestCase):
    def test_normalize_version_removes_prerelease_suffix_and_pads_missing_parts(self) -> None:
        self.assertEqual(VersionComparator.normalize_version(""), "0.0.0")
        self.assertEqual(VersionComparator.normalize_version("1.2-snapshot.4"), "1.2.0")
        self.assertEqual(VersionComparator.normalize_version("0.14.0-dev.1"), "0.14.0")
        self.assertEqual(VersionComparator.normalize_version("999.0.0-preview.1"), "999.0.0")
        self.assertEqual(VersionComparator.normalize_version("1.2.3-preview.1+build.7"), "1.2.3")
        self.assertEqual(VersionComparator.normalize_version(" 2 "), "2.0.0")
        self.assertEqual(VersionComparator.normalize_version("1.2.3-"), "0.0.0")
        self.assertEqual(VersionComparator.normalize_version("not-a-version"), "0.0.0")

    def test_compare_versions_uses_semantic_numeric_order(self) -> None:
        self.assertEqual(VersionComparator.compare_versions("1.10.0", "1.2.9"), 1)
        self.assertEqual(VersionComparator.compare_versions("1.0", "1.0.0"), 0)
        self.assertEqual(VersionComparator.compare_versions("0.9.9", "1.0.0"), -1)

    def test_parse_version_and_forward_compatibility_fallbacks_are_explicit(self) -> None:
        with patch.object(VersionComparator, "normalize_version", return_value="bad.version"):
            self.assertEqual(VersionComparator.parse_version("bad"), (0, 0, 0))

        self.assertEqual(VersionComparator.check_forward_compatibility("9.9.9", "1.0.0"), (False, ""))

    def test_is_version_in_range_reports_minimum_maximum_and_compatibility_map(self) -> None:
        self.assertEqual(VersionComparator.is_version_in_range("1.0.0"), (True, ""))
        below_minimum, below_message = VersionComparator.is_version_in_range("0.7.9", min_version="0.8.0")
        mapped_compatible, mapped_message = VersionComparator.is_version_in_range("0.8.10", max_version="0.8.9")
        above_maximum, above_message = VersionComparator.is_version_in_range("2.1.0", max_version="2.0.0")

        self.assertFalse(below_minimum)
        self.assertIn("低于最小要求版本", below_message)
        self.assertTrue(mapped_compatible)
        self.assertIn("兼容", mapped_message)
        self.assertFalse(above_maximum)
        self.assertIn("高于最大支持版本", above_message)

    def test_add_compatibility_mapping_normalizes_versions(self) -> None:
        original_map = VersionComparator.COMPATIBILITY_MAP.copy()
        try:
            VersionComparator.add_compatibility_mapping("1.2", ["1.2.1-snapshot.1", "bad"])

            self.assertEqual(VersionComparator.COMPATIBILITY_MAP["1.2.0"], ["1.2.1", "0.0.0"])
        finally:
            VersionComparator.COMPATIBILITY_MAP = original_map

    def test_get_compatibility_info_returns_copy_of_mapping(self) -> None:
        info = VersionComparator.get_compatibility_info()
        info["0.8.9"] = []

        self.assertIn("0.8.10", VersionComparator.get_compatibility_info()["0.8.9"])


class ManifestValidatorTest(unittest.TestCase):
    def test_valid_manifest_accepts_complete_metadata_and_component_info(self) -> None:
        validator = ManifestValidator()
        manifest = {
            "manifest_version": 1,
            "name": "sample_plugin",
            "version": "1.0.0",
            "description": "Sample plugin",
            "author": {"name": "Mai", "url": "https://example.com"},
            "license": "MIT",
            "keywords": ["sample"],
            "categories": ["utility"],
            "homepage_url": "https://example.com/plugin",
            "repository_url": "https://example.com/repo",
            "host_application": {"min_version": "1.0.0", "max_version": "2.0.0"},
            "plugin_info": {
                "components": [
                    {"type": "action", "name": "greet", "description": "Greet users"},
                ],
            },
        }

        with patch.object(VersionComparator, "get_current_host_version", return_value="1.5.0"):
            self.assertTrue(validator.validate_manifest(manifest))

        self.assertEqual(validator.validation_errors, [])
        self.assertEqual(validator.validation_warnings, [])
        self.assertIn("验证通过", validator.get_validation_report())

    def test_manifest_validation_collects_required_field_and_nested_component_errors(self) -> None:
        validator = ManifestValidator()
        manifest = {
            "manifest_version": 3,
            "name": "",
            "version": "1.0.0",
            "description": "Missing author and invalid component",
            "author": {"url": "example.com"},
            "keywords": "not-a-list",
            "plugin_info": {"components": [{"type": "action", "name": "", "description": ""}]},
        }

        self.assertFalse(validator.validate_manifest(manifest))

        errors = "\n".join(validator.validation_errors)
        warnings = "\n".join(validator.validation_warnings)
        self.assertIn("必需字段不能为空: name", errors)
        self.assertIn("不支持的manifest版本", errors)
        self.assertIn("作者信息缺少name字段或为空", errors)
        self.assertIn("keywords应为数组格式", errors)
        self.assertIn("plugin_info.components[0]缺少必需字段: name", errors)
        self.assertIn("作者URL建议使用完整的URL格式", warnings)

    def test_manifest_v2_is_supported_without_weakening_v1_compatibility(self) -> None:
        validator = ManifestValidator()
        manifest = {
            "manifest_version": 2,
            "id": "github.alice.weather",
            "name": "天气插件",
            "version": "1.2.3",
            "description": "查询天气信息",
            "author": {"name": "Alice", "url": "https://github.com/alice"},
            "license": "MIT",
            "urls": {
                "repository": "https://github.com/alice/riyabot-weather",
                "homepage": "https://github.com/alice/riyabot-weather#readme",
            },
            "host_application": {"min_version": "0.14.0"},
            "sdk": {"min_version": "1.0.0", "max_version": "1.99.99"},
            "entrypoint": "plugin.py",
            "capabilities": ["network"],
            "i18n": {"default_locale": "zh-CN", "supported_locales": ["zh-CN"]},
        }

        with patch.object(VersionComparator, "get_current_host_version", return_value="0.14.1"):
            self.assertTrue(validator.validate_manifest(manifest))

        self.assertEqual(validator.validation_errors, [])

    def test_validation_state_is_reset_between_runs(self) -> None:
        validator = ManifestValidator()
        self.assertFalse(validator.validate_manifest({}))

        valid_manifest = {
            "manifest_version": 1,
            "name": "sample",
            "version": "1.0.0",
            "description": "Sample",
            "author": "Mai",
            "license": "MIT",
            "keywords": [],
            "categories": [],
        }
        self.assertTrue(validator.validate_manifest(valid_manifest))
        self.assertEqual(validator.validation_errors, [])

    def test_manifest_validation_reports_author_host_url_list_and_plugin_info_edge_cases(self) -> None:
        validator = ManifestValidator()
        manifest = {
            "manifest_version": 1,
            "name": "sample",
            "version": "1.0.0",
            "description": "Sample",
            "author": "",
            "license": "MIT",
            "homepage_url": "example.com/home",
            "repository_url": "git.example.com/repo",
            "host_application": {"min_version": "", "max_version": ""},
            "keywords": ["ok", 1],
            "categories": ["utility", object()],
            "plugin_info": {"components": "not-a-list"},
        }

        self.assertFalse(validator.validate_manifest(manifest))

        errors = "\n".join(validator.validation_errors)
        warnings = "\n".join(validator.validation_warnings)
        self.assertIn("作者信息不能为空", errors)
        self.assertIn("plugin_info.components应为数组格式", errors)
        self.assertIn("host_application.min_version为空", warnings)
        self.assertIn("host_application.max_version为空", warnings)
        self.assertIn("homepage_url建议使用完整的URL格式", warnings)
        self.assertIn("repository_url建议使用完整的URL格式", warnings)
        self.assertIn("keywords[1]应为字符串", warnings)
        self.assertIn("categories[1]应为字符串", warnings)

        validator = ManifestValidator()
        manifest["author"] = {"name": "Mai"}
        manifest["host_application"] = []
        manifest["plugin_info"] = {"components": [123]}

        self.assertFalse(validator.validate_manifest(manifest))
        errors = "\n".join(validator.validation_errors)
        self.assertIn("host_application格式错误，应为对象", errors)
        self.assertIn("plugin_info.components[0]应为对象", errors)

        validator = ManifestValidator()
        manifest["author"] = 123
        manifest["host_application"] = {"max_version": "2.0.0"}
        manifest["plugin_info"] = []

        with patch.object(VersionComparator, "get_current_host_version", return_value="2.1.0"):
            self.assertFalse(validator.validate_manifest(manifest))

        errors = "\n".join(validator.validation_errors)
        self.assertIn("作者信息格式错误", errors)
        self.assertIn("版本兼容性检查失败", errors)
        self.assertIn("plugin_info应为对象格式", errors)


class ConfigTypesTest(unittest.TestCase):
    def test_config_field_infers_ui_type_from_type_and_constraints(self) -> None:
        self.assertEqual(ConfigField(type=bool, default=False, description="开关").get_ui_type(), "switch")
        self.assertEqual(ConfigField(type=int, default=1, description="数量").get_ui_type(), "number")
        self.assertEqual(ConfigField(type=float, default=0.5, description="概率", min=0, max=1).get_ui_type(), "slider")
        self.assertEqual(
            ConfigField(type=str, default="a", description="模式", choices=["a", "b"]).get_ui_type(), "select"
        )
        self.assertEqual(ConfigField(type=list, default=[], description="标签").get_ui_type(), "list")
        self.assertEqual(ConfigField(type=dict, default={}, description="映射").get_ui_type(), "json")
        self.assertEqual(ConfigField(type=tuple, default=(), description="未知").get_ui_type(), "text")
        self.assertEqual(
            ConfigField(type=str, default="", description="密钥", input_type="password").get_ui_type(), "password"
        )

    def test_config_field_to_dict_serializes_labels_choices_and_dependencies(self) -> None:
        field = ConfigField(
            type=str,
            default="debug",
            description="运行模式",
            choices=["debug", "release"],
            label="模式",
            depends_on="feature.enabled",
            depends_value=True,
            order=3,
        )

        data = field.to_dict()

        self.assertEqual(data["type"], "str")
        self.assertEqual(data["label"], "模式")
        self.assertEqual(data["choices"], ["debug", "release"])
        self.assertEqual(data["ui_type"], "select")
        self.assertEqual(data["depends_on"], "feature.enabled")
        self.assertTrue(data["depends_value"])
        self.assertEqual(data["order"], 3)

    def test_config_layout_and_section_meta_are_serializable(self) -> None:
        section = section_meta("基础", description="基础配置", icon="settings", collapsed=True, order=2)
        layout = ConfigLayout(
            type="tabs", tabs=[ConfigTab(id="basic", title="基础", sections=["plugin"], badge="Beta")]
        )

        self.assertEqual(
            section.to_dict(),
            {"title": "基础", "description": "基础配置", "icon": "settings", "collapsed": True, "order": 2},
        )
        self.assertEqual(
            layout.to_dict(),
            {
                "type": "tabs",
                "tabs": [
                    {
                        "id": "basic",
                        "title": "基础",
                        "sections": ["plugin"],
                        "icon": None,
                        "order": 0,
                        "badge": "Beta",
                    }
                ],
            },
        )


class ComponentTypesTest(unittest.TestCase):
    def test_enum_string_values_match_public_wire_values(self) -> None:
        self.assertEqual(str(ComponentType.ACTION), "action")
        self.assertEqual(str(ActionActivationType.KEYWORD), "keyword")
        self.assertEqual(str(ChatMode.FOCUS), "focus")
        self.assertEqual(str(EventType.ON_MESSAGE), "on_message")

    def test_python_dependency_uses_install_name_and_version_for_requirement(self) -> None:
        dependency = PythonDependency(package_name="PIL", install_name="pillow", version=">=11.0")
        default_install_name = PythonDependency(package_name="json")

        self.assertEqual(dependency.get_pip_requirement(), "pillow>=11.0")
        self.assertEqual(default_install_name.install_name, "json")
        self.assertEqual(default_install_name.get_pip_requirement(), "json")

    def test_component_info_subclasses_set_component_type_and_normalize_metadata(self) -> None:
        command = CommandInfo(name="cmd", component_type=ComponentType.ACTION, metadata=None)
        tool = ToolInfo(name="tool", component_type=ComponentType.ACTION, metadata=None)
        handler = EventHandlerInfo(name="handler", component_type=ComponentType.ACTION, metadata=None)

        self.assertEqual(command.component_type, ComponentType.COMMAND)
        self.assertEqual(command.metadata, {})
        self.assertEqual(tool.component_type, ComponentType.TOOL)
        self.assertEqual(tool.metadata, {})
        self.assertEqual(handler.component_type, ComponentType.EVENT_HANDLER)
        self.assertEqual(handler.metadata, {})

    def test_action_info_normalizes_none_collections_and_sets_component_type(self) -> None:
        action = ActionInfo(
            name="greet",
            component_type=ComponentType.COMMAND,
            metadata=None,
            activation_keywords=None,
            action_parameters=None,
            action_require=None,
            associated_types=None,
        )

        self.assertEqual(action.component_type, ComponentType.ACTION)
        self.assertEqual(action.metadata, {})
        self.assertEqual(action.activation_keywords, [])
        self.assertEqual(action.action_parameters, {})
        self.assertEqual(action.action_require, [])
        self.assertEqual(action.associated_types, [])

    def test_plugin_info_normalizes_none_collections(self) -> None:
        plugin = PluginInfo(
            display_name="Sample",
            name="sample",
            description="Sample plugin",
            components=None,
            dependencies=None,
            python_dependencies=None,
            metadata=None,
            manifest_data=None,
            keywords=None,
            categories=None,
        )

        self.assertEqual(plugin.components, [])
        self.assertEqual(plugin.dependencies, [])
        self.assertEqual(plugin.python_dependencies, [])
        self.assertEqual(plugin.metadata, {})
        self.assertEqual(plugin.manifest_data, {})
        self.assertEqual(plugin.keywords, [])
        self.assertEqual(plugin.categories, [])

    def test_plugin_info_reports_missing_required_packages_and_formats_requirements(self) -> None:
        required_missing = PythonDependency("definitely_missing_maibot_unit_package")
        optional_missing = PythonDependency("definitely_missing_optional_maibot_unit_package", optional=True)
        installed = PythonDependency("json")
        plugin = PluginInfo(
            display_name="Sample",
            name="sample",
            description="Sample plugin",
            python_dependencies=[required_missing, optional_missing, installed],
        )

        self.assertEqual(plugin.get_missing_packages(), [required_missing])
        self.assertEqual(
            plugin.get_pip_requirements(),
            [
                "definitely_missing_maibot_unit_package",
                "definitely_missing_optional_maibot_unit_package",
                "json",
            ],
        )

    def test_mai_messages_modifiers_update_values_flags_and_warning_behavior(self) -> None:
        message = MaiMessages(plain_text="old")
        message_with_none_segments = MaiMessages(message_segments=None)
        copied_message = message.deepcopy()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            message.modify_message_segments([], suppress_warning=False)

        self.assertEqual(len(caught), 1)
        self.assertEqual(message_with_none_segments.message_segments, [])
        self.assertIsNot(copied_message, message)
        self.assertTrue(message._modify_flags.modify_message_segments)

        message.plain_text = ""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            message.modify_plain_text("new", suppress_warning=False)
            message.modify_llm_prompt("prompt", suppress_warning=False)
            message.modify_llm_response_content("content", suppress_warning=False)
            message.modify_llm_response_reasoning("reasoning", suppress_warning=False)

        self.assertEqual(len(caught), 4)
        self.assertEqual(message.plain_text, "new")
        self.assertEqual(message.llm_prompt, "prompt")
        self.assertEqual(message.llm_response_content, "content")
        self.assertEqual(message.llm_response_reasoning, "reasoning")
        self.assertTrue(message._modify_flags.modify_plain_text)
        self.assertTrue(message._modify_flags.modify_llm_prompt)
        self.assertTrue(message._modify_flags.modify_llm_response_content)
        self.assertTrue(message._modify_flags.modify_llm_response_reasoning)


if __name__ == "__main__":
    unittest.main()
