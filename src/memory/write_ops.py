# -*- coding: utf-8 -*-
"""
写操作日志与恢复机制

为记忆系统提供多步写入操作的日志记录、断点续传和崩溃恢复能力。
每次对 SQLite/Qdrant 的写入都记录为 WriteOp，支持重放和回滚。

设计原则：
- 轻量：纯 JSONL 文件 I/O，不依赖 Peewee/ORM
- 可恢复：所有失败操作可在启动时自动重放
- 原子性：通过 WriteOperation 上下文管理器自动标记完成/失败
- 自维护：自动裁剪超过阈值的已完成记录
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional

from src.common.logger import get_logger
from src.llm_models.embedding import embedding_source_hash
from src.memory.embedding_utils import generate_embedding
from src.memory.types import RollbackAction

logger = get_logger("memory.write_ops")


class OpStatus(str, Enum):
    """写操作状态枚举"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class OpType(str, Enum):
    """写操作类型枚举"""

    INSERT_ATOM = "insert_atom"
    UPDATE_ATOM = "update_atom"
    DELETE_ATOM = "delete_atom"
    BATCH_INSERT = "batch_insert"
    DREAM_CONSOLE = "dream_consolidation"
    ARCHIVE_ATOM = "archive_atom"
    MIGRATE_ATOM = "migrate_atom"


@dataclasses.dataclass
class WriteOp:
    """单次写操作记录，支持断点续传和回滚

    Attributes:
        op_id: 全局唯一操作ID
        op_type: 操作类型
        target: 目标存储 ("sqlite", "qdrant", "both")
        atom_ids: 关联的记忆原子ID列表
        payload: 操作负载（包含完整数据）
        status: 当前状态
        created_at: 创建时间戳（秒）
        started_at: 开始执行时间戳（秒）
        completed_at: 完成时间戳（秒）
        error_message: 错误信息
        retry_count: 已重试次数
        rollback_actions: 可重放的撤销步骤列表
    """

    op_id: str
    op_type: OpType
    target: str  # "sqlite", "qdrant", "both"
    atom_ids: list[str] = dataclasses.field(default_factory=list)
    payload: dict[str, Any] = dataclasses.field(default_factory=dict)
    status: OpStatus = OpStatus.PENDING
    created_at: float = dataclasses.field(default_factory=lambda: datetime.now().timestamp())
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error_message: Optional[str] = None
    retry_count: int = 0
    rollback_actions: list[RollbackAction] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 可序列化的字典"""
        d = dataclasses.asdict(self)
        d["op_type"] = self.op_type.value
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WriteOp":
        """从字典反序列化"""
        # 兼容旧格式：缺失字段使用默认值
        d["op_type"] = OpType(d["op_type"])
        d["status"] = OpStatus(d.get("status", "pending"))
        d.setdefault("atom_ids", [])
        d.setdefault("payload", {})
        d.setdefault("started_at", None)
        d.setdefault("completed_at", None)
        d.setdefault("error_message", None)
        d.setdefault("retry_count", 0)
        d.setdefault("rollback_actions", [])
        # created_at 兼容旧数据
        d.setdefault("created_at", datetime.now().timestamp())
        return cls(**d)

    @property
    def elapsed(self) -> Optional[float]:
        """操作耗时（秒），未完成则返回 None"""
        if self.started_at is None:
            return None
        end = self.completed_at or datetime.now().timestamp()
        return end - self.started_at

    @property
    def is_retriable(self) -> bool:
        """判断是否还可以重试（失败且未超过最大重试次数）"""
        return self.status == OpStatus.FAILED and self.retry_count < 3

    @property
    def is_terminal(self) -> bool:
        """是否为终态（不会再变更）"""
        return self.status in (OpStatus.COMPLETED, OpStatus.ROLLED_BACK)


def generate_op_id() -> str:
    """生成唯一操作ID

    格式: op_{毫秒时间戳}_{UUID短尾}，保证全局唯一且按时间有序
    """
    return f"op_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


def _default_log_path(db_path: str) -> str:
    """根据 SQLite 数据库路径推导 JSONL 日志路径"""
    if db_path.endswith(".db"):
        return db_path[:-3] + "_write_ops.jsonl"
    return db_path + "_write_ops.jsonl"


class WriteOpLogger:
    """写操作日志管理器 — 保证数据一致性

    使用 JSONL 文件记录所有写操作，支持：
    - 操作全生命周期追踪（pending → in_progress → completed/failed）
    - 崩溃后自动恢复失败操作
    - 自动清理过期已完成记录（不超过 max_entries 条）
    - 文件锁保障并发安全

    Args:
        db_path: SQLite 数据库路径，用于推导 JSONL 路径
        max_entries: 日志文件最大条目数，超过后自动裁剪
    """

    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB — 超过此大小触发日志轮转
    MAX_LINE_COUNT = 10000  # 10K 行 — 超过此行数触发日志轮转

    def __init__(self, db_path: str, max_entries: int = 10000):
        self.db_path = db_path
        self.log_file = _default_log_path(db_path)
        # 锁必须挂在独立文件上：_rotate_log 会 rename 数据文件，
        # 若把锁挂在数据文件的 inode 上，轮转前后的写入者会锁到两个不同的 inode，
        # 互斥语义直接失效。锁文件本身永不改名。
        self.lock_file = self.log_file + ".lock"
        self.max_entries = max_entries
        # 同进程内的可重入互斥：flock 是按 fd 生效的，
        # 同一进程重复 LOCK_EX 会把自己挂死（如 _append_op 内嵌套 _auto_trim）。
        self._thread_lock = threading.RLock()
        self._lock_depth = 0
        self._lock_handle: Optional[Any] = None
        # 确保日志目录存在
        log_dir = os.path.dirname(self.log_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

    # ── 文件 I/O ──────────────────────────────────────────────

    @contextlib.contextmanager
    def _exclusive_access(self) -> Iterator[None]:
        """持有 WAL 的跨进程排他锁，覆盖读取/截断/写入/轮转全过程。

        必须把「读-改-写」整体包进同一个临界区，否则两个并发的 _update_op
        会各自读到同一份快照、各自写回，后写者静默覆盖先写者的修改。
        本上下文可重入，嵌套调用只在最外层真正 flock。
        """

        with self._thread_lock:
            if self._lock_depth == 0:
                # 打不开锁文件时降级为「仅进程内互斥」，与 _acquire_lock 在
                # fcntl 不可用时的处理一致：锁是可用性增强，不该成为单点故障。
                try:
                    handle = open(self.lock_file, "a+", encoding="utf-8")
                except OSError:
                    logger.debug("打开写操作日志锁文件失败，降级为进程内互斥", exc_info=True)
                    handle = None
                if handle is not None:
                    try:
                        os.chmod(self.lock_file, 0o600)
                    except OSError:
                        pass
                    self._acquire_lock(handle)
                    self._lock_handle = handle
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
                if self._lock_depth == 0 and self._lock_handle is not None:
                    handle = self._lock_handle
                    self._lock_handle = None
                    try:
                        self._release_lock(handle)
                    finally:
                        handle.close()

    def _acquire_lock(self, f) -> None:
        """获取文件排他锁（fcntl.flock），保障并发安全"""
        try:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            # fcntl 不可用时（如 Windows），退化到无锁模式
            logger.debug("获取文件锁失败，继续使用无锁模式", exc_info=True)

    def _release_lock(self, f) -> None:
        """释放文件锁"""
        try:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            logger.debug("释放文件锁失败，继续使用无锁模式", exc_info=True)

    # ── 日志轮转 ──────────────────────────────────────────────

    def _check_rotate(self) -> None:
        """检查日志文件是否超过大小/行数阈值，若是则触发轮转"""
        with self._exclusive_access():
            if not os.path.exists(self.log_file):
                return

            # 快速路径：检查文件大小（O(1)）
            file_size = os.path.getsize(self.log_file)
            if file_size < self.MAX_FILE_SIZE:
                # 大小未超限，再检查行数（需读取文件）
                try:
                    with open(self.log_file, "r", encoding="utf-8") as f:
                        line_count = sum(1 for _ in f)
                except OSError:
                    return
                if line_count < self.MAX_LINE_COUNT:
                    return

            self._rotate_log()

    def _rotate_log(self) -> None:
        """执行日志轮转，并保留可恢复操作在活动 WAL 中。

        将当前 memory_write_ops.jsonl 重命名为 .jsonl.1 / .jsonl.2 / ...，
        然后将未终态记录写入新的活动文件，确保启动恢复不依赖轮转归档。
        超过 50 个轮转文件时发出告警并裁剪最旧文件。
        """
        base = self.log_file
        try:
            recoverable_ops = [op for op in self._read_all_ops() if not op.is_terminal]
        except OSError as e:
            logger.warning(
                "write_ops 日志轮转前读取失败（保留原文件继续写入）",
                extra={"error": str(e), "path": self.log_file},
            )
            return

        # 寻找下一个可用序号
        n = 1
        while os.path.exists(f"{base}.{n}"):
            n += 1

        new_path = f"{base}.{n}"
        try:
            os.rename(self.log_file, new_path)
        except OSError as e:
            logger.warning(
                "write_ops 日志轮转失败（保留原文件继续写入）",
                extra={"error": str(e), "path": self.log_file},
            )
            # 轮转失败时不中断，继续写入原文件
            return
        # 新活动 WAL 必须先耐久写入可恢复记录；失败时还原归档，避免恢复记录丢失。
        try:
            with open(self.log_file, "w", encoding="utf-8") as f:
                for op in recoverable_ops:
                    f.write(json.dumps(op.to_dict(), ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            logger.warning(
                "write_ops 日志轮转后重建活动文件失败，尝试还原归档",
                extra={"error": str(e), "path": self.log_file},
            )
            try:
                if os.path.exists(self.log_file):
                    os.remove(self.log_file)
                os.rename(new_path, self.log_file)
            except OSError:
                logger.exception(
                    "write_ops 日志轮转回滚失败，恢复记录保留在归档文件中",
                    extra={"archive": new_path, "path": self.log_file},
                )
            return

        logger.info(
            "write_ops 日志轮转",
            extra={"old": new_path, "new": self.log_file, "rotation_number": n},
        )

        # 超过 50 个备份文件时，裁剪最旧的那个
        if n > 50:
            first = f"{base}.1"
            if os.path.exists(first):
                try:
                    os.remove(first)
                    logger.warning(
                        "write_ops 日志：轮转文件过多，已删除最旧备份",
                        extra={"removed": first, "remaining": n - 1},
                    )
                except OSError as e:
                    logger.warning("删除最旧轮转文件失败", extra={"path": first, "error": str(e)})

    def _read_all_ops(self) -> list[WriteOp]:
        """从 JSONL 文件读取所有操作记录（带文件锁）"""
        with self._exclusive_access():
            if not os.path.exists(self.log_file):
                return []
            ops: list[WriteOp] = []
            with open(self.log_file, "r", encoding="utf-8") as f:
                for line_num, raw in enumerate(f, 1):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                        ops.append(WriteOp.from_dict(data))
                    except (json.JSONDecodeError, KeyError, ValueError) as e:
                        logger.warning(
                            "解析写操作日志行失败",
                            extra={
                                "line": line_num,
                                "error": str(e),
                                "content_preview": raw[:120],
                            },
                        )
            return ops

    def _write_all_ops(self, ops: list[WriteOp]) -> None:
        """将操作记录全量写回 JSONL 文件（带文件锁）

        截断必须发生在持锁之后：`open(path, "w")` 会在返回前就清空文件，
        若先 open 再 flock，等锁期间日志已经没了，锁等于没加。
        """
        with self._exclusive_access():
            self._check_rotate()
            with open(self.log_file, "w", encoding="utf-8") as f:
                for op in ops:
                    f.write(json.dumps(op.to_dict(), ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())

    def _append_op(self, op: WriteOp) -> None:
        """追加一条新操作记录到文件末尾（带文件锁）"""
        with self._exclusive_access():
            self._check_rotate()
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(op.to_dict(), ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            # 每次追加后尝试裁剪，防止累积过多已完成记录
            self._auto_trim()

    def _update_op(self, op_id: str, **updates: Any) -> Optional[WriteOp]:
        """更新指定操作的状态字段（读-改-写模式，全程持锁）

        返回更新后的 WriteOp，若未找到则返回 None。
        """
        # 读和写之间不能放锁：否则两个并发更新会各自读到同一份快照再写回，
        # 后写者会把先写者的状态变更整条覆盖掉。
        with self._exclusive_access():
            ops = self._read_all_ops()
            found = None
            for i, op in enumerate(ops):
                if op.op_id == op_id:
                    for key, val in updates.items():
                        if hasattr(op, key):
                            setattr(op, key, val)
                    ops[i] = op
                    found = op
                    break
            if found:
                self._write_all_ops(ops)
                self._auto_trim(ops)
            return found

    def _auto_trim(self, ops: Optional[list[WriteOp]] = None) -> int:
        """自动裁剪超过阈值的已完成/已回滚记录

        Returns:
            被裁剪的条目数
        """
        with self._exclusive_access():
            if ops is None:
                ops = self._read_all_ops()
            if len(ops) <= self.max_entries:
                return 0

            # 区分可裁剪（终态）和不可裁剪（非终态）的记录
            retainable = [op for op in ops if not op.is_terminal]
            cleanable = [op for op in ops if op.is_terminal]

            # 按创建时间排序（最旧优先裁剪）
            cleanable.sort(key=lambda op: op.created_at)

            # 裁剪直到总条数 <= max_entries 或无可裁剪条目
            to_remove = len(ops) - self.max_entries
            trimmed = 0
            while to_remove > 0 and cleanable:
                cleanable.pop(0)
                to_remove -= 1
                trimmed += 1

            # Pending/failed operations must remain recoverable. If none can be
            # removed, rewriting and fsyncing the identical WAL adds no safety.
            if trimmed == 0:
                return 0

            remaining = retainable + cleanable
            # 按 created_at 稳定排序，保持原有顺序
            remaining.sort(key=lambda op: op.created_at)
            self._write_all_ops(remaining)
            logger.info(
                "自动裁剪写操作日志",
                extra={
                    "trimmed": trimmed,
                    "remaining": len(remaining),
                    "max_entries": self.max_entries,
                },
            )
            return trimmed

    # ── 操作生命周期管理 ─────────────────────────────────────

    def log_op(self, op: WriteOp) -> str:
        """记录一次写操作，返回 op_id"""
        self._append_op(op)
        logger.debug(
            "记录写操作",
            extra={
                "op_id": op.op_id,
                "op_type": op.op_type.value,
                "target": op.target,
            },
        )
        return op.op_id

    def mark_started(self, op_id: str) -> Optional[WriteOp]:
        """标记操作开始执行"""
        return self._update_op(op_id, status=OpStatus.IN_PROGRESS, started_at=datetime.now().timestamp())

    def mark_completed(self, op_id: str) -> Optional[WriteOp]:
        """标记操作成功完成"""
        result = self._update_op(op_id, status=OpStatus.COMPLETED, completed_at=datetime.now().timestamp())
        if result:
            logger.debug("写操作完成", extra={"op_id": op_id})
        return result

    def mark_failed(self, op_id: str, error: str) -> Optional[WriteOp]:
        """标记操作失败"""
        result = self._update_op(
            op_id, status=OpStatus.FAILED, completed_at=datetime.now().timestamp(), error_message=error
        )
        if result:
            logger.warning(
                "写操作失败",
                extra={
                    "op_id": op_id,
                    "error": error,
                    "retry_count": result.retry_count,
                },
            )
        return result

    # ── 查询方法 ─────────────────────────────────────────────

    def get_pending_ops(self) -> list[WriteOp]:
        """获取所有未完成的操作（用于启动恢复）"""
        ops = self._read_all_ops()
        return [op for op in ops if op.status in (OpStatus.PENDING, OpStatus.IN_PROGRESS)]

    def get_failed_ops(self, max_retries: int = 3) -> list[WriteOp]:
        """获取可重试的失败操作

        Args:
            max_retries: 最大重试次数阈值（retry_count < max_retries 的才返回）

        Returns:
            可重试的失败操作列表
        """
        ops = self._read_all_ops()
        return [op for op in ops if op.status == OpStatus.FAILED and op.retry_count < max_retries]

    def get_recoverable_ops(self, max_retries: int = 3) -> list[WriteOp]:
        """获取启动恢复时可重放的操作。

        包含失败但未超过重试上限的操作，以及上次进程崩溃时可能停在 pending/in_progress 的操作。
        """
        ops = self._read_all_ops()
        recoverable = [
            op
            for op in ops
            if op.retry_count < max_retries and op.status in (OpStatus.PENDING, OpStatus.IN_PROGRESS, OpStatus.FAILED)
        ]
        return sorted(recoverable, key=lambda op: op.created_at)

    def get_op(self, op_id: str) -> Optional[WriteOp]:
        """根据 op_id 查找单个操作记录"""
        ops = self._read_all_ops()
        for op in ops:
            if op.op_id == op_id:
                return op
        return None

    def get_inconsistent_ops(self) -> list[tuple[WriteOp, WriteOp]]:
        """查找所有不一致的写操作对（一侧成功、一侧失败）

        遍历日志，寻找针对同一组 atom_ids 和 op_type 但 target 不同的操作对：
        例如 INSERT_ATOM 的 SQLite 写入成功但 Qdrant 写入失败。
        只有一侧成功、另一侧失败时才会被识别为不一致。

        Returns:
            不一致的操作对列表，每对为 (成功侧操作, 失败侧操作)
        """
        ops = self._read_all_ops()
        # 只关注终态操作（completed / failed）
        completed = [op for op in ops if op.status == OpStatus.COMPLETED]
        failed = [op for op in ops if op.status == OpStatus.FAILED]
        inconsistent: list[tuple[WriteOp, WriteOp]] = []

        # 使用 (op_type, frozenset(atom_ids)) 作为键进行关联
        for failed_op in failed:
            if not failed_op.atom_ids:
                continue
            key = (failed_op.op_type.value, frozenset(failed_op.atom_ids))
            for completed_op in completed:
                if completed_op.op_type.value != key[0]:
                    continue
                if frozenset(completed_op.atom_ids) != key[1]:
                    continue
                if completed_op.target == failed_op.target:
                    # 同一 target 的 completed/failed 不是跨存储不一致
                    continue
                # 找到匹配对：SQLite vs Qdrant 一侧成功一侧失败
                inconsistent.append((completed_op, failed_op))
                break

        return inconsistent

    # ── 恢复机制 ─────────────────────────────────────────────

    async def replay_failed_ops(self, store) -> list[str]:
        """重放所有可恢复操作，返回成功恢复的 op_id 列表

        启动时调用，自动恢复上一次崩溃时未完成或失败的写入。

        Args:
            store: MemoryStore 实例，需提供 insert_atom / update_atom / delete_atom 等方法

        Returns:
            成功恢复的操作 ID 列表
        """
        recovered: list[str] = []
        recoverable_ops = self.get_recoverable_ops(max_retries=3)

        if not recoverable_ops:
            logger.info("无可恢复操作需要重放")
            return recovered

        logger.info("开始重放可恢复操作", extra={"count": len(recoverable_ops)})

        for op in recoverable_ops:
            op.retry_count += 1
            self._update_op(
                op.op_id,
                status=OpStatus.IN_PROGRESS,
                retry_count=op.retry_count,
                started_at=datetime.now().timestamp(),
                error_message=None,
            )
            try:
                await self._dispatch_replay(op, store)
                self.mark_completed(op.op_id)
                recovered.append(op.op_id)
                logger.info("操作重放成功", extra={"op_id": op.op_id})
            except Exception as e:
                self.mark_failed(op.op_id, str(e))
                logger.error(
                    "操作重放失败",
                    extra={
                        "op_id": op.op_id,
                        "error": str(e),
                        "retry_count": op.retry_count,
                    },
                )

        logger.info(
            "操作重放完成",
            extra={
                "recovered": len(recovered),
                "total": len(recoverable_ops),
            },
        )
        return recovered

    @staticmethod
    def _replay_targets(op: WriteOp) -> set[str]:
        target = (op.target or "").lower()
        if target in {"both", "all"}:
            return {"sqlite", "qdrant"}
        if target in {"sqlite", "qdrant"}:
            return {target}
        return {"sqlite", "qdrant"}

    @staticmethod
    def _atom_vector_payload(atom_id: str, atom: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "atom_id": atom_id,
            "atom_type": atom.get("atom_type", "factual"),
            "user_id": atom.get("user_id"),
            "group_id": atom.get("group_id"),
            "weight": atom.get("weight", 0.5),
            "importance": atom.get("importance", 0.5),
            "confidence": atom.get("confidence", 0.5),
            "status": atom.get("status", "active"),
            "source_scene": atom.get("source_scene", "chat"),
            "source_id": atom.get("source_id"),
            "privacy_level": atom.get("privacy_level", "context_sensitive"),
        }
        content = atom.get("content")
        if isinstance(content, str) and content:
            payload["embedding_source_hash"] = embedding_source_hash(content)
        return payload

    async def _ensure_sqlite_atom(self, store, atom: dict[str, Any]) -> str:
        atom_id = atom.get("atom_id")
        if not atom_id:
            raise ValueError("atom payload 缺少 atom_id")

        existing = await store.get_atom(atom_id)
        if existing is not None:
            return atom_id
        return await store.insert_atom(atom)

    async def _upsert_qdrant_atom(self, store, atom_id: str, atom: dict[str, Any]) -> None:
        content = atom.get("content", "")
        if not content:
            raise ValueError(f"Qdrant replay 缺少 content，无法生成 embedding: {atom_id}")

        # Operation logs can outlive an embedding-model change.  Never trust
        # a persisted vector here: regenerate it through the canonical service
        # so replay cannot label an old vector as belonging to the new space.
        embedding = await generate_embedding(content)
        if not embedding:
            raise RuntimeError(f"Qdrant replay 生成 embedding 失败: {atom_id}")

        ok = await store.qdrant.upsert_atom_vector(
            point_id=atom_id,
            vector=embedding,
            payload=self._atom_vector_payload(atom_id, atom),
        )
        if not ok:
            raise RuntimeError(f"Qdrant replay upsert 失败: {atom_id}")

    async def _dispatch_replay(self, op: WriteOp, store) -> None:
        """根据操作类型分发到 store 的对应方法"""
        # 避免循环导入：store 作为参数传入，此处动态分发
        targets = self._replay_targets(op)

        if op.op_type == OpType.INSERT_ATOM:
            atom = op.payload.get("atom")
            if atom is None:
                raise ValueError("INSERT_ATOM payload 缺少 atom 字段")
            atom_id = atom.get("atom_id") or (op.atom_ids[0] if op.atom_ids else None)
            if not atom_id:
                raise ValueError("INSERT_ATOM 缺少 atom_id")
            if "sqlite" in targets:
                atom_id = await self._ensure_sqlite_atom(store, atom)
            if "qdrant" in targets:
                existing = await store.get_atom(atom_id)
                await self._upsert_qdrant_atom(store, atom_id, existing or atom)

        elif op.op_type == OpType.UPDATE_ATOM:
            if not op.atom_ids:
                raise ValueError("UPDATE_ATOM 需要 atom_ids")
            updates = op.payload.get("updates", {})
            atom_id = op.atom_ids[0]
            if "sqlite" in targets:
                await store.update_atom(atom_id, updates)
            if "qdrant" in targets:
                if "content" in updates:
                    current_atom = await store.get_atom(atom_id)
                    if current_atom is None:
                        raise ValueError(f"UPDATE_ATOM replay 找不到原子: {atom_id}")
                    await self._upsert_qdrant_atom(store, atom_id, current_atom)
                else:
                    qdrant_updates = {}
                    for key in (
                        "weight",
                        "importance",
                        "confidence",
                        "status",
                        "privacy_level",
                        "source_scene",
                        "source_id",
                    ):
                        if key in updates:
                            qdrant_updates[key] = updates[key]
                    if qdrant_updates:
                        ok = await store.qdrant.set_atom_payload(atom_id, qdrant_updates)
                        if not ok:
                            raise RuntimeError(f"Qdrant replay payload 更新失败: {atom_id}")

        elif op.op_type == OpType.DELETE_ATOM:
            if not op.atom_ids:
                raise ValueError("DELETE_ATOM 需要 atom_ids")
            atom_id = op.atom_ids[0]
            if "sqlite" in targets:
                await store.delete_atom(atom_id)
            if "qdrant" in targets:
                ok = await store.qdrant.delete_atom_vector(atom_id)
                if not ok:
                    raise RuntimeError(f"Qdrant replay 删除失败: {atom_id}")

        elif op.op_type == OpType.BATCH_INSERT:
            # 从 payload 读取原子数据（由 MemoryWriter.batch_write 存入，使用 "atom" 键）
            atoms = []
            single_atom = op.payload.get("atom")
            if single_atom:
                atoms = [single_atom]
            else:
                atoms = op.payload.get("atoms", [])  # 兼容旧格式
            if atoms:
                # 有完整数据：重新插入 SQLite 并同步 Qdrant
                for atom_data in atoms:
                    atom_id = atom_data.get("atom_id", "")
                    if not atom_id:
                        raise ValueError("BATCH_INSERT atom payload 缺少 atom_id")
                    if "sqlite" in targets:
                        await self._ensure_sqlite_atom(store, atom_data)
                    if "qdrant" in targets:
                        existing = await store.get_atom(atom_id)
                        await self._upsert_qdrant_atom(store, atom_id, existing or atom_data)
            else:
                # 无 payload 数据：尝试从 SQLite 按 atom_ids 读取，仅补 Qdrant
                logger.warning("BATCH_INSERT replay payload 缺少 atoms 字段，尝试从 SQLite 兜底")
                if "sqlite" in targets:
                    raise ValueError("BATCH_INSERT 缺少 payload，无法恢复 SQLite 插入")
                for aid in op.atom_ids:
                    existing = await store.get_atom(aid)
                    if existing is None:
                        raise ValueError(f"BATCH_INSERT replay 找不到原子: {aid}")
                    await self._upsert_qdrant_atom(store, aid, existing)

        elif op.op_type == OpType.DREAM_CONSOLE:
            # 梦境巩固重放 — 记录到日志，由梦境系统自行处理一致性
            logger.info("梦境巩固重放跳过（由梦境系统自行处理）", extra={"op_id": op.op_id})

        elif op.op_type == OpType.ARCHIVE_ATOM:
            if not op.atom_ids:
                raise ValueError("ARCHIVE_ATOM 需要 atom_ids")
            if not await store.archive_atom(op.atom_ids[0]):
                raise RuntimeError(f"ARCHIVE_ATOM 未处理任何原子: {op.atom_ids[0]}")

        elif op.op_type == OpType.MIGRATE_ATOM:
            if not op.atom_ids:
                raise ValueError("MIGRATE_ATOM 需要 atom_ids")
            target_type = op.payload.get("target_type", "")
            if not await store.migrate_atom(op.atom_ids[0], target_type):
                raise RuntimeError(f"MIGRATE_ATOM 未处理任何原子: {op.atom_ids[0]} -> {target_type}")

        else:
            logger.warning(
                "未知操作类型，跳过重放",
                extra={
                    "op_id": op.op_id,
                    "op_type": str(op.op_type),
                },
            )

    # ── 维护 ─────────────────────────────────────────────────

    def cleanup_completed(self, older_than_days: int = 7) -> int:
        """清理超过 N 天的已完成操作记录

        Args:
            older_than_days: 保留天数，超过此天数的已完成记录将被删除

        Returns:
            清理的记录数
        """
        cutoff = datetime.now().timestamp() - older_than_days * 86400
        ops = self._read_all_ops()
        before = len(ops)
        ops = [op for op in ops if not (op.is_terminal and op.completed_at is not None and op.completed_at < cutoff)]
        removed = before - len(ops)
        if removed:
            self._write_all_ops(ops)
            logger.info(
                "清理旧写操作日志",
                extra={
                    "removed": removed,
                    "remaining": len(ops),
                    "older_than_days": older_than_days,
                },
            )
        return removed

    def get_stats(self) -> dict[str, Any]:
        """返回写操作统计

        Returns:
            包含各类操作数量和状态的统计字典
        """
        ops = self._read_all_ops()
        if not ops:
            return {
                "total_ops": 0,
                "by_status": {},
                "by_type": {},
                "failed_count": 0,
                "pending_count": 0,
                "oldest_op": None,
                "newest_op": None,
                "file_size_bytes": 0,
            }

        by_status: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for op in ops:
            by_status[op.status.value] = by_status.get(op.status.value, 0) + 1
            by_type[op.op_type.value] = by_type.get(op.op_type.value, 0) + 1

        timestamps = [op.created_at for op in ops]
        file_size = os.path.getsize(self.log_file) if os.path.exists(self.log_file) else 0

        return {
            "total_ops": len(ops),
            "by_status": by_status,
            "by_type": by_type,
            "failed_count": by_status.get("failed", 0),
            "pending_count": by_status.get("pending", 0) + by_status.get("in_progress", 0),
            "oldest_op": min(timestamps),
            "newest_op": max(timestamps),
            "file_size_bytes": file_size,
            "log_file": self.log_file,
        }


class WriteOperation:
    """上下文管理器 — 自动记录、执行、标记写操作

    使用示例:
        async with WriteOperation(logger, OpType.INSERT_ATOM, "both",
                                   atom_ids=[atom.atom_id],
                                   payload={"atom": atom}) as op:
            await store.insert_atom(atom)
            await qdrant_client.upsert(...)
        # 退出上下文时自动标记 completed 或 failed

    Args:
        op_logger: WriteOpLogger 实例
        op_type: 操作类型
        target: 目标存储
        atom_ids: 关联的记忆原子ID列表
        payload: 操作负载
    """

    def __init__(
        self,
        op_logger: WriteOpLogger,
        op_type: OpType,
        target: str,
        atom_ids: Optional[list[str]] = None,
        payload: Optional[dict[str, Any]] = None,
    ):
        self.logger = op_logger
        self.op = WriteOp(
            op_id=generate_op_id(),
            op_type=op_type,
            target=target,
            atom_ids=atom_ids or [],
            payload=payload or {},
        )

    async def __aenter__(self) -> WriteOp:
        """进入上下文：记录操作并标记为进行中"""
        if self.logger is not None:
            self.logger.log_op(self.op)
            self.logger.mark_started(self.op.op_id)
        return self.op

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """退出上下文：根据异常情况标记完成或失败"""
        if self.logger is not None:
            if exc_type is None:
                self.logger.mark_completed(self.op.op_id)
            else:
                error_msg = f"{exc_type.__name__}: {exc_val}" if exc_val else str(exc_type)
                self.logger.mark_failed(self.op.op_id, error_msg)
        if exc_type is not None:
            return False  # 不吞异常，继续向上传播
        return True


def ensure_atomic_write(
    success_condition: Callable[[Any], bool],
    rollback_fn: Callable[[Any], None],
):
    """装饰器 — 确保原子性写入

    在函数执行后检查 success_condition，若返回 False 则执行 rollback_fn 回滚。

    Args:
        success_condition: 接收函数返回值，返回 True 表示写入成功
        rollback_fn: 接收函数返回值，执行回滚操作

    Returns:
        装饰器函数

    使用示例:
        @ensure_atomic_write(
            success_condition=lambda r: r is not None,
            rollback_fn=lambda r: cleanup_partial_write(r),
        )
        def write_atom(atom):
            ...
    """

    def decorator(func):
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            if not success_condition(result):
                rollback_fn(result)
            return result

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            result = await func(*args, **kwargs)
            if not success_condition(result):
                rollback_fn(result)
            return result

        # 判断是否为异步函数
        import inspect

        if inspect.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator
