import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.bw_learner import expression_learner, message_recorder


class LearningSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def test_expression_and_jargon_switches_control_persistence_independently(self):
        for expression_enabled, jargon_enabled in ((True, False), (False, True), (False, False), (True, True)):
            with self.subTest(expression=expression_enabled, jargon=jargon_enabled):
                learner = object.__new__(expression_learner.ExpressionLearner)
                learner.chat_id = "fixture"
                learner.chat_name = "fixture"
                learner.express_learn_model = SimpleNamespace(
                    generate_response_async=AsyncMock(return_value=("[]", None))
                )
                learner._check_cached_jargons_in_messages = Mock(return_value=[])
                learner._process_jargon_entries = AsyncMock()
                learner._filter_expressions = Mock(return_value=[("situation", "style")])
                learner._upsert_expression_record = AsyncMock()
                with (
                    patch.object(
                        expression_learner,
                        "global_config",
                        SimpleNamespace(
                            bot=SimpleNamespace(nickname="fixture"),
                            expression=SimpleNamespace(
                                get_expression_config_for_chat=lambda _, expression_enabled=expression_enabled, jargon_enabled=jargon_enabled: (
                                    True,
                                    expression_enabled,
                                    jargon_enabled,
                                )
                            ),
                        ),
                    ),
                    patch.object(expression_learner, "build_anonymous_messages", AsyncMock(return_value="fixture")),
                    patch.object(expression_learner.prompt_manager, "format_prompt", return_value="fixture"),
                    patch.object(
                        expression_learner,
                        "parse_expression_response",
                        return_value=([("s", "style", "1")], [("jargon", "1")]),
                    ),
                ):
                    await learner.learn_and_store([SimpleNamespace()])
                self.assertEqual(learner._process_jargon_entries.await_count, int(jargon_enabled))
                self.assertEqual(learner._upsert_expression_record.await_count, int(expression_enabled))
                self.assertEqual(
                    learner.express_learn_model.generate_response_async.await_count,
                    int(expression_enabled or jargon_enabled),
                )

    async def test_recorder_dispatches_jargon_only_learning(self):
        recorder = object.__new__(message_recorder.MessageRecorder)
        recorder.chat_id = recorder.chat_name = "fixture"
        recorder.chat_stream = object()
        recorder.last_extraction_time = 0
        recorder._extraction_lock = asyncio.Lock()
        recorder.should_trigger_extraction = lambda: True
        recorder.enable_expression_learning = False
        recorder.enable_jargon_learning = True
        recorder.enable_behavior_learning = False
        recorder._trigger_expression_learning = AsyncMock()
        pending = []
        with (
            patch.object(
                message_recorder, "get_raw_msg_by_timestamp_with_chat_inclusive", return_value=[SimpleNamespace(time=1)]
            ),
            patch.object(
                message_recorder, "spawn_background_task", side_effect=lambda coro, **kwargs: pending.append(coro)
            ),
        ):
            await recorder.extract_and_distribute()
        await asyncio.gather(*pending)
        self.assertEqual(recorder._trigger_expression_learning.await_count, 1)
