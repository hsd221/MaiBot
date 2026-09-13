import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.plugin_system.apis import message_api


class MessageQueryWrapperTest(unittest.TestCase):
    def test_time_range_query_filters_mai_messages_after_loading(self) -> None:
        raw_messages = [object()]
        filtered_messages = [object()]

        with (
            patch.object(message_api, "get_raw_msg_by_timestamp", return_value=raw_messages) as get_raw,
            patch.object(message_api, "filter_mai_messages", return_value=filtered_messages) as filter_mai,
        ):
            result = message_api.get_messages_by_time(1.0, 2.0, limit=3, limit_mode="earliest", filter_mai=True)

        self.assertIs(result, filtered_messages)
        get_raw.assert_called_once_with(1.0, 2.0, 3, "earliest")
        filter_mai.assert_called_once_with(raw_messages)

    def test_chat_query_rejects_invalid_time_range_and_limit(self) -> None:
        invalid_calls = [
            lambda: message_api.get_messages_by_time_in_chat("chat-1", "bad", 2.0),
            lambda: message_api.get_messages_by_time_in_chat("chat-1", 1.0, 2.0, limit=-1),
        ]

        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    call()

    def test_inclusive_chat_query_delegates_options_and_filters_when_requested(self) -> None:
        raw_messages = [object()]
        filtered_messages = [object()]

        with (
            patch.object(
                message_api, "get_raw_msg_by_timestamp_with_chat_inclusive", return_value=raw_messages
            ) as get_chat,
            patch.object(message_api, "filter_mai_messages", return_value=filtered_messages) as filter_mai,
        ):
            result = message_api.get_messages_by_time_in_chat_inclusive(
                "chat-1",
                10.0,
                20.0,
                limit=4,
                limit_mode="earliest",
                filter_mai=True,
                filter_command=True,
                filter_intercept_message_level=2,
            )

        self.assertIs(result, filtered_messages)
        get_chat.assert_called_once_with(
            chat_id="chat-1",
            timestamp_start=10.0,
            timestamp_end=20.0,
            limit=4,
            limit_mode="earliest",
            filter_bot=True,
            filter_command=True,
            filter_intercept_message_level=2,
        )
        filter_mai.assert_called_once_with(raw_messages)

    def test_inclusive_chat_query_returns_raw_messages_without_filtering(self) -> None:
        raw_messages = [object()]

        with patch.object(
            message_api, "get_raw_msg_by_timestamp_with_chat_inclusive", return_value=raw_messages
        ) as get_chat:
            result = message_api.get_messages_by_time_in_chat_inclusive("chat-1", 10.0, 20.0)

        self.assertIs(result, raw_messages)
        get_chat.assert_called_once_with(
            chat_id="chat-1",
            timestamp_start=10.0,
            timestamp_end=20.0,
            limit=message_api.DEFAULT_MESSAGE_LIMIT,
            limit_mode="latest",
            filter_bot=False,
            filter_command=False,
            filter_intercept_message_level=None,
        )

    def test_inclusive_chat_query_rejects_invalid_inputs(self) -> None:
        invalid_calls = [
            lambda: message_api.get_messages_by_time_in_chat_inclusive("", 1.0, 2.0),
            lambda: message_api.get_messages_by_time_in_chat_inclusive(123, 1.0, 2.0),
            lambda: message_api.get_messages_by_time_in_chat_inclusive("chat-1", "bad", 2.0),
            lambda: message_api.get_messages_by_time_in_chat_inclusive("chat-1", 1.0, 2.0, limit=-1),
        ]

        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    call()

    def test_chat_query_for_users_validates_and_delegates(self) -> None:
        raw_messages = [object()]
        person_ids = ["person-1", "person-2"]

        with patch.object(
            message_api, "get_raw_msg_by_timestamp_with_chat_users", return_value=raw_messages
        ) as get_chat:
            result = message_api.get_messages_by_time_in_chat_for_users(
                "chat-1", 1.0, 2.0, person_ids, limit=5, limit_mode="earliest"
            )

        self.assertIs(result, raw_messages)
        get_chat.assert_called_once_with("chat-1", 1.0, 2.0, person_ids, 5, "earliest")

        invalid_calls = [
            lambda: message_api.get_messages_by_time_in_chat_for_users("", 1.0, 2.0, person_ids),
            lambda: message_api.get_messages_by_time_in_chat_for_users(123, 1.0, 2.0, person_ids),
            lambda: message_api.get_messages_by_time_in_chat_for_users("chat-1", None, 2.0, person_ids),
            lambda: message_api.get_messages_by_time_in_chat_for_users("chat-1", 1.0, 2.0, person_ids, limit=-1),
        ]

        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    call()

    def test_global_query_for_users_validates_and_delegates(self) -> None:
        raw_messages = [object()]
        person_ids = ["person-1"]

        with patch.object(message_api, "get_raw_msg_by_timestamp_with_users", return_value=raw_messages) as get_users:
            result = message_api.get_messages_by_time_for_users(1.0, 2.0, person_ids, limit=6, limit_mode="earliest")

        self.assertIs(result, raw_messages)
        get_users.assert_called_once_with(1.0, 2.0, person_ids, 6, "earliest")

        invalid_calls = [
            lambda: message_api.get_messages_by_time_for_users("bad", 2.0, person_ids),
            lambda: message_api.get_messages_by_time_for_users(1.0, 2.0, person_ids, limit=-1),
        ]

        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    call()

    def test_random_chat_query_delegates_and_filters_when_requested(self) -> None:
        raw_messages = [object()]
        filtered_messages = [object()]

        with patch.object(message_api, "get_raw_msg_by_timestamp_random", return_value=raw_messages) as get_random:
            self.assertIs(
                message_api.get_random_chat_messages(1.0, 2.0, limit=3, limit_mode="earliest"),
                raw_messages,
            )
        get_random.assert_called_once_with(1.0, 2.0, 3, "earliest")

        with (
            patch.object(message_api, "get_raw_msg_by_timestamp_random", return_value=raw_messages) as get_random,
            patch.object(message_api, "filter_mai_messages", return_value=filtered_messages) as filter_mai,
        ):
            result = message_api.get_random_chat_messages(1.0, 2.0, limit=3, filter_mai=True)

        self.assertIs(result, filtered_messages)
        get_random.assert_called_once_with(1.0, 2.0, 3, "latest")
        filter_mai.assert_called_once_with(raw_messages)

        invalid_calls = [
            lambda: message_api.get_random_chat_messages("bad", 2.0),
            lambda: message_api.get_random_chat_messages(1.0, 2.0, limit=-1),
        ]

        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    call()

    def test_before_time_queries_delegate_filter_and_validate(self) -> None:
        raw_messages = [object()]
        filtered_messages = [object()]

        with patch.object(message_api, "get_raw_msg_before_timestamp", return_value=raw_messages) as get_before:
            self.assertIs(message_api.get_messages_before_time(10.0, limit=2), raw_messages)
        get_before.assert_called_once_with(10.0, 2)

        with (
            patch.object(message_api, "get_raw_msg_before_timestamp", return_value=raw_messages) as get_before,
            patch.object(message_api, "filter_mai_messages", return_value=filtered_messages) as filter_mai,
        ):
            result = message_api.get_messages_before_time(10.0, limit=2, filter_mai=True)

        self.assertIs(result, filtered_messages)
        get_before.assert_called_once_with(10.0, 2)
        filter_mai.assert_called_once_with(raw_messages)

        invalid_calls = [
            lambda: message_api.get_messages_before_time("bad"),
            lambda: message_api.get_messages_before_time(10.0, limit=-1),
        ]

        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    call()

    def test_before_time_chat_query_delegates_options_filters_and_validates(self) -> None:
        raw_messages = [object()]
        filtered_messages = [object()]

        with patch.object(message_api, "get_raw_msg_before_timestamp_with_chat", return_value=raw_messages) as get_chat:
            self.assertIs(
                message_api.get_messages_before_time_in_chat("chat-1", 10.0, limit=3, filter_intercept_message_level=4),
                raw_messages,
            )
        get_chat.assert_called_once_with(chat_id="chat-1", timestamp=10.0, limit=3, filter_intercept_message_level=4)

        with (
            patch.object(message_api, "get_raw_msg_before_timestamp_with_chat", return_value=raw_messages) as get_chat,
            patch.object(message_api, "filter_mai_messages", return_value=filtered_messages) as filter_mai,
        ):
            result = message_api.get_messages_before_time_in_chat("chat-1", 10.0, limit=3, filter_mai=True)

        self.assertIs(result, filtered_messages)
        get_chat.assert_called_once_with(chat_id="chat-1", timestamp=10.0, limit=3, filter_intercept_message_level=None)
        filter_mai.assert_called_once_with(raw_messages)

        invalid_calls = [
            lambda: message_api.get_messages_before_time_in_chat("", 10.0),
            lambda: message_api.get_messages_before_time_in_chat(123, 10.0),
            lambda: message_api.get_messages_before_time_in_chat("chat-1", "bad"),
            lambda: message_api.get_messages_before_time_in_chat("chat-1", 10.0, limit=-1),
        ]

        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    call()

    def test_before_time_query_for_users_validates_and_delegates(self) -> None:
        raw_messages = [object()]
        person_ids = ["person-1"]

        with patch.object(
            message_api, "get_raw_msg_before_timestamp_with_users", return_value=raw_messages
        ) as get_users:
            result = message_api.get_messages_before_time_for_users(10.0, person_ids, limit=4)

        self.assertIs(result, raw_messages)
        get_users.assert_called_once_with(10.0, person_ids, 4)

        invalid_calls = [
            lambda: message_api.get_messages_before_time_for_users("bad", person_ids),
            lambda: message_api.get_messages_before_time_for_users(10.0, person_ids, limit=-1),
        ]

        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    call()


class MessageRecentAndCountTest(unittest.TestCase):
    def test_recent_messages_filters_after_loading_and_uses_hours_window(self) -> None:
        raw_messages = [object()]
        filtered_messages = [object()]

        with (
            patch.object(message_api.time, "time", return_value=1000.0),
            patch.object(message_api, "get_raw_msg_by_timestamp_with_chat", return_value=raw_messages) as get_chat,
            patch.object(message_api, "filter_mai_messages", return_value=filtered_messages) as filter_mai,
        ):
            result = message_api.get_recent_messages(
                "chat-1", hours=2.5, limit=7, limit_mode="earliest", filter_mai=True
            )

        self.assertIs(result, filtered_messages)
        get_chat.assert_called_once_with("chat-1", 1000.0 - 2.5 * 3600, 1000.0, 7, "earliest")
        filter_mai.assert_called_once_with(raw_messages)

    def test_recent_messages_rejects_invalid_inputs(self) -> None:
        invalid_calls = [
            lambda: message_api.get_recent_messages("", hours=1.0),
            lambda: message_api.get_recent_messages(123, hours=1.0),
            lambda: message_api.get_recent_messages("chat-1", hours="bad"),
            lambda: message_api.get_recent_messages("chat-1", hours=-1.0),
            lambda: message_api.get_recent_messages("chat-1", limit=1.5),
            lambda: message_api.get_recent_messages("chat-1", limit=-1),
        ]

        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    call()

    def test_count_new_messages_for_users_validates_and_delegates(self) -> None:
        person_ids = ["person-1"]

        with patch.object(message_api, "num_new_messages_since_with_users", return_value=8) as count:
            self.assertEqual(message_api.count_new_messages_for_users("chat-1", 1.0, 2.0, person_ids), 8)

        count.assert_called_once_with("chat-1", 1.0, 2.0, person_ids)

        invalid_calls = [
            lambda: message_api.count_new_messages_for_users("", 1.0, 2.0, person_ids),
            lambda: message_api.count_new_messages_for_users(123, 1.0, 2.0, person_ids),
            lambda: message_api.count_new_messages_for_users("chat-1", "bad", 2.0, person_ids),
            lambda: message_api.count_new_messages_for_users("chat-1", 1.0, object(), person_ids),
        ]

        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    call()

    def test_count_new_messages_rejects_invalid_inputs(self) -> None:
        invalid_calls = [
            lambda: message_api.count_new_messages("", 1.0),
            lambda: message_api.count_new_messages(123, 1.0),
            lambda: message_api.count_new_messages("chat-1", "bad"),
        ]

        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    call()


class MessageFormatAndFilterTest(unittest.IsolatedAsyncioTestCase):
    def test_readable_messages_to_str_forwards_all_format_options(self) -> None:
        messages = [SimpleNamespace()]

        with patch.object(message_api, "build_readable_messages", return_value="readable") as build:
            result = message_api.build_readable_messages_to_str(
                messages,
                replace_bot_name=False,
                timestamp_mode="absolute",
                read_mark=3.5,
                truncate=True,
                show_actions=True,
            )

        self.assertEqual(result, "readable")
        build.assert_called_once_with(messages, False, "absolute", 3.5, True, True)

    async def test_detail_formatter_and_person_id_extractor_forward_options(self) -> None:
        messages = [SimpleNamespace()]
        details = ("readable", [(1.0, "Alice", "hello")])

        with patch.object(
            message_api, "build_readable_messages_with_list", new=AsyncMock(return_value=details)
        ) as build:
            result = await message_api.build_readable_messages_with_details(
                messages, replace_bot_name=False, timestamp_mode="absolute", truncate=True
            )

        self.assertIs(result, details)
        build.assert_awaited_once_with(messages, False, "absolute", True)

        with patch.object(message_api, "get_person_id_list", new=AsyncMock(return_value=["person-1"])) as get_ids:
            self.assertEqual(await message_api.get_person_ids_from_messages(messages), ["person-1"])
        get_ids.assert_awaited_once_with(messages)

    def test_filter_mai_messages_keeps_non_bot_messages(self) -> None:
        bot_message = SimpleNamespace(user_info=SimpleNamespace(platform="qq", user_id="bot"))
        user_message = SimpleNamespace(user_info=SimpleNamespace(platform="qq", user_id="user"))

        with patch.object(message_api, "is_bot_self", side_effect=[True, False]) as is_bot:
            result = message_api.filter_mai_messages([bot_message, user_message])

        self.assertEqual(result, [user_message])
        self.assertEqual(
            [call.args for call in is_bot.call_args_list],
            [("qq", "bot"), ("qq", "user")],
        )

    def test_translate_pid_to_description_strips_description_and_falls_back_for_blank_values(self) -> None:
        with patch.object(message_api.Images, "get_or_none", return_value=SimpleNamespace(description=" cat ")):
            self.assertEqual(message_api.translate_pid_to_description("img-1"), "cat")

        for image in [SimpleNamespace(description="   "), SimpleNamespace(description=None), None]:
            with self.subTest(image=image):
                with patch.object(message_api.Images, "get_or_none", return_value=image):
                    self.assertEqual(message_api.translate_pid_to_description("img-blank"), "[图片]")


if __name__ == "__main__":
    unittest.main()
