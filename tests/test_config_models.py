import unittest
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Dict, Literal
from unittest.mock import patch

from src.config.api_ada_configs import APIProvider, ModelInfo, ModelTaskConfig, TaskConfig
from src.config.config_base import ConfigBase
from src.config.official_configs import _parse_stream_config_to_chat_id
from src.config.official_configs import (
    BehaviorConfig,
    ChatConfig,
    EmojiConfig,
    ExpressionConfig,
    KeywordReactionConfig,
    KeywordRuleConfig,
    MemoryConfig,
    MessageReceiveConfig,
    PersonalityConfig,
    WebUIConfig,
)


@dataclass
class NestedConfig(ConfigBase):
    name: str
    count: int = 0


@dataclass
class CompositeConfig(ConfigBase):
    title: str
    nested: NestedConfig
    nested_list: list[NestedConfig]
    tags: set[str]
    pair: tuple[str, int]
    mapping: dict[str, int]
    optional_count: int | None = None
    mode: Literal["fast", "slow"] = "fast"
    _private: str = "ignored"


class ConfigBaseTest(unittest.TestCase):
    def test_from_dict_converts_nested_collections_optional_literal_and_skips_private_fields(self) -> None:
        config = CompositeConfig.from_dict(
            {
                "title": "sample",
                "nested": {"name": "inner", "count": "2"},
                "nested_list": [{"name": "a", "count": 1}, {"name": "b", "count": "3"}],
                "tags": ["x", "y", "x"],
                "pair": ["left", "4"],
                "mapping": {"a": "1", "b": 2},
                "optional_count": "5",
                "mode": "slow",
                "_private": "should-not-override",
            }
        )

        self.assertEqual(config.title, "sample")
        self.assertEqual(config.nested.count, 2)
        self.assertEqual([item.count for item in config.nested_list], [1, 3])
        self.assertEqual(config.tags, {"x", "y"})
        self.assertEqual(config.pair, ("left", 4))
        self.assertEqual(config.mapping, {"a": 1, "b": 2})
        self.assertEqual(config.optional_count, 5)
        self.assertEqual(config.mode, "slow")
        self.assertEqual(config._private, "ignored")

    def test_from_dict_reports_missing_required_field_and_type_conversion_errors(self) -> None:
        with self.assertRaisesRegex(ValueError, "Missing required field: 'nested'"):
            CompositeConfig.from_dict(
                {
                    "title": "sample",
                    "nested_list": [],
                    "tags": [],
                    "pair": ["a", 1],
                    "mapping": {},
                }
            )

        with self.assertRaisesRegex(TypeError, "Field 'pair' has a type error"):
            CompositeConfig.from_dict(
                {
                    "title": "sample",
                    "nested": {"name": "inner"},
                    "nested_list": [],
                    "tags": [],
                    "pair": ["only-one"],
                    "mapping": {},
                }
            )

        with self.assertRaisesRegex(TypeError, "Field 'mode' has a type error"):
            CompositeConfig.from_dict(
                {
                    "title": "sample",
                    "nested": {"name": "inner"},
                    "nested_list": [],
                    "tags": [],
                    "pair": ["a", 1],
                    "mapping": {},
                    "mode": "turbo",
                }
            )

    def test_from_dict_rejects_non_dict_input_and_unsupported_multi_type_union(self) -> None:
        @dataclass
        class UnsupportedUnionConfig(ConfigBase):
            value: int | str

        with self.assertRaises(TypeError):
            CompositeConfig.from_dict([])
        with self.assertRaisesRegex(TypeError, "不支持多类型 Union"):
            UnsupportedUnionConfig.from_dict({"value": 1})

    def test_from_dict_wraps_unexpected_conversion_errors_as_runtime_error(self) -> None:
        @dataclass
        class ExplodingConfig(ConfigBase):
            value: object

            @classmethod
            def _convert_field(cls, value, field_type):
                raise RuntimeError("boom")

        with self.assertRaisesRegex(RuntimeError, "Failed to convert field 'value' to target type: boom"):
            ExplodingConfig.from_dict({"value": "x"})

    def test_convert_field_rejects_invalid_nested_collection_dict_and_optional_values(self) -> None:
        @dataclass
        class BareDictConfig(ConfigBase):
            mapping: Dict

        self.assertEqual(ConfigBase._convert_field(["1", 2], list[int]), [1, 2])
        with self.assertRaisesRegex(TypeError, "Expected a dictionary for NestedConfig"):
            ConfigBase._convert_field([], NestedConfig)
        with self.assertRaisesRegex(TypeError, "Expected an list"):
            ConfigBase._convert_field("not-list", list[int])
        with self.assertRaisesRegex(TypeError, "Expected a dictionary"):
            ConfigBase._convert_field([], dict[str, int])
        with self.assertRaisesRegex(TypeError, "two type arguments"):
            BareDictConfig.from_dict({"mapping": {}})
        self.assertIsNone(ConfigBase._convert_field(None, int | None))
        with self.assertRaisesRegex(TypeError, "Cannot convert value to any type in Union"):
            ConfigBase._convert_field("not-int", int | None)
        with self.assertRaisesRegex(TypeError, "Cannot convert str to int"):
            ConfigBase._convert_field("abc", int)
        with patch("src.config.config_base.get_origin", return_value=type(None)):
            self.assertIsNone(ConfigBase._convert_field(None, object))

    def test_from_dict_converts_variadic_tuple_fields(self) -> None:
        @dataclass
        class VariadicTupleConfig(ConfigBase):
            names: tuple[str, ...]

        config = VariadicTupleConfig.from_dict({"names": ["alpha", "beta", "gamma"]})

        self.assertEqual(config.names, ("alpha", "beta", "gamma"))

    def test_string_representation_includes_dataclass_fields(self) -> None:
        config = NestedConfig(name="inner", count=2)

        self.assertEqual(str(config), "NestedConfig(name='inner', count=2)")

    def test_removed_visual_style_is_ignored_when_loading_legacy_personality_config(self) -> None:
        config = PersonalityConfig.from_dict(
            {
                "personality": "测试人格",
                "visual_style": "旧识图规则",
                "multiple_reply_style": ["旧可选风格"],
                "multiple_probability": 1.0,
                "plan_style": "旧 action 规则",
                "states": ["旧随机人格"],
                "state_probability": 1.0,
            }
        )

        self.assertFalse(hasattr(config, "visual_style"))
        self.assertFalse(hasattr(config, "multiple_reply_style"))
        self.assertFalse(hasattr(config, "multiple_probability"))
        self.assertFalse(hasattr(config, "plan_style"))
        self.assertFalse(hasattr(config, "states"))
        self.assertFalse(hasattr(config, "state_probability"))


class EmojiConfigTest(unittest.TestCase):
    def test_usage_scene_defaults_and_bounds(self) -> None:
        config = EmojiConfig()

        self.assertTrue(config.vector_selection_enabled)
        self.assertTrue(config.usage_scene_enabled)
        self.assertEqual(config.usage_scene_context_messages, 8)
        self.assertEqual(config.usage_scene_max_scenes, 8)
        self.assertEqual(config.usage_scene_weight, 0.6)
        self.assertEqual(config.selection_candidate_count, 8)

        invalid_values = (
            {"usage_scene_context_messages": 0},
            {"usage_scene_context_messages": 33},
            {"usage_scene_max_scenes": 0},
            {"usage_scene_max_scenes": 33},
            {"usage_scene_weight": -0.1},
            {"usage_scene_weight": 1.1},
            {"selection_candidate_count": 0},
            {"selection_candidate_count": 31},
        )
        for values in invalid_values:
            with self.subTest(values=values), self.assertRaises(ValueError):
                EmojiConfig(**values)


class ApiAdaConfigTest(unittest.TestCase):
    def test_api_provider_validates_required_credentials_and_allows_gemini_without_base_url(self) -> None:
        provider = APIProvider(name="gemini-provider", base_url="", api_key="secret", client_type="gemini")

        self.assertEqual(provider.get_api_key(), "secret")
        with self.assertRaisesRegex(ValueError, "API密钥不能为空"):
            APIProvider(name="openai", base_url="https://api.example.test", api_key="")
        with self.assertRaisesRegex(ValueError, "API基础URL不能为空"):
            APIProvider(name="openai", base_url="", api_key="secret")
        with self.assertRaisesRegex(ValueError, "API提供商名称不能为空"):
            APIProvider(name="", base_url="https://api.example.test", api_key="secret")

    def test_model_info_validates_identity_fields(self) -> None:
        model = ModelInfo(model_identifier="gpt-test", name="test-model", api_provider="openai")

        self.assertEqual(model.extra_params, {})
        with self.assertRaisesRegex(ValueError, "模型标识符不能为空"):
            ModelInfo(model_identifier="", name="test-model", api_provider="openai")
        with self.assertRaisesRegex(ValueError, "模型名称不能为空"):
            ModelInfo(model_identifier="gpt-test", name="", api_provider="openai")
        with self.assertRaisesRegex(ValueError, "API提供商不能为空"):
            ModelInfo(model_identifier="gpt-test", name="test-model", api_provider="")

    def test_model_task_config_get_task_returns_named_task_or_raises(self) -> None:
        task = TaskConfig(model_list=["model-a"], max_tokens=256, temperature=0.7)
        config = ModelTaskConfig(
            utils=TaskConfig(),
            replyer=task,
            vlm=TaskConfig(),
            voice=TaskConfig(),
            tool_use=TaskConfig(),
            planner=TaskConfig(),
            embedding=TaskConfig(),
        )

        self.assertIs(config.get_task("replyer"), task)
        self.assertEqual(config.memory_encoder.model_list, [])
        with self.assertRaisesRegex(ValueError, "任务 'unknown' 未找到对应的配置"):
            config.get_task("unknown")


class OfficialConfigTest(unittest.TestCase):
    def test_webui_config_has_a_single_backend_managed_plugin_registry(self) -> None:
        config = WebUIConfig()

        self.assertEqual(
            config.plugin_registry_url,
            "https://raw.githubusercontent.com/hsd221/RiyaBot-Plugins-Registry/main/registry.json",
        )

    def test_message_receive_config_from_dict_converts_lists_to_sets(self) -> None:
        config = MessageReceiveConfig.from_dict({"ban_words": ["bad", "bad"], "ban_msgs_regex": ["^spam"]})

        self.assertEqual(config.ban_words, {"bad"})
        self.assertEqual(config.ban_msgs_regex, {"^spam"})

    def test_memory_config_validates_agent_limits(self) -> None:
        self.assertEqual(MemoryConfig(max_agent_iterations=1, agent_timeout_seconds=1.0).max_agent_iterations, 1)
        with self.assertRaisesRegex(ValueError, "max_agent_iterations 必须至少为1"):
            MemoryConfig(max_agent_iterations=0)
        with self.assertRaisesRegex(ValueError, "agent_timeout_seconds 必须大于0"):
            MemoryConfig(agent_timeout_seconds=0)

    def test_chat_config_time_ranges_and_talk_value_rules(self) -> None:
        config = ChatConfig(
            talk_value=0,
            talk_value_rules=[
                {"target": "", "time": "08:00-10:00", "value": 0},
                {"target": "", "time": "10:01-11:00", "value": "0.5"},
                {"target": "", "time": "bad", "value": 1},
                "not-a-rule",
            ],
        )
        config._now_minutes = lambda: 8 * 60 + 30

        self.assertEqual(config._parse_range("23:00-02:30"), (1380, 150))
        self.assertTrue(config._in_range(30, 1380, 150))
        self.assertFalse(config._in_range(600, 1380, 150))
        self.assertEqual(config.get_talk_value(chat_id=None), 0.0000001)

        config._now_minutes = lambda: 10 * 60 + 30
        self.assertEqual(config.get_talk_value(chat_id=None), 0.5)

        config.enable_talk_value_rules = False
        self.assertEqual(config.get_talk_value(chat_id=None), 0.0000001)

    def test_chat_config_prefers_specific_rules_and_ignores_invalid_rule_shapes(self) -> None:
        fake_manager = SimpleNamespace(
            get_stream_id=lambda platform, raw_id, is_group: f"{platform}:{raw_id}:{is_group}"
        )
        config = ChatConfig(
            talk_value=0.25,
            talk_value_rules=[
                {"target": "bad-format", "time": "00:00-23:59", "value": 0.9},
                "not-a-rule",
                {"target": "qq:123:group", "time": "12:00-13:00", "value": "bad"},
                {"target": "qq:123:group", "time": "bad", "value": 0.9},
                {"target": "qq:123:group", "time": "12:00-13:00", "value": 0},
                {"target": "qq:999:private", "time": "12:00-13:00", "value": 0.8},
                {"target": "", "time": 123, "value": 0.7},
                {"target": "", "time": "bad", "value": 0.7},
                {"target": "", "time": "12:00-13:00", "value": 0.6},
            ],
        )
        config._now_minutes = lambda: 12 * 60 + 30

        with patch(
            "src.chat.message_receive.chat_stream.ChatManager.get_stream_id", side_effect=fake_manager.get_stream_id
        ):
            _parse_stream_config_to_chat_id.cache_clear()
            self.assertEqual(config._parse_stream_config_to_chat_id("qq:123:group"), "qq:123:True")
            self.assertEqual(config._parse_stream_config_to_chat_id("qq:999:private"), "qq:999:False")
            self.assertIsNone(config._parse_stream_config_to_chat_id("bad-format"))
            first = ChatConfig(
                talk_value=0.25, talk_value_rules=[{"target": "qq:123:group", "time": "12:00-13:00", "value": 0.75}]
            )
            first._now_minutes = lambda: 12 * 60 + 30
            self.assertEqual(first.get_talk_value("qq:123:True"), 0.75)
            self.assertEqual(config.get_talk_value("qq:123:True"), 0.0000001)
            self.assertEqual(config.get_talk_value("missing-chat"), 0.6)

        raising_manager = SimpleNamespace(
            get_stream_id=lambda platform, raw_id, is_group: (_ for _ in ()).throw(ValueError("bad stream"))
        )
        with patch(
            "src.chat.message_receive.chat_stream.ChatManager.get_stream_id", side_effect=raising_manager.get_stream_id
        ):
            _parse_stream_config_to_chat_id.cache_clear()
            self.assertIsNone(config._parse_stream_config_to_chat_id("qq:123:group"))

        config._parse_stream_config_to_chat_id = lambda _target: None
        self.assertEqual(config.get_talk_value("qq:123:True"), 0.6)

        fallback = ChatConfig(talk_value=0.25, talk_value_rules=[{"target": "", "time": "00:00-01:00", "value": "bad"}])
        fallback._now_minutes = lambda: 30
        self.assertEqual(fallback.get_talk_value(None), 0.25)

        no_rule = ChatConfig(talk_value=0.25, talk_value_rules=[])
        self.assertEqual(no_rule.get_talk_value(None), 0.25)
        zero_fallback = ChatConfig(
            talk_value=0, talk_value_rules=[{"target": "", "time": "00:00-01:00", "value": "bad"}]
        )
        zero_fallback._now_minutes = lambda: 30
        self.assertEqual(zero_fallback.get_talk_value(None), 0.0000001)

        with patch.object(time, "localtime", return_value=SimpleNamespace(tm_hour=3, tm_min=4)):
            self.assertEqual(ChatConfig()._now_minutes(), 184)

    def test_expression_config_resolves_specific_global_defaults_and_invalid_items(self) -> None:
        fake_manager = SimpleNamespace(
            get_stream_id=lambda platform, raw_id, is_group: f"{platform}:{raw_id}:{is_group}"
        )
        config = ExpressionConfig(
            learning_list=[
                [],
                ["too-short"],
                ["bad-format", "enable", "enable", "enable"],
                ["qq:123:group", "disable", "enable", "disable"],
                ["", "enable", "disable", "enable"],
            ]
        )

        self.assertTrue(ExpressionConfig().vector_selection_enabled)
        self.assertEqual(ExpressionConfig().get_expression_config_for_chat(), (True, True, True))
        with patch(
            "src.chat.message_receive.chat_stream.ChatManager.get_stream_id", side_effect=fake_manager.get_stream_id
        ):
            _parse_stream_config_to_chat_id.cache_clear()
            self.assertEqual(config._parse_stream_config_to_chat_id("qq:123:group"), "qq:123:True")
            self.assertIsNone(config._parse_stream_config_to_chat_id("bad-format"))
            self.assertEqual(config.get_expression_config_for_chat("qq:123:True"), (False, True, False))
            self.assertEqual(config.get_expression_config_for_chat("missing"), (True, False, True))

        raising_manager = SimpleNamespace(
            get_stream_id=lambda platform, raw_id, is_group: (_ for _ in ()).throw(ValueError("bad stream"))
        )
        with patch(
            "src.chat.message_receive.chat_stream.ChatManager.get_stream_id", side_effect=raising_manager.get_stream_id
        ):
            _parse_stream_config_to_chat_id.cache_clear()
            self.assertIsNone(config._parse_stream_config_to_chat_id("qq:123:group"))

        no_match = ExpressionConfig(learning_list=[["bad-format", "enable", "enable", "enable"]])
        self.assertEqual(no_match.get_expression_config_for_chat("missing"), (True, True, True))

        class LowerRaises:
            def lower(self):
                raise ValueError("bad value")

        broken_specific = ExpressionConfig(learning_list=[["qq:123:group", LowerRaises(), "enable", "enable"]])
        broken_specific._parse_stream_config_to_chat_id = lambda _target: "chat-1"
        self.assertIsNone(broken_specific._get_stream_specific_config("chat-1"))
        broken_global = ExpressionConfig(learning_list=[["", LowerRaises(), "enable", "enable"]])
        self.assertIsNone(broken_global._get_global_config())

    def test_behavior_config_resolves_specific_global_defaults_and_invalid_items(self) -> None:
        fake_manager = SimpleNamespace(
            get_stream_id=lambda platform, raw_id, is_group: f"{platform}:{raw_id}:{is_group}"
        )
        config = BehaviorConfig(
            learning_list=[
                [],
                ["too-short"],
                ["bad-format", "enable", "disable"],
                ["qq:123:private", "disable", "enable"],
                ["", "enable", "disable"],
            ]
        )

        self.assertEqual(BehaviorConfig(learning_list=[]).get_behavior_config_for_chat(), (True, True))
        with patch(
            "src.chat.message_receive.chat_stream.ChatManager.get_stream_id", side_effect=fake_manager.get_stream_id
        ):
            _parse_stream_config_to_chat_id.cache_clear()
            self.assertEqual(config._parse_stream_config_to_chat_id("qq:123:private"), "qq:123:False")
            self.assertIsNone(config._parse_stream_config_to_chat_id("bad-format"))
            self.assertEqual(config.get_behavior_config_for_chat("qq:123:False"), (False, True))
            self.assertEqual(config.get_behavior_config_for_chat("missing"), (True, False))

        raising_manager = SimpleNamespace(
            get_stream_id=lambda platform, raw_id, is_group: (_ for _ in ()).throw(ValueError("bad stream"))
        )
        with patch(
            "src.chat.message_receive.chat_stream.ChatManager.get_stream_id", side_effect=raising_manager.get_stream_id
        ):
            _parse_stream_config_to_chat_id.cache_clear()
            self.assertIsNone(config._parse_stream_config_to_chat_id("qq:123:private"))

        no_match = BehaviorConfig(learning_list=[["bad-format", "enable", "disable"]])
        self.assertEqual(no_match.get_behavior_config_for_chat("missing"), (True, True))

        class LowerRaises:
            def lower(self):
                raise ValueError("bad value")

        broken_specific = BehaviorConfig(learning_list=[["qq:123:private", LowerRaises(), "enable"]])
        broken_specific._parse_stream_config_to_chat_id = lambda _target: "chat-1"
        self.assertIsNone(broken_specific._get_stream_specific_config("chat-1"))
        broken_global = BehaviorConfig(learning_list=[["", LowerRaises(), "enable"]])
        self.assertIsNone(broken_global._get_global_config())

    def test_keyword_configs_validate_required_fields_regex_and_rule_types(self) -> None:
        rule = KeywordRuleConfig(keywords=["hello"], reaction="hi")
        regex_rule = KeywordRuleConfig(regex=[r"^hi"], reaction="hello")
        config = KeywordReactionConfig(keyword_rules=[rule], regex_rules=[regex_rule])

        self.assertIs(config.keyword_rules[0], rule)
        with self.assertRaisesRegex(ValueError, "至少包含keywords或regex"):
            KeywordRuleConfig(reaction="hi")
        with self.assertRaisesRegex(ValueError, "必须包含reaction"):
            KeywordRuleConfig(keywords=["hello"])
        with self.assertRaisesRegex(ValueError, "无效的正则表达式"):
            KeywordRuleConfig(regex=["("], reaction="bad")
        with self.assertRaisesRegex(ValueError, "规则必须是KeywordRuleConfig类型"):
            KeywordReactionConfig(keyword_rules=[object()])


if __name__ == "__main__":
    unittest.main()
