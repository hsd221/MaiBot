import unittest
from dataclasses import dataclass
from typing import List, Set

import tomlkit

from src.config.api_ada_configs import APIProvider, ModelTaskConfig, TaskConfig
from src.config.config import get_key_comment
from src.config.config_base import ConfigBase
from src.config.official_configs import BehaviorConfig, BotConfig, ChatConfig, ExpressionConfig


@dataclass
class UntypedCollections(ConfigBase):
    values: List
    tags: Set


class AuditConfigTests(unittest.TestCase):
    def test_provider_string_does_not_disclose_secret(self):
        provider = APIProvider(name="fixture", base_url="https://example.invalid", api_key="fixture-credential")
        self.assertNotIn("fixture-credential", str(provider))
        self.assertNotIn("fixture-credential", str([provider]))

    def test_unparameterized_typing_collections_preserve_values(self):
        config = UntypedCollections.from_dict({"values": [1, "two"], "tags": [1, 1, 2]})
        self.assertEqual(config.values, [1, "two"])
        self.assertEqual(config.tags, {1, 2})

    def test_learning_rules_ignore_invalid_rows_and_fall_back(self):
        for schema, enabled, disabled in (
            (ExpressionConfig, ["enable"] * 3, (False,) * 3),
            (BehaviorConfig, ["enable"] * 2, (False,) * 2),
        ):
            with self.subTest(schema=schema):
                config = schema(learning_list=[["", True, *enabled], ["", *(["disable"] * len(enabled))]])
                self.assertEqual(config._get_global_config(), disabled)
                config.learning_list = [[False, *enabled]]
                self.assertIsNone(config._get_stream_specific_config("unknown"))

    def test_task_lookup_rejects_methods_and_magic_attributes(self):
        task = TaskConfig()
        config = ModelTaskConfig(task, task, task, task, task, task, task)
        for name in ("__dict__", "__init__", "get_task"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                config.get_task(name)
        self.assertIs(config.get_task("replyer"), task)

    def test_bot_defaults_do_not_identify_placeholder_accounts_as_self(self):
        self.assertEqual(BotConfig("qq", "123", "fixture").platforms, [])

    def test_key_comment_uses_actual_item_trivia(self):
        document = tomlkit.parse("[section]\nvalue = 1 # expected\n")
        self.assertEqual(get_key_comment(document["section"], "value"), "# expected")

    def test_stream_parser_handles_non_string(self):
        for config in (ChatConfig(), ExpressionConfig(), BehaviorConfig()):
            self.assertIsNone(config._parse_stream_config_to_chat_id(False))
