import ast
import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_bot_functions():
    """Load lifecycle functions without starting the application or reading runtime state."""
    tree = ast.parse((ROOT / "bot.py").read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    namespace = {
        "asyncio": asyncio,
        "os": os,
        "sys": sys,
        "Path": Path,
        "subprocess": subprocess,
        "time": SimpleNamespace(sleep=Mock(), monotonic=Mock(return_value=0)),
        "logger": Mock(),
        "RESTART_EXIT_CODE": 42,
        "UPDATE_EXIT_CODE": 43,
        "script_dir": str(ROOT),
        "shutdown_logging": Mock(),
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(ROOT / "bot.py"), "exec"), namespace)
    return namespace


class LifecycleAuditTest(unittest.TestCase):
    def test_import_does_not_exit_or_start_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", "import bot; print('import completed')"],
                cwd=directory,
                env={**os.environ, "PYTHONPATH": str(ROOT), "MAIBOT_WORKER_PROCESS": "0"},
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertFalse((Path(directory) / "config").exists())
            self.assertFalse((Path(directory) / ".env").exists())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("import completed", result.stdout)

    def test_runner_recovers_after_crash_and_stops_on_normal_exit(self):
        namespace = load_bot_functions()
        processes = [Mock(wait=Mock(return_value=1)), Mock(wait=Mock(return_value=0))]
        with patch.object(subprocess, "Popen", side_effect=processes) as popen:
            with self.assertRaises(SystemExit) as exit_result:
                namespace["run_runner_process"]()
        self.assertEqual(exit_result.exception.code, 0)
        self.assertEqual(popen.call_count, 2)
        namespace["time"].sleep.assert_called_once_with(1)

    def test_runner_uses_absolute_entry_after_changing_directory(self):
        namespace = load_bot_functions()
        with patch.object(sys, "argv", ["riyabot/bot.py", "--example"]):
            with patch.object(subprocess, "Popen", return_value=Mock(wait=Mock(return_value=0))) as popen:
                with self.assertRaises(SystemExit):
                    namespace["run_runner_process"]()
        self.assertEqual(popen.call_args.args[0], [sys.executable, str(ROOT / "bot.py"), "--example"])

    def test_runner_backs_off_repeated_crashes_and_resets_after_stable_run(self):
        namespace = load_bot_functions()
        codes = [1] * 8 + [1, 0]
        namespace["time"].monotonic.side_effect = [0] * 16 + [0, 61, 61]
        with patch.object(subprocess, "Popen", side_effect=[Mock(wait=Mock(return_value=code)) for code in codes]):
            with self.assertRaises(SystemExit):
                namespace["run_runner_process"]()
        self.assertEqual(
            [call.args[0] for call in namespace["time"].sleep.call_args_list], [1, 2, 4, 8, 16, 32, 60, 60, 1]
        )

    def test_worker_normal_and_controlled_exits_close_the_loop(self):
        for exit_code in (0, 42, 43):
            with self.subTest(exit_code=exit_code):
                namespace = load_bot_functions()
                schedule = AsyncMock(side_effect=SystemExit(exit_code) if exit_code else None)
                namespace["raw_main"] = Mock(
                    return_value=SimpleNamespace(initialize=AsyncMock(), schedule_tasks=schedule)
                )
                namespace["graceful_shutdown"] = AsyncMock()
                loop = asyncio.new_event_loop()
                with patch.object(asyncio, "new_event_loop", return_value=loop):
                    with patch("src.common.logger.initialize_ws_handler"):
                        actual = namespace["run_worker_process"]()
                self.assertEqual(actual, exit_code)
                namespace["graceful_shutdown"].assert_awaited_once()
                self.assertTrue(loop.is_closed())

    def test_worker_initialization_failure_still_shuts_down(self):
        namespace = load_bot_functions()
        system = SimpleNamespace(initialize=AsyncMock(side_effect=RuntimeError("init failed")))
        namespace["raw_main"] = Mock(return_value=system)
        namespace["graceful_shutdown"] = AsyncMock()
        with patch("src.common.logger.initialize_ws_handler"):
            code = namespace["run_worker_process"]()
        self.assertEqual(code, 1)
        namespace["graceful_shutdown"].assert_awaited_once()

    def test_dotenv_preserves_service_environment(self):
        namespace = load_bot_functions()
        from dotenv import load_dotenv

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("AUDIT_ENV_TEST=file\n", encoding="utf-8")
            namespace.update(
                __file__=str(root / "bot.py"),
                script_dir=str(root),
                load_dotenv=load_dotenv,
                initialize_logging=Mock(),
                install=Mock(),
            )
            with patch.dict(os.environ, {"AUDIT_ENV_TEST": "service"}), patch.object(os, "chdir"):
                namespace["initialize_runtime"]()
                self.assertEqual(os.environ["AUDIT_ENV_TEST"], "service")


if __name__ == "__main__":
    unittest.main()
