import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from playhouse.sqlite_ext import SqliteExtDatabase

from src.bw_learner import behavior_learner, learner_utils, message_recorder
from src.bw_learner.behavior_learner import BehaviorLearner, parse_behavior_response
from src.bw_learner.behavior_selector import BehaviorSelector
from src.bw_learner.behavior_store import BehaviorPatternStore
from src.common.database.database_model import BehaviorPattern


_DEFAULT_STREAM = object()


class LearnerUtilsTest(unittest.TestCase):
    def test_content_filter_similarity_and_chat_id_helpers_handle_legacy_and_json_shapes(self) -> None:
        content = "[回复Alice]，说： hi @<Mai:10000> [picid:abc] [表情包：cat] 正文"

        self.assertEqual(learner_utils.filter_message_content(content), "hi    正文")
        self.assertEqual(learner_utils.filter_message_content(None), "")
        self.assertEqual(learner_utils.calculate_similarity("abc", "abc"), 1.0)
        self.assertEqual(learner_utils.calculate_style_similarity("使用反问句式", "反问"), 1.0)
        self.assertEqual(learner_utils.calculate_style_similarity("", "反问"), 0.0)
        self.assertEqual(learner_utils.parse_chat_id_list("chat-a"), [["chat-a", 1]])
        self.assertEqual(learner_utils.parse_chat_id_list('"chat-a"'), [["chat-a", 1]])
        self.assertEqual(learner_utils.parse_chat_id_list('[["chat-a", 2]]'), [["chat-a", 2]])
        self.assertEqual(learner_utils.parse_chat_id_list([["chat-b", 3]]), [["chat-b", 3]])

        chat_ids = learner_utils.update_chat_id_list([["chat-a", "bad"], ["chat-b"]], "chat-a", increment=2)
        learner_utils.update_chat_id_list(chat_ids, "chat-b", increment=3)
        learner_utils.update_chat_id_list(chat_ids, "chat-c", increment=4)

        self.assertEqual(chat_ids, [["chat-a", 2], ["chat-b", 3], ["chat-c", 4]])
        self.assertTrue(learner_utils.chat_id_list_contains(chat_ids, "chat-c"))
        self.assertFalse(learner_utils.chat_id_list_contains(chat_ids, "missing"))

    def test_weighted_sample_uses_count_scaled_weights_without_replacement(self) -> None:
        population = [
            {"id": "low", "count": 1},
            {"id": "mid", "count": 3},
            {"id": "high", "count": 5},
        ]

        with patch.object(learner_utils.random, "uniform", side_effect=[0.1, 3.5]):
            selected = learner_utils.weighted_sample(population, 2)

        self.assertEqual([item["id"] for item in selected], ["low", "high"])
        self.assertEqual([item["id"] for item in population], ["low", "mid", "high"])
        self.assertEqual(learner_utils.weighted_sample(population, 0), [])
        self.assertEqual(learner_utils.weighted_sample(population[:1], 5), population[:1])

    def test_bot_name_and_message_identity_use_configured_platform_accounts(self) -> None:
        fake_config = SimpleNamespace(
            bot=SimpleNamespace(
                nickname="Mai",
                alias_names=["麦麦"],
                qq_account="10000",
                platforms=["telegram:tg-bot", "wx:wx-bot"],
            )
        )

        with patch.object(learner_utils, "global_config", fake_config):
            self.assertTrue(learner_utils.contains_bot_self_name("麦麦在吗"))
            self.assertFalse(learner_utils.contains_bot_self_name("普通消息"))
            self.assertTrue(learner_utils.is_bot_message(SimpleNamespace(user_platform="qq", user_id="10000")))
            self.assertTrue(
                learner_utils.is_bot_message(
                    SimpleNamespace(user_info=SimpleNamespace(platform="telegram", user_id="tg-bot"))
                )
            )
            self.assertTrue(learner_utils.is_bot_message(SimpleNamespace(user_platform="wx", user_id="wx-bot")))
            self.assertFalse(learner_utils.is_bot_message(SimpleNamespace(user_platform="qq", user_id="other")))
            self.assertFalse(learner_utils.is_bot_message(None))

    def test_build_context_paragraph_validates_window_and_handles_builder_errors(self) -> None:
        messages = [SimpleNamespace(id=i) for i in range(6)]

        with patch.object(learner_utils, "build_readable_messages", return_value=" context "):
            self.assertEqual(learner_utils.build_context_paragraph(messages, 2), "context")

        self.assertIsNone(learner_utils.build_context_paragraph(messages, -1))
        self.assertIsNone(learner_utils.build_context_paragraph([], 0))

        with patch.object(learner_utils, "build_readable_messages", side_effect=RuntimeError("bad message")):
            self.assertIsNone(learner_utils.build_context_paragraph(messages, 2))

    def test_parse_expression_response_extracts_expression_and_jargon_items_from_json_blocks(self) -> None:
        response = """
        ```json
        [
          {"situation": "被问配置", "style": "先确认上下文", "source_id": "1"},
          {"content": "赛博夜宵", "source_id": "2"},
          "ignored"
        ]
        ```
        """

        expressions, jargon = learner_utils.parse_expression_response(response)
        dict_expression, dict_jargon = learner_utils.parse_expression_response(
            '{"situation": "打招呼", "style": "短句回应", "source_id": "3"}'
        )

        self.assertEqual(expressions, [("被问配置", "先确认上下文", "1")])
        self.assertEqual(jargon, [("赛博夜宵", "2")])
        self.assertEqual(dict_expression, [("打招呼", "短句回应", "3")])
        self.assertEqual(dict_jargon, [])
        self.assertEqual(learner_utils.parse_expression_response(""), ([], []))


class BehaviorLearningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.test_db = SqliteExtDatabase(str(Path(self.tmpdir.name) / "behavior.db"))
        self.original_db = BehaviorPattern._meta.database
        self.test_db.bind([BehaviorPattern], bind_refs=False, bind_backrefs=False)
        self.test_db.connect()
        self.test_db.create_tables([BehaviorPattern])

    def tearDown(self) -> None:
        self.test_db.drop_tables([BehaviorPattern])
        self.test_db.close()
        BehaviorPattern._meta.set_database(self.original_db)
        self.tmpdir.cleanup()

    def test_parse_behavior_response_accepts_upstream_json_shape(self) -> None:
        response = """
        ```json
        [
          {
            "actor_type": "other_user",
            "learning_type": "observed_behavior",
            "action": "先短句共情，再给一个可执行的小建议",
            "outcome": "对方继续补充细节",
            "source_ids": ["2", "4"]
          },
          {
            "actor_type": "unknown",
            "learning_type": "observed_behavior",
            "action": "一次性查询某个配置值",
            "outcome": "得到结果",
            "source_ids": []
          }
        ]
        ```
        """

        patterns = parse_behavior_response(response)

        self.assertEqual(len(patterns), 1)
        self.assertEqual(patterns[0].actor_type, "other_user")
        self.assertEqual(patterns[0].learning_type, "observed_behavior")
        self.assertEqual(patterns[0].source_ids, ["2", "4"])

    def test_store_merges_similar_action_and_outcome(self) -> None:
        store = BehaviorPatternStore(similarity_threshold=0.8)
        now = time.time()

        first = store.upsert_pattern(
            chat_id="chat-a",
            actor_type="other_user",
            learning_type="observed_behavior",
            action="先确认关键配置，再给出排查步骤",
            outcome="对方补充信息，排查方向变明确",
            source_text="alice: 怎么连不上\nbob: 先看端口",
            source_ids=["1", "2"],
            current_time=now,
        )
        second = store.upsert_pattern(
            chat_id="chat-a",
            actor_type="other_user",
            learning_type="observed_behavior",
            action="先确认关键配置，再给出排查步骤",
            outcome="对方补充信息，排查方向变明确",
            source_text="alice: 还是连不上\nbob: 看下端口",
            source_ids=["3", "4"],
            current_time=now + 1,
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(BehaviorPattern.select().count(), 1)
        stored = BehaviorPattern.get_by_id(first.id)
        self.assertEqual(stored.count, 2)

    def test_selector_returns_context_relevant_reference(self) -> None:
        now = time.time()
        BehaviorPattern.create(
            chat_id="chat-a",
            actor_type="other_user",
            learning_type="observed_behavior",
            action="先确认关键配置，再给出排查步骤",
            outcome="对方补充信息，排查方向变明确",
            source_text="排查端口配置",
            source_ids='["1"]',
            count=3,
            score=1.0,
            last_active_time=now,
            create_date=now,
        )
        BehaviorPattern.create(
            chat_id="chat-a",
            actor_type="group_collective",
            learning_type="observed_behavior",
            action="顺着玩笑接梗，保持轻松语气",
            outcome="话题转向轻松闲聊",
            source_text="群友互相开玩笑",
            source_ids='["2"]',
            count=1,
            score=1.0,
            last_active_time=now,
            create_date=now,
        )

        block, selected_ids = BehaviorSelector().build_reference_block(
            "chat-a",
            "用户说接口连不上，怀疑是端口配置问题",
            max_num=1,
        )

        self.assertIn("行为参考", block)
        self.assertIn("确认关键配置", block)
        self.assertNotIn("顺着玩笑接梗", block)
        self.assertEqual(len(selected_ids), 1)


class BehaviorLearnerPromptTest(unittest.IsolatedAsyncioTestCase):
    async def test_behavior_learning_requests_timestamped_anonymous_chat(self) -> None:
        learner = object.__new__(BehaviorLearner)
        learner.chat_id = "chat-a"
        learner.chat_name = "Test Chat"
        learner.behavior_model = SimpleNamespace(
            generate_response_async=AsyncMock(return_value=("[]", None)),
        )
        messages = [SimpleNamespace(processed_plain_text="先确认一下配置")]
        fake_config = SimpleNamespace(bot=SimpleNamespace(nickname="Mai"))

        with (
            patch.object(behavior_learner, "global_config", fake_config),
            patch.object(
                behavior_learner,
                "build_anonymous_messages",
                new=AsyncMock(return_value="[1] [2026-07-31 12:00:00] A说 先确认一下配置"),
            ) as build_chat,
        ):
            result = await learner._learn_and_store_locked(messages)

        self.assertEqual(result, [])
        build_chat.assert_awaited_once_with(messages, show_ids=True, show_timestamps=True)
        prompt = learner.behavior_model.generate_response_async.await_args.args[0]
        self.assertIn("[2026-07-31 12:00:00]", prompt)
        self.assertIn("时间间隔", prompt)


class MessageRecorderTest(unittest.IsolatedAsyncioTestCase):
    def build_recorder(
        self, *, stream=_DEFAULT_STREAM, expression_enabled=True, jargon_enabled=False, behavior_enabled=True
    ):
        if stream is _DEFAULT_STREAM:
            stream = object()
        manager = SimpleNamespace(
            get_stream=Mock(return_value=stream),
            get_stream_name=Mock(return_value="Test Chat"),
        )
        expression_config = SimpleNamespace(
            get_expression_config_for_chat=Mock(return_value=(None, expression_enabled, jargon_enabled))
        )
        behavior_config = SimpleNamespace(get_behavior_config_for_chat=Mock(return_value=(None, behavior_enabled)))
        fake_config = SimpleNamespace(expression=expression_config, behavior=behavior_config)
        expression_manager = SimpleNamespace(get_expression_learner=Mock(return_value=SimpleNamespace()))
        miner_manager = SimpleNamespace(get_miner=Mock(return_value=SimpleNamespace()))
        behavior_manager = SimpleNamespace(get_behavior_learner=Mock(return_value=SimpleNamespace()))

        patches = [
            patch.object(message_recorder, "get_chat_manager", return_value=manager),
            patch.object(message_recorder, "global_config", fake_config),
            patch.object(message_recorder, "expression_learner_manager", expression_manager),
            patch.object(message_recorder, "miner_manager", miner_manager),
            patch.object(message_recorder, "behavior_learner_manager", behavior_manager),
            patch.object(message_recorder.time, "time", return_value=1000.0),
        ]

        for patcher in patches:
            patcher.__enter__()
        try:
            recorder = message_recorder.MessageRecorder("chat-1")
        finally:
            for patcher in reversed(patches):
                patcher.__exit__(None, None, None)

        return (
            recorder,
            manager,
            expression_config,
            behavior_config,
            expression_manager,
            miner_manager,
            behavior_manager,
        )

    def test_message_recorder_initializes_stream_config_and_learning_instances(self) -> None:
        recorder, manager, expression_config, behavior_config, expression_manager, miner_manager, behavior_manager = (
            self.build_recorder(expression_enabled=True, jargon_enabled=True, behavior_enabled=False)
        )

        self.assertEqual(recorder.chat_id, "chat-1")
        self.assertEqual(recorder.chat_name, "Test Chat")
        self.assertEqual(recorder.last_extraction_time, 1000.0)
        self.assertTrue(recorder.enable_expression_learning)
        self.assertTrue(recorder.enable_jargon_learning)
        self.assertFalse(recorder.enable_behavior_learning)
        manager.get_stream.assert_called_once_with("chat-1")
        manager.get_stream_name.assert_called_once_with("chat-1")
        expression_config.get_expression_config_for_chat.assert_called_once_with("chat-1")
        behavior_config.get_behavior_config_for_chat.assert_called_once_with("chat-1")
        expression_manager.get_expression_learner.assert_called_once_with("chat-1")
        miner_manager.get_miner.assert_called_once_with("chat-1")
        behavior_manager.get_behavior_learner.assert_called_once_with("chat-1")

    def test_should_trigger_extraction_requires_interval_and_minimum_message_count(self) -> None:
        recorder, *_ = self.build_recorder()
        recorder.last_extraction_time = 900.0
        recorder.min_extraction_interval = 60
        recorder.min_messages_for_extraction = 3

        with patch.object(message_recorder.time, "time", return_value=950.0):
            self.assertFalse(recorder.should_trigger_extraction())

        with (
            patch.object(message_recorder.time, "time", return_value=1000.0),
            patch.object(message_recorder, "get_raw_msg_by_timestamp_with_chat_inclusive", return_value=[1, 2]),
        ):
            self.assertFalse(recorder.should_trigger_extraction())

        with (
            patch.object(message_recorder.time, "time", return_value=1000.0),
            patch.object(message_recorder, "get_raw_msg_by_timestamp_with_chat_inclusive", return_value=[1, 2, 3]),
        ):
            self.assertTrue(recorder.should_trigger_extraction())

    async def test_extract_and_distribute_sorts_messages_updates_time_and_schedules_enabled_learners(self) -> None:
        recorder, *_ = self.build_recorder(expression_enabled=True, behavior_enabled=True)
        recorder.last_extraction_time = 100.0
        first = SimpleNamespace(time=2.0)
        second = SimpleNamespace(time=1.0)
        recorder._trigger_expression_learning = AsyncMock()
        recorder._trigger_behavior_learning = AsyncMock()
        created_tasks = []

        def fake_create_task(coro, **_kwargs):
            created_tasks.append(coro)
            coro.close()
            return SimpleNamespace(cancel=Mock())

        with (
            patch.object(recorder, "should_trigger_extraction", return_value=True),
            patch.object(message_recorder.time, "time", return_value=200.0),
            patch.object(
                message_recorder,
                "get_raw_msg_by_timestamp_with_chat_inclusive",
                return_value=[first, second],
            ) as get_messages,
            patch.object(message_recorder, "spawn_background_task", side_effect=fake_create_task) as create_task,
        ):
            await recorder.extract_and_distribute()

        self.assertEqual(recorder.last_extraction_time, 200.0)
        get_messages.assert_called_once_with(chat_id="chat-1", timestamp_start=100.0, timestamp_end=200.0)
        self.assertEqual(recorder._trigger_expression_learning.call_args.args[0], [second, first])
        self.assertEqual(recorder._trigger_behavior_learning.call_args.args[0], [second, first])
        self.assertEqual(create_task.call_count, 2)
        self.assertEqual(len(created_tasks), 2)

    async def test_extract_and_distribute_skips_missing_stream_and_manager_caches_recorders(self) -> None:
        recorder, *_ = self.build_recorder(stream=None)

        with patch.object(recorder, "should_trigger_extraction", return_value=True):
            await recorder.extract_and_distribute()

        manager = message_recorder.MessageRecorderManager()
        fake_recorder = object()
        with patch.object(message_recorder, "MessageRecorder", return_value=fake_recorder) as recorder_cls:
            self.assertIs(manager.get_recorder("chat-1"), fake_recorder)
            self.assertIs(manager.get_recorder("chat-1"), fake_recorder)

        recorder_cls.assert_called_once_with("chat-1")

    async def test_trigger_learning_helpers_delegate_and_suppress_learner_errors(self) -> None:
        recorder, *_ = self.build_recorder()
        messages = [SimpleNamespace(time=1.0)]
        recorder.expression_learner = SimpleNamespace(learn_and_store=AsyncMock(return_value={"style": "short"}))
        recorder.behavior_learner = SimpleNamespace(learn_and_store=AsyncMock(side_effect=RuntimeError("bad llm")))

        await recorder._trigger_expression_learning(messages)
        with patch("traceback.print_exc") as print_exc:
            await recorder._trigger_behavior_learning(messages)

        recorder.expression_learner.learn_and_store.assert_awaited_once_with(messages=messages)
        recorder.behavior_learner.learn_and_store.assert_awaited_once_with(messages=messages)
        print_exc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
