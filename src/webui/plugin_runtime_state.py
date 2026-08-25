"""Controlled runtime-state migration for plugin directory replacements."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from fastapi import HTTPException

from src.common.logger import get_logger


logger = get_logger("webui.plugin_runtime_state")

MAX_PLUGIN_RUNTIME_ENTRIES = 100_000
DEFAULT_PLUGIN_RUNTIME_CONFIG_FILES = frozenset({"config.toml", "plugin_config.toml"})
PLUGIN_RUNTIME_DIRECTORIES = frozenset({"config_backup", "data", "logs"})


def _is_runtime_entry(name: str, config_files: frozenset[str]) -> bool:
    if name in PLUGIN_RUNTIME_DIRECTORIES or name in config_files:
        return True
    return any(
        name.startswith((f"{config_file}.backup.", f"{config_file}.backup_", f"{config_file}.reset."))
        for config_file in config_files
    )


def _validate_runtime_entry(path: Path, *, directory: bool) -> int:
    try:
        entry_stat = os.lstat(path)
    except OSError as exc:
        raise HTTPException(status_code=400, detail="插件运行数据路径无效") from exc
    if stat.S_ISLNK(entry_stat.st_mode):
        raise HTTPException(status_code=400, detail="插件运行数据不能包含符号链接")
    if directory:
        if not stat.S_ISDIR(entry_stat.st_mode):
            raise HTTPException(status_code=400, detail="插件运行数据目录无效")
    elif not stat.S_ISREG(entry_stat.st_mode):
        raise HTTPException(status_code=400, detail="插件运行配置路径无效")
    elif entry_stat.st_nlink > 1:
        raise HTTPException(status_code=400, detail="插件运行配置不能是硬链接")
    return 1


def _validate_runtime_directory(path: Path) -> int:
    entry_count = _validate_runtime_entry(path, directory=True)
    for current_root, directory_names, file_names in os.walk(path, topdown=True, followlinks=False):
        current_path = Path(current_root)
        for name in directory_names:
            entry_count += _validate_runtime_entry(current_path / name, directory=True)
            if entry_count > MAX_PLUGIN_RUNTIME_ENTRIES:
                raise HTTPException(status_code=413, detail="插件运行数据文件数量超过限制")
        for name in file_names:
            entry_count += _validate_runtime_entry(current_path / name, directory=False)
            if entry_count > MAX_PLUGIN_RUNTIME_ENTRIES:
                raise HTTPException(status_code=413, detail="插件运行数据文件数量超过限制")
    return entry_count


def collect_plugin_runtime_entries(
    plugin_path: Path,
    staged_path: Path,
    config_files: frozenset[str],
) -> tuple[str, ...]:
    """Return validated top-level runtime entries that can move to a new version."""
    runtime_entries: list[str] = []
    total_entries = 0
    try:
        candidates = sorted(plugin_path.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise HTTPException(status_code=400, detail="插件运行数据路径无效") from exc

    for candidate in candidates:
        if not _is_runtime_entry(candidate.name, config_files):
            continue
        if os.path.lexists(staged_path / candidate.name):
            raise HTTPException(status_code=409, detail="新版本包含与本地运行数据同名的文件")
        if candidate.name in PLUGIN_RUNTIME_DIRECTORIES:
            total_entries += _validate_runtime_directory(candidate)
        else:
            total_entries += _validate_runtime_entry(candidate, directory=False)
        if total_entries > MAX_PLUGIN_RUNTIME_ENTRIES:
            raise HTTPException(status_code=413, detail="插件运行数据文件数量超过限制")
        runtime_entries.append(candidate.name)
    return tuple(runtime_entries)


def move_plugin_runtime_entries(source: Path, target: Path, runtime_entries: tuple[str, ...]) -> None:
    """Move validated entries and undo the partial move if any entry fails."""
    moved_entries: list[str] = []
    try:
        for name in runtime_entries:
            source_path = source / name
            target_path = target / name
            if os.path.lexists(target_path):
                raise HTTPException(status_code=409, detail="插件运行数据目标已存在")
            os.replace(source_path, target_path)
            moved_entries.append(name)
    except BaseException:
        for name in reversed(moved_entries):
            try:
                os.replace(target / name, source / name)
            except OSError as rollback_error:
                logger.critical("插件运行数据移动回滚失败", error_type=type(rollback_error).__name__)
        raise


def restore_plugin_runtime_entries(source: Path, target: Path, runtime_entries: tuple[str, ...]) -> None:
    """Collect runtime entries in the restore target without discarding a partially rolled-back move."""
    moved_entries: list[str] = []
    try:
        for name in runtime_entries:
            source_path = source / name
            target_path = target / name
            source_exists = os.path.lexists(source_path)
            target_exists = os.path.lexists(target_path)
            if source_exists and target_exists:
                raise HTTPException(status_code=409, detail="插件运行数据在新旧版本中同时存在")
            if not source_exists and not target_exists:
                raise HTTPException(status_code=409, detail="插件运行数据在新旧版本中均不存在")
            if target_exists:
                continue
            os.replace(source_path, target_path)
            moved_entries.append(name)
    except BaseException:
        for name in reversed(moved_entries):
            try:
                os.replace(target / name, source / name)
            except OSError as rollback_error:
                logger.critical("插件运行数据恢复回滚失败", error_type=type(rollback_error).__name__)
        raise


__all__ = [
    "DEFAULT_PLUGIN_RUNTIME_CONFIG_FILES",
    "collect_plugin_runtime_entries",
    "move_plugin_runtime_entries",
    "restore_plugin_runtime_entries",
]
