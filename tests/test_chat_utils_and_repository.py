import asyncio
import unittest
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import patch

from peewee import SqliteDatabase

from src.chat.utils import timer_calculator
from src.chat.utils import typo_generator
from src.chat.utils import utils as chat_utils
from src.common import message_repository
from src.common.data_models.database_data_model import DatabaseMessages
from src.common.database.database_model import Messages
from src.services.adapter_identity import get_adapter_identity_registry


def make_message_record(message_id: str, time_value: float, **overrides):
    data = {
        "message_id": message_id,
        "time": time_value,
        "chat_id": "chat-1",
        "chat_info_stream_id": "chat-1",
        "chat_info_platform": "qq",
        "chat_info_user_platform": "qq",
        "chat_info_user_id": "bot",
        "chat_info_user_nickname": "Mai",
        "chat_info_create_time": 1.0,
        "chat_info_last_active_time": 2.0,
        "user_platform": "qq",
        "user_id": "user",
        "user_nickname": "Alice",
        "processed_plain_text": message_id,
        "is_command": False,
        "intercept_message_level": 0,
    }
    data.update(overrides)
    return Messages.create(**data)


class MessageRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = SqliteDatabase(":memory:")
        self.original_db = Messages._meta.database
        Messages._meta.set_database(self.db)
        self.db.connect()
        self.db.create_tables([Messages])

        make_message_record("m1", 1.0, user_id="bot")
        make_message_record("m2", 2.0, is_command=True)
        make_message_record("m3", 3.0, intercept_message_level=2)
        make_message_record("notice", 4.0)

    def tearDown(self) -> None:
        self.db.drop_tables([Messages])
        self.db.close()
        Messages._meta.set_database(self.original_db)

    def test_find_messages_applies_filters_sort_limit_and_visibility_flags(self) -> None:
        fake_config = SimpleNamespace(bot=SimpleNamespace(qq_account="bot"))
        with patch.object(message_repository, "global_config", fake_config):
            latest_two = message_repository.find_messages({"chat_id": "chat-1"}, limit=2, limit_mode="latest")
            earliest_one = message_repository.find_messages({"chat_id": "chat-1"}, limit=1, limit_mode="earliest")
            sorted_desc = message_repository.find_messages(
                {"time": {"$gte": 1.0}, "unknown": "ignored"},
                sort=[("time", -1), ("time", 0), ("missing", 1)],
                limit=0,
            )
            without_bot = message_repository.find_messages({"chat_id": "chat-1"}, filter_bot=True)
            without_commands = message_repository.find_messages({"chat_id": "chat-1"}, filter_command=True)
            low_intercept = message_repository.find_messages(
                {"chat_id": "chat-1"},
                filter_intercept_message_level=1,
            )

        self.assertEqual([msg.message_id for msg in latest_two], ["m2", "m3"])
        self.assertEqual([msg.message_id for msg in earliest_one], ["m1"])
        self.assertEqual([msg.message_id for msg in sorted_desc], ["m3", "m2", "m1"])
        self.assertEqual([msg.message_id for msg in without_bot], ["m2", "m3"])
        self.assertEqual([msg.message_id for msg in without_commands], ["m1", "m3"])
        self.assertEqual([msg.message_id for msg in low_intercept], ["m1", "m2"])
        self.assertTrue(all(isinstance(msg, DatabaseMessages) for msg in latest_two))

    def test_find_and_count_support_mongo_style_operators_and_error_fallbacks(self) -> None:
        range_result = message_repository.find_messages({"time": {"$gt": 1.0, "$lt": 3.0}})
        lte_result = message_repository.find_messages({"time": {"$lte": 2.0}})
        ne_result = message_repository.find_messages({"message_id": {"$ne": "m1"}})
        sorted_asc = message_repository.find_messages({"chat_id": "chat-1"}, sort=[("time", 1)])
        in_result = message_repository.find_messages({"message_id": {"$in": ["m1", "m3"]}})
        nin_result = message_repository.find_messages({"message_id": {"$nin": ["m1"]}})
        unknown_operator = message_repository.find_messages({"time": {"$unknown": 1.0}})
        count_range = message_repository.count_messages({"time": {"$gt": 1.0, "$lte": 3.0}})
        count_lt_gte = message_repository.count_messages({"time": {"$lt": 3.0, "$gte": 1.0}})
        count_in = message_repository.count_messages({"message_id": {"$in": ["m1", "m3"]}})
        count_nin = message_repository.count_messages({"message_id": {"$nin": ["m1"]}})
        count_ne = message_repository.count_messages({"message_id": {"$ne": "m1"}})
        count_equal = message_repository.count_messages({"message_id": "m1"})
        count_unknown_operator = message_repository.count_messages({"time": {"$unknown": 1.0}})
        count_unknown_field = message_repository.count_messages({"missing": "ignored"})

        self.assertEqual([msg.message_id for msg in range_result], ["m2"])
        self.assertEqual([msg.message_id for msg in lte_result], ["m1", "m2"])
        self.assertEqual([msg.message_id for msg in ne_result], ["m2", "m3"])
        self.assertEqual([msg.message_id for msg in sorted_asc], ["m1", "m2", "m3"])
        self.assertEqual([msg.message_id for msg in in_result], ["m1", "m3"])
        self.assertEqual([msg.message_id for msg in nin_result], ["m2", "m3"])
        self.assertEqual([msg.message_id for msg in unknown_operator], ["m1", "m2", "m3"])
        self.assertEqual(count_range, 2)
        self.assertEqual(count_lt_gte, 2)
        self.assertEqual(count_in, 2)
        self.assertEqual(count_nin, 2)
        self.assertEqual(count_ne, 2)
        self.assertEqual(count_equal, 1)
        self.assertEqual(count_unknown_operator, 3)
        self.assertEqual(count_unknown_field, 3)

        with patch.object(message_repository.Messages, "select", side_effect=RuntimeError("db down")):
            self.assertEqual(message_repository.find_messages({}), [])
            self.assertEqual(message_repository.count_messages({}), 0)


class ChatUtilsTest(unittest.TestCase):
    def setUp(self) -> None:
        get_adapter_identity_registry().clear()

    def tearDown(self) -> None:
        get_adapter_identity_registry().clear()

    def test_platform_account_parsing_and_bot_identity_support_multiple_platforms(self) -> None:
        accounts = chat_utils.parse_platform_accounts(["tg: 12345", "telegram: fallback", "wx: wxid", "bad"])

        self.assertEqual(accounts, {"tg": "12345", "telegram": "fallback", "wx": "wxid"})
        self.assertEqual(chat_utils.get_current_platform_account("qq", accounts, "10000"), "10000")
        self.assertEqual(chat_utils.get_current_platform_account("telegram", accounts, "10000"), "12345")
        self.assertEqual(chat_utils.get_current_platform_account("wx", accounts, "10000"), "wxid")

        fake_config = SimpleNamespace(
            bot=SimpleNamespace(qq_account="10000", platforms=["tg:12345", "wx:wxid"]),
        )
        with patch.object(chat_utils, "global_config", fake_config):
            self.assertTrue(chat_utils.is_bot_self("qq", "10000"))
            self.assertTrue(chat_utils.is_bot_self("webui", "10000"))
            self.assertTrue(chat_utils.is_bot_self("telegram", "12345"))
            self.assertTrue(chat_utils.is_bot_self("wx", "wxid"))
            self.assertFalse(chat_utils.is_bot_self("telegram", "other"))
            self.assertFalse(chat_utils.is_bot_self("", "10000"))

    def test_runtime_adapter_identity_takes_precedence_over_manual_account_config(self) -> None:
        get_adapter_identity_registry().register("onebot_default", "qq", "20000", "Connected Bot")
        fake_config = SimpleNamespace(
            bot=SimpleNamespace(qq_account="10000", platforms=[]),
        )

        with patch.object(chat_utils, "global_config", fake_config):
            self.assertTrue(chat_utils.is_bot_self("qq", "20000"))
            self.assertFalse(chat_utils.is_bot_self("qq", "10000"))

    def test_mention_detection_uses_explicit_flags_segments_account_patterns_and_aliases(self) -> None:
        fake_config = SimpleNamespace(
            bot=SimpleNamespace(
                qq_account="10000",
                platforms=["tg:botname"],
                nickname="Mai",
                alias_names=["麦麦"],
            ),
            chat=SimpleNamespace(at_bot_inevitable_reply=1, mentioned_bot_reply=1),
        )

        def make_message(text: str, *, platform: str = "qq", additional_config=None, segment=None):
            return SimpleNamespace(
                processed_plain_text=text,
                message_info=SimpleNamespace(platform=platform, additional_config=additional_config or {}),
                message_segment=segment,
                is_mentioned=False,
            )

        mention_segment = SimpleNamespace(type="seglist", data=[SimpleNamespace(type="mention_bot", data=None)])
        with patch.object(chat_utils, "global_config", fake_config):
            self.assertEqual(
                chat_utils.is_mentioned_bot_in_message(make_message("hello", segment=mention_segment)),
                (True, True, 1.0),
            )
            self.assertEqual(
                chat_utils.is_mentioned_bot_in_message(make_message("@<Mai:10000> hello")), (True, True, 1.0)
            )
            self.assertEqual(
                chat_utils.is_mentioned_bot_in_message(make_message("@botname hi", platform="telegram")),
                (True, True, 1.0),
            )
            self.assertEqual(chat_utils.is_mentioned_bot_in_message(make_message("麦麦在吗")), (True, False, 1.0))
            self.assertEqual(
                chat_utils.is_mentioned_bot_in_message(
                    make_message("hello", additional_config={"is_mentioned": "0.4"})
                ),
                (True, False, 1.0),
            )
            self.assertEqual(chat_utils.is_mentioned_bot_in_message(make_message("hello")), (False, False, 0.0))

    def test_text_splitting_punctuation_kaomoji_ratio_time_and_keyword_helpers(self) -> None:
        with patch.object(chat_utils.random, "random", return_value=1.0):
            self.assertEqual(
                chat_utils.split_into_sentences_w_remove_punctuation("你好，世界。hello world\n下一行"),
                ["你好", "世界", "hello world", "下一行"],
            )
        with patch.object(chat_utils.random, "random", return_value=0.0):
            self.assertEqual(chat_utils.split_into_sentences_w_remove_punctuation("Hi"), ["H", "i"])
        with patch.object(chat_utils.random, "random", side_effect=[0.1, 0.2]):
            self.assertEqual(chat_utils.random_remove_punctuation("你，好。"), "你 好")

        protected, mapping = chat_utils.protect_kaomoji("你好 (╯°□°）╯︵ ┻━┻")
        self.assertIn("__KAOMOJI_0__", protected)
        self.assertEqual(chat_utils.recover_kaomoji([protected], mapping), ["你好 (╯°□°）╯︵ ┻━┻"])
        self.assertEqual(chat_utils.get_western_ratio("abc123中文"), 0.375)
        self.assertEqual(chat_utils.truncate_message("abcdefgh", max_length=4), "abcd...")

        with patch.object(chat_utils.time, "time", return_value=1_000.0):
            self.assertEqual(chat_utils.translate_timestamp_to_human_readable(990.0, mode="relative"), "刚刚")
            self.assertEqual(chat_utils.translate_timestamp_to_human_readable(940.0, mode="relative"), "1分钟前")
            self.assertEqual(chat_utils.calculate_typing_time("中a", 995.0, chinese_time=0.3, english_time=0.1), 0.4)
            self.assertEqual(chat_utils.calculate_typing_time("emoji", 995.0, is_emoji=True), 1)
            self.assertEqual(chat_utils.calculate_typing_time("很久", 900.0), 1)

        self.assertEqual(chat_utils.parse_keywords_string('{"keywords": [" A ", "B"]}'), ["A", "B"])
        self.assertEqual(chat_utils.parse_keywords_string("A/B/C"), ["A", "B", "C"])
        self.assertEqual(chat_utils.parse_keywords_string([" A ", "", "B"]), ["A", "B"])

        messages = [DatabaseMessages(message_id="a"), DatabaseMessages(message_id="b")]
        with patch.object(chat_utils.random, "randint", return_value=5):
            self.assertEqual([item[0] for item in chat_utils.assign_message_ids(messages)], ["m15", "m25"])

        with patch.object(chat_utils.jieba, "cut", return_value=["苹果", "和", "香蕉", "的", "故事", "，"]):
            self.assertEqual(chat_utils.cut_key_words("苹果和香蕉的故事"), ["苹果和香蕉", "故事"])


class TypoGeneratorTest(unittest.TestCase):
    def make_generator(self) -> typo_generator.ChineseTypoGenerator:
        generator = typo_generator.ChineseTypoGenerator.__new__(typo_generator.ChineseTypoGenerator)
        generator.error_rate = 1.0
        generator.min_freq = 5
        generator.tone_error_rate = 0.0
        generator.word_replace_rate = 0.0
        generator.max_freq_diff = 200
        generator.pinyin_dict = defaultdict(list, {"ma1": ["妈", "麻", "马"]})
        generator.char_frequency = {"妈": 100.0, "麻": 90.0, "马": 1.0}
        return generator

    def test_pinyin_tone_frequency_and_character_helpers_cover_boundaries(self) -> None:
        generator = self.make_generator()

        self.assertTrue(generator._is_chinese_char("你"))
        self.assertFalse(generator._is_chinese_char("a"))
        self.assertEqual(generator._get_pinyin("妈 a"), [("妈", "ma1")])
        self.assertEqual(generator._get_similar_tone_pinyin("ma"), "ma1")
        with patch.object(typo_generator.random, "choice", return_value=2):
            self.assertEqual(generator._get_similar_tone_pinyin("ma1"), "ma2")
            self.assertEqual(generator._get_similar_tone_pinyin("ma5"), "ma2")

        self.assertEqual(generator._calculate_replacement_probability(100, 120), 1.0)
        self.assertEqual(generator._calculate_replacement_probability(300, 1), 0.0)
        self.assertGreater(generator._calculate_replacement_probability(100, 90), 0.0)

        with patch.object(typo_generator.random, "random", return_value=1.0):
            self.assertEqual(generator._get_similar_frequency_chars("妈", "ma1"), ["麻"])
        self.assertIsNone(generator._get_similar_frequency_chars("无", "missing"))

    def test_create_typo_sentence_replaces_single_character_and_returns_optional_correction(self) -> None:
        generator = self.make_generator()

        with (
            patch.object(generator, "_segment_sentence", return_value=["妈", "!"]),
            patch.object(generator, "_get_word_pinyin", return_value=["ma1"]),
            patch.object(typo_generator.random, "random", side_effect=[0.0, 0.0, 0.0, 0.0]),
            patch.object(typo_generator.random, "choice", side_effect=lambda seq: seq[0]),
        ):
            typo_sentence, correction = generator.create_typo_sentence("妈!")

        self.assertEqual(typo_sentence, "麻!")
        self.assertEqual(correction, "妈")

    def test_format_typo_info_and_set_params_report_known_and_unknown_keys(self) -> None:
        generator = self.make_generator()

        self.assertEqual(generator.format_typo_info([]), "未生成错别字")
        formatted = generator.format_typo_info(
            [
                ("妈", "麻", "ma1", "ma2", 100.0, 90.0),
                ("妈妈", "麻麻", "ma1 ma1", "ma2 ma2", 100.0, 90.0),
            ]
        )

        self.assertIn("声调错误", formatted)
        self.assertIn("整词替换", formatted)

        with patch("builtins.print") as print_mock:
            generator.set_params(error_rate=0.2, missing=1)

        self.assertEqual(generator.error_rate, 0.2)
        self.assertEqual(print_mock.call_count, 2)


class TimerCalculatorTest(unittest.IsolatedAsyncioTestCase):
    def test_timer_context_type_validation_string_and_sync_decorator(self) -> None:
        with self.assertRaisesRegex(timer_calculator.TimerTypeError, "name"):
            timer_calculator.Timer(name=123, do_type_check=True)  # type: ignore[arg-type]
        with self.assertRaisesRegex(timer_calculator.TimerTypeError, "storage"):
            timer_calculator.Timer(storage=[], do_type_check=True)  # type: ignore[arg-type]

        storage = {}
        with patch.object(timer_calculator, "perf_counter", side_effect=[10.0, 10.1, 10.25]):
            with timer_calculator.Timer("block", storage) as timer:
                self.assertIn("计时中", str(timer))

        self.assertEqual(storage["block"], 0.25)
        self.assertEqual(timer.human_readable, "250.00毫秒")
        self.assertIn("250.00毫秒", str(timer))

        with patch.object(timer_calculator, "perf_counter", side_effect=[1.0, 2.5]):
            timed = timer_calculator.Timer("sync", storage)(lambda value: value + 1)
            self.assertEqual(timed(2), 3)
        self.assertEqual(storage["sync"], 1.5)

    async def test_timer_async_decorator_records_elapsed_time(self) -> None:
        storage = {}

        async def sample(value):
            await asyncio.sleep(0)
            return value * 2

        with patch.object(timer_calculator, "perf_counter", side_effect=[5.0, 5.5]):
            timed = timer_calculator.Timer("async", storage)(sample)
            self.assertEqual(await timed(4), 8)

        self.assertEqual(storage["async"], 0.5)
        self.assertIs(timed.__timer__.storage, storage)


if __name__ == "__main__":
    unittest.main()
