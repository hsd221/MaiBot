import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.bw_learner import expression_learner, expression_selector
from src.bw_learner.expression_learner import ExpressionLearner
from src.bw_learner.expression_selector import ExpressionSelector
from src.config.official_configs import ExpressionConfig


class ExpressionSelectorCandidateTest(unittest.IsolatedAsyncioTestCase):
    def make_selector(self) -> ExpressionSelector:
        selector = object.__new__(ExpressionSelector)
        selector.llm_model = SimpleNamespace(generate_response_async=AsyncMock())
        selector.update_expressions_last_active_time = Mock()
        return selector

    def test_build_vector_query_text_prioritizes_reason_target_and_recent_chat_tail(self) -> None:
        long_chat = "A" * 3100

        query = ExpressionSelector._build_vector_query_text(
            chat_info=long_chat,
            target_message="  需要回复的消息  ",
            reply_reason="  需要解释上下文  ",
        )

        self.assertIn("回复理由：\n需要解释上下文", query)
        self.assertIn("目标消息：\n需要回复的消息", query)
        self.assertIn("最近聊天：\n", query)
        self.assertNotIn("A" * 3100, query)
        self.assertEqual(ExpressionSelector._build_vector_query_text("", None, None), "")

    async def test_select_from_candidate_pool_accepts_only_valid_llm_indices_and_updates_selected_rows(self) -> None:
        selector = self.make_selector()
        selector.llm_model.generate_response_async.return_value = ('{"selected_situations": [2, 99, "bad", 1]}', None)
        candidates = [
            {"id": 10, "situation": "问候", "style": "短句回应"},
            {"id": 20, "situation": "被追问", "style": "先确认问题"},
        ]
        prompt_template = (
            "{bot_name}|{chat_observe_info}|{all_situations}|{max_num}|"
            "{target_message}|{target_message_extra_block}|{reply_reason_block}"
        )
        prompt_manager = SimpleNamespace(
            format_prompt=Mock(side_effect=lambda _name, **kwargs: prompt_template.format(**kwargs))
        )
        fake_config = SimpleNamespace(bot=SimpleNamespace(nickname="Mai"))

        with (
            patch.object(expression_selector, "prompt_manager", prompt_manager),
            patch.object(expression_selector, "global_config", fake_config),
        ):
            selected, selected_ids = await selector._select_from_candidate_pool(
                candidates,
                chat_info="最近聊天",
                max_num=2,
                target_message="目标消息",
                reply_reason=None,
            )

        self.assertEqual(selected_ids, [20, 10])
        self.assertEqual([item["style"] for item in selected], ["先确认问题", "短句回应"])
        selector.update_expressions_last_active_time.assert_called_once_with(selected)
        prompt = selector.llm_model.generate_response_async.await_args.kwargs["prompt"]
        self.assertIn("现在你想要对这条消息进行回复", prompt)
        self.assertIn("1.当 问候 时，使用 短句回应", prompt)

    async def test_select_from_candidate_pool_returns_empty_for_empty_or_invalid_llm_response(self) -> None:
        selector = self.make_selector()
        self.assertEqual(await selector._select_from_candidate_pool([], "", 2, None, None), ([], []))

        selector.llm_model.generate_response_async.return_value = ('{"selected_situations": "bad"}', None)
        prompt_manager = SimpleNamespace(
            format_prompt=Mock(side_effect=lambda _name, **kwargs: "{reply_reason_block}".format(**kwargs))
        )
        fake_config = SimpleNamespace(bot=SimpleNamespace(nickname="Mai"))

        with (
            patch.object(expression_selector, "prompt_manager", prompt_manager),
            patch.object(expression_selector, "global_config", fake_config),
        ):
            selected = await selector._select_from_candidate_pool(
                [{"id": 1, "situation": "问候", "style": "短句"}],
                chat_info="ignored when reason exists",
                max_num=1,
                target_message=None,
                reply_reason="需要解释",
            )

        self.assertEqual(selected, ([], []))
        self.assertIn(
            "你的回复理由是：需要解释", selector.llm_model.generate_response_async.await_args.kwargs["prompt"]
        )
        selector.update_expressions_last_active_time.assert_not_called()

    async def test_vector_selection_handles_none_empty_simple_and_llm_selection_paths(self) -> None:
        selector = self.make_selector()
        selector._load_all_expression_candidates = Mock(return_value=[{"id": i, "style": f"s{i}"} for i in range(1, 5)])
        selector._select_from_candidate_pool = AsyncMock(return_value=([{"id": 3}], [3]))

        with patch.object(
            expression_selector.expression_vector_index, "select_candidates", new=AsyncMock(return_value=None)
        ):
            self.assertIsNone(await selector._select_expressions_vector("chat-a", "chat", 5, None, None, 1))

        with patch.object(
            expression_selector.expression_vector_index, "select_candidates", new=AsyncMock(return_value=[])
        ):
            self.assertEqual(await selector._select_expressions_vector("chat-a", "chat", 5, None, None, 1), ([], []))

        vector_candidates = [{"id": 1}, {"id": 2}, {"id": 3}]
        with patch.object(
            expression_selector.expression_vector_index,
            "select_candidates",
            new=AsyncMock(return_value=vector_candidates),
        ) as select_candidates:
            self.assertEqual(
                await selector._select_expressions_vector("chat-a", "chat", 2, "target", "reason", 0),
                ([{"id": 1}, {"id": 2}], [1, 2]),
            )

        select_candidates.assert_awaited()
        selector.update_expressions_last_active_time.assert_called_once_with([{"id": 1}, {"id": 2}])

        with patch.object(
            expression_selector.expression_vector_index,
            "select_candidates",
            new=AsyncMock(return_value=vector_candidates),
        ):
            self.assertEqual(
                await selector._select_expressions_vector("chat-a", "chat", 2, None, None, 1), ([{"id": 3}], [3])
            )

        selector._select_from_candidate_pool.assert_awaited_once_with(vector_candidates, "chat", 2, None, None)


class ExpressionLearnerFilteringTest(unittest.IsolatedAsyncioTestCase):
    def make_learner(self) -> ExpressionLearner:
        learner = object.__new__(ExpressionLearner)
        learner.chat_id = "chat-a"
        return learner

    def test_parse_content_list_accepts_only_json_string_lists(self) -> None:
        learner = self.make_learner()

        self.assertEqual(learner._parse_content_list(None), [])
        self.assertEqual(learner._parse_content_list("{bad json"), [])
        self.assertEqual(learner._parse_content_list('{"not": "a list"}'), [])
        self.assertEqual(learner._parse_content_list('["问候", 1, "追问"]'), ["问候", "追问"])

    def test_filter_expressions_drops_invalid_sources_bot_messages_and_blocked_content(self) -> None:
        learner = self.make_learner()
        messages = [
            SimpleNamespace(processed_plain_text="你好，最近怎么样", marker="human"),
            SimpleNamespace(processed_plain_text="机器人自己的回复", marker="bot"),
            SimpleNamespace(processed_plain_text="SELF 不应学习", marker="human"),
            SimpleNamespace(processed_plain_text="这里有 表情：开心", marker="human"),
            SimpleNamespace(processed_plain_text="[图片:abc]", marker="human"),
        ]
        expressions = [
            ("打招呼", "短句回应", "1"),
            ("无效编号", "忽略", "bad"),
            ("越界", "忽略", "99"),
            ("机器人发言", "忽略", "2"),
            ("包含 SELF", "忽略", "3"),
            ("包含表情", "表情：开心", "4"),
            ("包含图片", "忽略 [图片", "5"),
            ("机器人昵称", "Mai", "1"),
        ]
        fake_config = SimpleNamespace(bot=SimpleNamespace(nickname="Mai", alias_names=["麦麦"]))

        with (
            patch.object(expression_learner, "global_config", fake_config),
            patch.object(expression_learner, "is_bot_message", side_effect=lambda msg: msg.marker == "bot"),
        ):
            self.assertEqual(learner._filter_expressions(expressions, messages), [("打招呼", "短句回应")])

    def test_check_cached_jargons_matches_human_messages_with_word_boundaries(self) -> None:
        learner = self.make_learner()
        miner = SimpleNamespace(get_cached_jargons=Mock(return_value=["GPU", "赛博夜宵", ""]))
        messages = [
            SimpleNamespace(processed_plain_text="这块 GPU 太热了", marker="human"),
            SimpleNamespace(processed_plain_text="egpu 外设不应匹配单词边界", marker="human"),
            SimpleNamespace(processed_plain_text="赛博夜宵安排一下", marker="human"),
            SimpleNamespace(processed_plain_text="GPU 自述", marker="bot"),
        ]

        with (
            patch.object(expression_learner.miner_manager, "get_miner", return_value=miner),
            patch.object(expression_learner, "is_bot_message", side_effect=lambda msg: msg.marker == "bot"),
        ):
            self.assertEqual(learner._check_cached_jargons_in_messages(messages), [("GPU", "1"), ("赛博夜宵", "3")])

    async def test_expression_learning_requests_timestamped_anonymous_chat(self) -> None:
        learner = self.make_learner()
        learner.chat_name = "Test Chat"
        learner.express_learn_model = SimpleNamespace(
            generate_response_async=AsyncMock(return_value=("[]", None)),
        )
        messages = [SimpleNamespace(processed_plain_text="这也太离谱了", marker="human")]
        miner = SimpleNamespace(get_cached_jargons=Mock(return_value=[]))
        fake_config = SimpleNamespace(
            bot=SimpleNamespace(nickname="Mai", alias_names=[]),
            expression=ExpressionConfig(learning_list=[["", "enable", "enable", "enable"]]),
        )

        with (
            patch.object(expression_learner, "global_config", fake_config),
            patch.object(expression_learner.miner_manager, "get_miner", return_value=miner),
            patch.object(
                expression_learner,
                "build_anonymous_messages",
                new=AsyncMock(return_value="[1] [2026-07-31 12:00:00] A说 这也太离谱了"),
            ) as build_chat,
        ):
            result = await learner.learn_and_store(messages)

        self.assertEqual(result, [])
        build_chat.assert_awaited_once_with(messages, show_ids=True, show_timestamps=True)
        prompt = learner.express_learn_model.generate_response_async.await_args.args[0]
        self.assertIn("[2026-07-31 12:00:00]", prompt)
        self.assertIn("时间间隔", prompt)

    async def test_process_jargon_entries_filters_invalid_entries_and_delegates_to_miner(self) -> None:
        learner = self.make_learner()
        miner = SimpleNamespace(process_extracted_entries=AsyncMock())
        messages = [
            SimpleNamespace(processed_plain_text="普通上下文", marker="human"),
            SimpleNamespace(processed_plain_text="机器人上下文", marker="bot"),
            SimpleNamespace(processed_plain_text="", marker="human"),
        ]
        entries = [
            ("  GPU  ", "1"),
            ("SELF 黑话", "1"),
            ("Mai黑话", "1"),
            ("无效编号", "bad"),
            ("越界", "9"),
            ("机器人消息", "2"),
            ("空上下文", "3"),
            ("", "1"),
        ]

        with (
            patch.object(expression_learner.miner_manager, "get_miner", return_value=miner),
            patch.object(expression_learner, "contains_bot_self_name", side_effect=lambda text: "Mai" in text),
            patch.object(expression_learner, "is_bot_message", side_effect=lambda msg: msg.marker == "bot"),
            patch.object(expression_learner, "build_context_paragraph", side_effect=["context for GPU", None]),
        ):
            await learner._process_jargon_entries(entries, messages)

        miner.process_extracted_entries.assert_awaited_once_with(
            [{"content": "GPU", "raw_content": ["context for GPU"]}]
        )


if __name__ == "__main__":
    unittest.main()
