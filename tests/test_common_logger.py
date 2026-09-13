import asyncio
import hashlib
import logging
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.common import logger as app_logger


class LoggerRedactionTest(unittest.TestCase):
    def test_hash_id_and_redact_secret_are_stable_and_handle_empty_values(self) -> None:
        self.assertEqual(app_logger.hash_id("secret", length=8), hashlib.sha256(b"secret").hexdigest()[:8])
        self.assertEqual(app_logger.hash_id(None), "")

        self.assertEqual(app_logger.redact_secret("short"), "***")
        self.assertEqual(app_logger.redact_secret("abcdefghijklmnopqrstuvwxyz"), "abcd***wxyz")
        self.assertEqual(app_logger.redact_secret(None), "")

    def test_summarize_and_redact_text_escape_truncate_and_mask_secret_shapes(self) -> None:
        summarized = app_logger.summarize_text("hello\nworld", max_length=20)
        self.assertEqual(summarized, r"hello\nworld")

        truncated = app_logger.summarize_text("abcdefgh", max_length=5)
        self.assertRegex(truncated, r"abcde\.\.\.<truncated chars=8 sha256=[0-9a-f]{16}>")

        text = "api_key=abcdefghijklmnopqrstuvwxyz jwt aaa.bbbbbbbbbbbb.cccccccccccc long " + "Z" * 84
        redacted = app_logger.redact_text(text, allow_plaintext=True, max_length=400)

        self.assertIn("api_key=abcd***wxyz", redacted)
        self.assertIn("aaa.bbbbbbbbbbbb.cccccccccccc", redacted)
        self.assertRegex(redacted, r"<base64 chars=84 sha256=[0-9a-f]{16}>")

        self.assertEqual(app_logger.summarize_text(None), "")
        self.assertEqual(app_logger.redact_text(None), "")

    def test_sanitize_log_event_masks_sensitive_fields_and_text_payloads_recursively(self) -> None:
        event = {
            "api_key": "abcdefghijklmnopqrstuvwxyz",
            "content": "private message",
            "nested": {
                "password": "super-secret-value",
                "message": "inner text",
            },
            "items": list(range(25)),
            "exc_info": True,
        }

        sanitized = app_logger.sanitize_log_event(None, "info", event)

        self.assertEqual(sanitized["api_key"], "abcd***wxyz")
        self.assertRegex(sanitized["content"], r"<text chars=15 sha256=[0-9a-f]{16}>")
        self.assertEqual(sanitized["nested"]["password"], "supe***alue")
        self.assertRegex(sanitized["nested"]["message"], r"<text chars=10 sha256=[0-9a-f]{16}>")
        self.assertEqual(sanitized["items"][-1], "<truncated items=25>")
        self.assertTrue(sanitized["exc_info"])

    def test_debug_plaintext_logging_allows_content_summary_only_when_enabled(self) -> None:
        with patch.object(app_logger, "LOG_CONFIG", {"debug_plaintext_logging": True, "max_debug_text_length": 5}):
            debug_event = app_logger.sanitize_log_event(None, "debug", {"content": "abcdefg"})

        with patch.object(app_logger, "LOG_CONFIG", {"debug_plaintext_logging": False}):
            info_event = app_logger.sanitize_log_event(None, "debug", {"content": "abcdefg"})

        self.assertRegex(debug_event["content"], r"abcde\.\.\.<truncated chars=7 sha256=[0-9a-f]{16}>")
        self.assertRegex(info_event["content"], r"<text chars=7 sha256=[0-9a-f]{16}>")

    def test_sanitize_log_event_redacts_sensitive_event_strings(self) -> None:
        event = app_logger.sanitize_log_event(None, "info", {"event": "完整提示词: " + "x" * 20})

        self.assertRegex(event["event"], r"<event chars=27 sha256=[0-9a-f]{16}>")

    def test_sanitize_value_handles_paths_long_payloads_unknown_objects_and_truncated_dicts(self) -> None:
        class CustomValue:
            def __str__(self) -> str:
                return "custom token=abcdefghijklmnopqrstuvwxyz"

        large_event = "payload " + "x" * 510 + "\n{}"
        event = {
            "event": large_event,
            "path": Path("logs/app.log"),
            "many_items": {f"k{i}": i for i in range(55)},
            "custom": CustomValue(),
            "none_value": None,
            "bool_value": True,
            "number_value": 3,
        }

        sanitized = app_logger.sanitize_log_event(None, "info", event)

        self.assertRegex(sanitized["event"], r"<event chars=\d+ sha256=[0-9a-f]{16}>")
        self.assertEqual(sanitized["path"], "logs/app.log")
        self.assertEqual(sanitized["many_items"]["<truncated>"], "items=55")
        self.assertIn("token=abcd***wxyz", sanitized["custom"])
        self.assertIsNone(sanitized["none_value"])
        self.assertTrue(sanitized["bool_value"])
        self.assertEqual(sanitized["number_value"], 3)

    def test_redact_text_masks_long_hex_and_uses_configured_non_plaintext_length(self) -> None:
        long_hex = "a" * 48
        with patch.object(app_logger, "LOG_CONFIG", {"max_log_field_length": 60}):
            redacted = app_logger.redact_text(f"hex {long_hex} tail " + "x" * 40)

        self.assertRegex(redacted, r"hex <hex sha256=[0-9a-f]{16}>")
        self.assertRegex(redacted, r"\.\.\.<truncated chars=\d+ sha256=[0-9a-f]{16}>")


class LoggerPathAndRenderingTest(unittest.TestCase):
    def test_convert_pathname_to_module_handles_project_external_and_message_logger_paths(self) -> None:
        project_file = app_logger.PROJECT_ROOT / "src" / "common" / "logger.py"
        converted = app_logger.convert_pathname_to_module(None, "info", {"pathname": str(project_file)})
        self.assertEqual(converted["module"], "src.common.logger")
        self.assertNotIn("pathname", converted)

        fallback = app_logger.convert_pathname_to_module(None, "info", {"pathname": "/outside/example.py"})
        self.assertEqual(fallback["module"], "example")
        self.assertNotIn("pathname", fallback)

        maim = app_logger.convert_pathname_to_module(
            None,
            "info",
            {"logger_name": "maim_message", "pathname": "/outside/broker.py"},
        )
        self.assertEqual(maim["module"], "maim_message")
        self.assertNotIn("pathname", maim)

    def test_console_renderer_uses_alias_level_abbreviation_json_and_extra_fields_without_color(self) -> None:
        renderer = app_logger.ModuleColoredConsoleRenderer(colors=False)

        rendered = renderer(
            None,
            "warning",
            {
                "timestamp": "07-08 12:00:00",
                "level": "warning",
                "logger_name": "planner",
                "event": {"action": "reply"},
                "meta": ["a", "b"],
            },
        )

        self.assertEqual(
            rendered,
            '07-08 12:00:00 | [W] | [规划器] | {"action": "reply"} | meta=["a", "b"]',
        )

    def test_console_renderer_honors_color_modes_unknown_levels_and_non_jsonable_values(self) -> None:
        class NonJsonable:
            pass

        with patch.object(app_logger, "LOG_CONFIG", {"color_text": "title"}):
            title_color = app_logger.ModuleColoredConsoleRenderer(colors=True)
        self.assertTrue(title_color._enable_module_colors)
        self.assertFalse(title_color._enable_level_colors)
        self.assertFalse(title_color._enable_full_content_colors)

        with patch.object(app_logger, "LOG_CONFIG", {"color_text": "none"}):
            no_color = app_logger.ModuleColoredConsoleRenderer(colors=True)
        self.assertFalse(no_color._colors)

        with patch.object(app_logger, "LOG_CONFIG", {"color_text": "full"}):
            full_color = app_logger.ModuleColoredConsoleRenderer(colors=True)
        self.assertTrue(full_color._enable_level_colors)
        self.assertTrue(full_color._enable_full_content_colors)

        with patch.object(app_logger, "LOG_CONFIG", {"color_text": "unexpected"}):
            fallback_color = app_logger.ModuleColoredConsoleRenderer(colors=True)
        self.assertTrue(fallback_color._enable_module_colors)
        self.assertFalse(fallback_color._enable_level_colors)

        rendered = no_color(
            None,
            "notice",
            {
                "level": "notice",
                "logger_name": "unknown_module",
                "event": {"bad": NonJsonable()},
                "extra": {"bad": NonJsonable()},
            },
        )

        self.assertIn("[N]", rendered)
        self.assertIn("[unknown_module]", rendered)
        self.assertIn("'bad':", rendered)
        self.assertIn("extra={'bad':", rendered)

        colored = title_color(
            None,
            "info",
            {
                "level": "info",
                "logger_name": "planner",
                "event": "planning started",
                "count": 2,
            },
        )
        self.assertIn(app_logger.LEVEL_COLORS["info"], colored)
        self.assertIn(app_logger.MODULE_COLORS["planner"], colored)
        self.assertIn("planning started", colored)
        self.assertIn("count=2", colored)

        unknown_colored = title_color(
            None,
            "info",
            {"level": "info", "logger_name": "unknown_module", "event": 123},
        )
        self.assertIn("[unknown_module]", unknown_colored)
        self.assertIn("123", unknown_colored)


class LoggerHandlerTest(unittest.TestCase):
    def test_handler_singletons_reuse_existing_root_file_handler_and_initialize_websocket_once(self) -> None:
        root_logger = logging.getLogger()
        original_handlers = root_logger.handlers[:]
        original_file_handler = app_logger._file_handler
        original_console_handler = app_logger._console_handler
        original_ws_handler = app_logger._ws_handler
        try:
            app_logger._file_handler = None
            app_logger._console_handler = None
            app_logger._ws_handler = None
            root_logger.handlers[:] = []

            with tempfile.TemporaryDirectory() as tmpdir:
                existing_file_handler = app_logger.TimestampedFileHandler(Path(tmpdir), max_bytes=1024, backup_count=1)
                root_logger.addHandler(existing_file_handler)

                self.assertIs(app_logger.get_file_handler(), existing_file_handler)
                console_handler = app_logger.get_console_handler()
                self.assertIs(app_logger.get_console_handler(), console_handler)
                ws_handler = app_logger.get_ws_handler()
                self.assertIs(app_logger.get_ws_handler(), ws_handler)
                self.assertEqual(ws_handler.level, logging.DEBUG)

                fake_loop = object()
                scheduled = []

                def capture_scheduled(coro, loop):
                    scheduled.append(loop)
                    if hasattr(coro, "close"):
                        coro.close()
                    return object()

                with patch("asyncio.run_coroutine_threadsafe", side_effect=capture_scheduled):
                    app_logger.initialize_ws_handler(fake_loop)
                    app_logger.initialize_ws_handler(fake_loop)

                self.assertIs(ws_handler.loop, fake_loop)
                self.assertIs(ws_handler.formatter, app_logger.file_formatter)
                self.assertEqual(root_logger.handlers.count(ws_handler), 1)
                existing_file_handler.close()
        finally:
            root_logger.handlers[:] = original_handlers
            app_logger._file_handler = original_file_handler
            app_logger._console_handler = original_console_handler
            app_logger._ws_handler = original_ws_handler

    def test_timestamped_file_handler_writes_records_and_cleans_old_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir)
            handler = app_logger.TimestampedFileHandler(log_dir, max_bytes=1024, backup_count=3)
            handler.setFormatter(logging.Formatter("%(message)s"))

            try:
                record = logging.LogRecord("unit", logging.INFO, __file__, 1, "hello", None, None)
                handler.emit(record)
                self.assertIn("hello", handler.current_file.read_text(encoding="utf-8"))

                base_time = time.time() - 100
                old_files = []
                for index in range(4):
                    path = log_dir / f"app_20200101_00000{index}.log.jsonl"
                    path.write_text(str(index), encoding="utf-8")
                    os.utime(path, (base_time + index, base_time + index))
                    old_files.append(path)

                handler._cleanup_old_files()

                self.assertFalse(old_files[0].exists())
                self.assertFalse(old_files[1].exists())
                self.assertTrue(handler.current_file.exists())
                self.assertLessEqual(len(list(log_dir.glob("app_*.log.jsonl"))), 3)
            finally:
                handler.close()

    def test_timestamped_file_handler_rolls_over_handles_cleanup_and_emit_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir)
            handler = app_logger.TimestampedFileHandler(log_dir, max_bytes=1, backup_count=1)
            handler.setFormatter(logging.Formatter("%(message)s"))
            record = logging.LogRecord("unit", logging.INFO, __file__, 1, "hello", None, None)
            first_file = handler.current_file

            try:
                handler.current_file = log_dir / "missing.log.jsonl"
                self.assertFalse(handler._should_rollover())
                handler.current_file = first_file

                with patch.object(handler, "_do_rollover") as do_rollover:
                    with patch.object(handler, "_should_rollover", return_value=True):
                        handler.emit(record)
                do_rollover.assert_called_once_with()

                handler._do_rollover()
                self.assertIsNotNone(handler.current_stream)

                stale = log_dir / "app_20000101_000000.log.jsonl"
                stale.write_text("old", encoding="utf-8")
                with patch.object(Path, "unlink", side_effect=RuntimeError("unlink failed")):
                    handler._cleanup_old_files()

                handler.emit(record)
                self.assertTrue(first_file.exists())
                self.assertIsNotNone(handler.current_stream)

                with patch.object(Path, "glob", side_effect=RuntimeError("glob failed")):
                    handler._cleanup_old_files()

                with (
                    patch.object(handler, "_should_rollover", side_effect=RuntimeError("stat failed")),
                    patch.object(handler, "handleError") as handle_error,
                ):
                    handler.emit(record)

                handle_error.assert_called_once_with(record)
            finally:
                handler.close()

    def test_remove_duplicate_handlers_keeps_first_timestamped_file_handler(self) -> None:
        root_logger = logging.getLogger()
        original_handlers = root_logger.handlers[:]
        original_file_handler = app_logger._file_handler
        try:
            root_logger.handlers[:] = []
            app_logger._file_handler = None
            with tempfile.TemporaryDirectory() as tmpdir:
                first = app_logger.TimestampedFileHandler(Path(tmpdir) / "a", max_bytes=1024, backup_count=1)
                second = app_logger.TimestampedFileHandler(Path(tmpdir) / "b", max_bytes=1024, backup_count=1)
                root_logger.addHandler(first)
                root_logger.addHandler(second)

                app_logger.remove_duplicate_handlers()

                self.assertIn(first, root_logger.handlers)
                self.assertNotIn(second, root_logger.handlers)
                self.assertIs(app_logger._file_handler, first)
                self.assertIsNone(second.current_stream)
                first.close()
        finally:
            root_logger.handlers[:] = original_handlers
            app_logger._file_handler = original_file_handler

    def test_close_handlers_closes_and_clears_all_global_handlers(self) -> None:
        original_file_handler = app_logger._file_handler
        original_console_handler = app_logger._console_handler
        original_ws_handler = app_logger._ws_handler
        try:
            file_handler = Mock()
            console_handler = Mock()
            ws_handler = Mock()
            app_logger._file_handler = file_handler
            app_logger._console_handler = console_handler
            app_logger._ws_handler = ws_handler

            app_logger.close_handlers()

            file_handler.close.assert_called_once_with()
            console_handler.close.assert_called_once_with()
            ws_handler.close.assert_called_once_with()
            self.assertIsNone(app_logger._file_handler)
            self.assertIsNone(app_logger._console_handler)
            self.assertIsNone(app_logger._ws_handler)
        finally:
            app_logger._file_handler = original_file_handler
            app_logger._console_handler = original_console_handler
            app_logger._ws_handler = original_ws_handler

    def test_cleanup_old_logs_deletes_only_expired_log_files(self) -> None:
        class FakeLogger:
            def __init__(self) -> None:
                self.messages: list[str] = []

            def info(self, message: str) -> None:
                self.messages.append(message)

            def warning(self, message: str) -> None:
                self.messages.append(message)

            def error(self, message: str) -> None:
                self.messages.append(message)

        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir)
            old_log = log_dir / "old.log.jsonl"
            fresh_log = log_dir / "fresh.log.jsonl"
            old_log.write_text("old", encoding="utf-8")
            fresh_log.write_text("fresh", encoding="utf-8")
            old_time = time.time() - 31 * 24 * 60 * 60
            os.utime(old_log, (old_time, old_time))
            fake_logger = FakeLogger()

            with (
                patch.object(app_logger, "LOG_DIR", log_dir),
                patch.object(app_logger, "get_logger", return_value=fake_logger),
            ):
                app_logger.cleanup_old_logs()

            self.assertFalse(old_log.exists())
            self.assertTrue(fresh_log.exists())
            self.assertTrue(any("清理了 1 个过期日志文件" in message for message in fake_logger.messages))

    def test_cleanup_old_logs_logs_file_and_outer_errors(self) -> None:
        class FakeLogger:
            def __init__(self) -> None:
                self.warning_messages: list[str] = []
                self.error_messages: list[str] = []

            def warning(self, message: str) -> None:
                self.warning_messages.append(message)

            def error(self, message: str) -> None:
                self.error_messages.append(message)

        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir)
            old_log = log_dir / "old.log.jsonl"
            old_log.write_text("old", encoding="utf-8")
            old_time = time.time() - 31 * 24 * 60 * 60
            os.utime(old_log, (old_time, old_time))
            fake_logger = FakeLogger()

            with (
                patch.object(app_logger, "LOG_DIR", log_dir),
                patch.object(app_logger, "get_logger", return_value=fake_logger),
                patch.object(Path, "unlink", side_effect=RuntimeError("unlink failed")),
            ):
                app_logger.cleanup_old_logs()

            self.assertTrue(fake_logger.warning_messages)

        fake_logger = FakeLogger()
        with (
            patch.object(
                app_logger, "LOG_DIR", types.SimpleNamespace(glob=Mock(side_effect=RuntimeError("glob failed")))
            ),
            patch.object(app_logger, "get_logger", return_value=fake_logger),
        ):
            app_logger.cleanup_old_logs()

        self.assertTrue(fake_logger.error_messages)

    def test_websocket_log_handler_formats_record_and_schedules_broadcast_without_real_websocket(self) -> None:
        captured: list[dict] = []

        async def broadcast_log(log_data: dict) -> None:
            captured.append(log_data)

        def run_coroutine_threadsafe(coro, loop):
            self.assertIs(loop, fake_loop)
            asyncio.run(coro)
            return object()

        fake_loop = object()
        fake_logs_ws = types.ModuleType("src.webui.logs_ws")
        fake_logs_ws.broadcast_log = broadcast_log
        handler = app_logger.WebSocketLogHandler(loop=fake_loop)
        handler.set_loop(fake_loop)
        handler.setFormatter(logging.Formatter("%(message)s"))
        record = logging.LogRecord(
            "unit.module",
            logging.WARNING,
            __file__,
            1,
            '{"event": "hello from json"}',
            None,
            None,
        )

        with (
            patch.dict(sys.modules, {"src.webui.logs_ws": fake_logs_ws}),
            patch("asyncio.run_coroutine_threadsafe", side_effect=run_coroutine_threadsafe),
        ):
            handler.emit(record)

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["level"], "WARNING")
        self.assertEqual(captured[0]["module"], "unit.module")
        self.assertEqual(captured[0]["message"], "hello from json")
        self.assertRegex(captured[0]["id"], r"^\d+_")

    def test_websocket_log_handler_ignores_uninitialized_broadcast_and_handles_errors(self) -> None:
        record = logging.LogRecord("unit.module", logging.INFO, __file__, 1, "plain message", None, None)
        uninitialized = app_logger.WebSocketLogHandler()
        with patch("asyncio.run_coroutine_threadsafe") as run_coroutine_threadsafe:
            uninitialized.emit(record)
        run_coroutine_threadsafe.assert_not_called()

        handler = app_logger.WebSocketLogHandler(loop=object())
        handler.set_loop(handler.loop)
        fake_logs_ws = types.ModuleType("src.webui.logs_ws")
        fake_logs_ws.broadcast_log = Mock(return_value=object())

        def fail_scheduling(coro, loop):
            if hasattr(coro, "close"):
                coro.close()
            raise RuntimeError("schedule failed")

        with (
            patch.dict(sys.modules, {"src.webui.logs_ws": fake_logs_ws}),
            patch("asyncio.run_coroutine_threadsafe", side_effect=fail_scheduling),
        ):
            handler.emit(record)

        broken_handler = app_logger.WebSocketLogHandler(loop=object())
        broken_handler.set_loop(broken_handler.loop)
        broken_handler.setFormatter(logging.Formatter("%(message)s"))
        broken_handler.format = Mock(side_effect=RuntimeError("format failed"))
        with patch.object(broken_handler, "handleError") as handle_error:
            broken_handler.emit(record)
        handle_error.assert_called_once_with(record)


class LoggerConfigurationTest(unittest.TestCase):
    def test_get_logger_returns_cached_named_loggers_and_raw_logger_for_none(self) -> None:
        original_binds = dict(app_logger.binds)
        try:
            app_logger.binds.clear()
            self.assertIs(app_logger.get_logger(None), app_logger.raw_logger)

            first = app_logger.get_logger("unit_test_logger")
            second = app_logger.get_logger("unit_test_logger")

            self.assertIs(first, second)
            self.assertIs(app_logger.binds["unit_test_logger"], first)
        finally:
            app_logger.binds.clear()
            app_logger.binds.update(original_binds)

    def test_config_helpers_parse_bool_int_and_timestamp_styles(self) -> None:
        config = {
            "flag_true": "yes",
            "flag_false": "off",
            "flag_number": 1,
            "count": "12",
            "bad_count": "NaN",
            "date_style": "Y-m-d H:i:s",
        }
        with patch.object(app_logger, "LOG_CONFIG", config):
            self.assertTrue(app_logger._get_bool_config("flag_true"))
            self.assertFalse(app_logger._get_bool_config("flag_false", True))
            self.assertTrue(app_logger._get_bool_config("flag_number"))
            self.assertEqual(app_logger._get_int_config("count", 3), 12)
            self.assertEqual(app_logger._get_int_config("bad_count", 3), 3)
            self.assertEqual(app_logger.get_timestamp_format(), "%Y-%m-%d %H:%M:%S")

    def test_load_log_config_reads_existing_toml_or_falls_back_on_missing_and_parse_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(app_logger, "PROJECT_ROOT", Path(tmpdir)):
            root = Path(tmpdir)
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "bot_config.toml").write_text(
                '[log]\nconsole_log_level = "ERROR"\nfile_log_level = "WARNING"\n',
                encoding="utf-8",
            )

            old_cwd = os.getcwd()
            try:
                os.chdir(root)
                self.assertEqual(
                    app_logger.load_log_config(),
                    {"console_log_level": "ERROR", "file_log_level": "WARNING"},
                )

                (config_dir / "bot_config.toml").write_text("[log\n", encoding="utf-8")
                fallback = app_logger.load_log_config()
                self.assertEqual(fallback["log_level"], "INFO")
                self.assertIn("httpx", fallback["suppress_libraries"])

                (config_dir / "bot_config.toml").unlink()
                missing_fallback = app_logger.load_log_config()
                self.assertEqual(missing_fallback["file_log_level"], "DEBUG")
            finally:
                os.chdir(old_cwd)

    def test_configure_third_party_loggers_sets_root_suppressed_and_custom_levels(self) -> None:
        root_logger = logging.getLogger()
        original_root_level = root_logger.level
        suppressed = logging.getLogger("unit_suppressed_lib")
        custom = logging.getLogger("unit_custom_lib")
        try:
            with patch.object(
                app_logger,
                "LOG_CONFIG",
                {
                    "console_log_level": "ERROR",
                    "file_log_level": "DEBUG",
                    "suppress_libraries": ["unit_suppressed_lib"],
                    "library_log_levels": {"unit_custom_lib": "INFO", "unit_bad_level": "NOPE"},
                },
            ):
                app_logger.configure_third_party_loggers()

            self.assertEqual(root_logger.level, logging.DEBUG)
            self.assertEqual(suppressed.level, logging.CRITICAL + 1)
            self.assertFalse(suppressed.propagate)
            self.assertEqual(custom.level, logging.INFO)
            self.assertEqual(logging.getLogger("unit_bad_level").level, logging.WARNING)
        finally:
            root_logger.setLevel(original_root_level)
            suppressed.setLevel(logging.NOTSET)
            suppressed.propagate = True
            custom.setLevel(logging.NOTSET)
            logging.getLogger("unit_bad_level").setLevel(logging.NOTSET)

    def test_reconfigure_existing_loggers_reformats_handlers_and_applies_library_rules(self) -> None:
        root_logger = logging.getLogger()
        root_file = Mock(spec=app_logger.TimestampedFileHandler)
        root_stream = logging.StreamHandler()
        logger_obj = logging.getLogger("unit_existing_logger")
        suppressed_logger = logging.getLogger("unit_suppressed.child")
        custom_logger = logging.getLogger("unit_custom.child")
        old_stream = logging.StreamHandler()
        old_file = Mock(spec=app_logger.TimestampedFileHandler)
        original_handlers = logger_obj.handlers[:]
        try:
            root_logger.addHandler(root_file)
            root_logger.addHandler(root_stream)
            logger_obj.handlers[:] = [old_stream, old_file]
            with patch.object(
                app_logger,
                "LOG_CONFIG",
                {
                    "suppress_libraries": ["unit_suppressed"],
                    "library_log_levels": {"unit_custom": "ERROR"},
                },
            ):
                app_logger.reconfigure_existing_loggers()

            root_file.setFormatter.assert_called_with(app_logger.file_formatter)
            self.assertIs(root_stream.formatter, app_logger.console_formatter)
            self.assertEqual(suppressed_logger.level, logging.CRITICAL + 1)
            self.assertFalse(suppressed_logger.propagate)
            self.assertEqual(custom_logger.level, logging.ERROR)
            self.assertIn(old_stream, logger_obj.handlers)
            self.assertNotIn(old_file, logger_obj.handlers)
            old_file.close.assert_called_once_with()
            self.assertIs(old_stream.formatter, app_logger.console_formatter)
        finally:
            for handler in [root_file, root_stream]:
                if handler in root_logger.handlers:
                    root_logger.removeHandler(handler)
            logger_obj.handlers[:] = original_handlers
            suppressed_logger.setLevel(logging.NOTSET)
            suppressed_logger.propagate = True
            custom_logger.setLevel(logging.NOTSET)

    def test_initialize_logging_is_idempotent_and_verbose_logs_levels(self) -> None:
        original_initialized = app_logger._logging_initialized
        original_config = app_logger.LOG_CONFIG
        fake_logger = Mock()
        try:
            app_logger._logging_initialized = False
            with (
                patch.object(
                    app_logger, "load_log_config", return_value={"console_log_level": "ERROR", "file_log_level": "INFO"}
                ),
                patch.object(app_logger, "configure_third_party_loggers") as configure_third_party,
                patch.object(app_logger, "reconfigure_existing_loggers") as reconfigure_existing,
                patch.object(app_logger, "start_log_cleanup_task") as start_cleanup,
                patch.object(app_logger, "get_logger", return_value=fake_logger),
            ):
                app_logger.initialize_logging(verbose=True)
                app_logger.initialize_logging(verbose=True)

            configure_third_party.assert_called_once_with()
            reconfigure_existing.assert_called_once_with()
            start_cleanup.assert_called_once_with(verbose=True)
            self.assertEqual(fake_logger.info.call_count, 4)
            self.assertTrue(app_logger._logging_initialized)
        finally:
            app_logger._logging_initialized = original_initialized
            app_logger.LOG_CONFIG = original_config

    def test_start_log_cleanup_task_starts_once_and_can_suppress_verbose_log(self) -> None:
        original_started = app_logger._cleanup_task_started
        fake_thread = Mock()
        fake_logger = Mock()
        try:
            app_logger._cleanup_task_started = False
            captured_targets = []

            def make_thread(*args, **kwargs):
                captured_targets.append(kwargs["target"])
                return fake_thread

            with (
                patch.object(app_logger.threading, "Thread", side_effect=make_thread) as thread_cls,
                patch.object(app_logger, "get_logger", return_value=fake_logger),
            ):
                app_logger.start_log_cleanup_task(verbose=True)
                app_logger.start_log_cleanup_task(verbose=True)

            thread_cls.assert_called_once()
            self.assertTrue(thread_cls.call_args.kwargs["daemon"])
            fake_thread.start.assert_called_once_with()
            fake_logger.info.assert_called_once()

            with (
                patch.object(app_logger, "cleanup_old_logs") as cleanup_old_logs,
                patch.object(app_logger.time, "sleep", side_effect=RuntimeError("stop loop")) as sleep,
            ):
                with self.assertRaises(RuntimeError):
                    captured_targets[0]()

            cleanup_old_logs.assert_called_once_with()
            sleep.assert_called_once_with(24 * 60 * 60)

            app_logger._cleanup_task_started = False
            fake_logger.reset_mock()
            with (
                patch.object(app_logger.threading, "Thread", return_value=fake_thread),
                patch.object(app_logger, "get_logger", return_value=fake_logger),
            ):
                app_logger.start_log_cleanup_task(verbose=False)

            fake_logger.info.assert_not_called()
        finally:
            app_logger._cleanup_task_started = original_started

    def test_shutdown_logging_closes_root_and_named_logger_handlers(self) -> None:
        root_logger = logging.getLogger()
        named_logger = logging.getLogger("unit_shutdown_logger")
        original_root_handlers = root_logger.handlers[:]
        original_named_handlers = named_logger.handlers[:]
        root_handler = Mock()
        named_handler = Mock()
        try:
            root_logger.handlers[:] = [root_handler]
            named_logger.handlers[:] = [named_handler]

            with patch.object(app_logger, "close_handlers") as close_handlers:
                app_logger.shutdown_logging()

            root_handler.close.assert_called_once_with()
            named_handler.close.assert_called_once_with()
            self.assertNotIn(root_handler, root_logger.handlers)
            self.assertNotIn(named_handler, named_logger.handlers)
            close_handlers.assert_called_once_with()
        finally:
            root_logger.handlers[:] = original_root_handlers
            named_logger.handlers[:] = original_named_handlers


if __name__ == "__main__":
    unittest.main()
