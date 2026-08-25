import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from src.webui import plugin_runtime_state


class PluginRuntimeStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.current = self.root / "current"
        self.staged = self.root / "staged"
        self.current.mkdir()
        self.staged.mkdir()

    def test_collect_rejects_hard_links_and_entry_overflow(self) -> None:
        config = self.current / "config.toml"
        config.write_text("enabled = true\n", encoding="utf-8")
        os.link(config, self.root / "linked-config.toml")

        with self.assertRaises(HTTPException) as hard_link:
            plugin_runtime_state.collect_plugin_runtime_entries(
                self.current,
                self.staged,
                plugin_runtime_state.DEFAULT_PLUGIN_RUNTIME_CONFIG_FILES,
            )

        self.assertEqual(hard_link.exception.status_code, 400)

        config.unlink()
        (self.current / "data").mkdir()
        (self.current / "data" / "one.db").write_text("one", encoding="utf-8")
        (self.current / "data" / "two.db").write_text("two", encoding="utf-8")
        with (
            patch.object(plugin_runtime_state, "MAX_PLUGIN_RUNTIME_ENTRIES", 2),
            self.assertRaises(HTTPException) as overflow,
        ):
            plugin_runtime_state.collect_plugin_runtime_entries(
                self.current,
                self.staged,
                plugin_runtime_state.DEFAULT_PLUGIN_RUNTIME_CONFIG_FILES,
            )

        self.assertEqual(overflow.exception.status_code, 413)

    def test_move_rolls_back_entries_when_a_later_move_fails(self) -> None:
        (self.current / "config.toml").write_text("config", encoding="utf-8")
        (self.current / "data").mkdir()
        (self.current / "data" / "state.db").write_text("database", encoding="utf-8")
        real_replace = os.replace
        replace_calls = 0

        def fail_second_move(source, destination):
            nonlocal replace_calls
            replace_calls += 1
            if replace_calls == 2:
                raise OSError("simulated move failure")
            return real_replace(source, destination)

        with (
            patch.object(plugin_runtime_state.os, "replace", side_effect=fail_second_move),
            self.assertRaises(OSError),
        ):
            plugin_runtime_state.move_plugin_runtime_entries(
                self.current,
                self.staged,
                ("config.toml", "data"),
            )

        self.assertEqual((self.current / "config.toml").read_text(encoding="utf-8"), "config")
        self.assertEqual((self.current / "data" / "state.db").read_text(encoding="utf-8"), "database")
        self.assertFalse((self.staged / "config.toml").exists())
        self.assertFalse((self.staged / "data").exists())


if __name__ == "__main__":
    unittest.main()
