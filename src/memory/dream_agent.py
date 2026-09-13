"""
DreamAgent — 梦境维护后台任务 (Phase 2C)

闲时运行记忆巩固、噪声清理、图谱构建。
所有操作基于启发式规则，不调用 LLM。

DreamTask 作为 AsyncTask 子类运行，由 async_task_manager 调度。
"""

from __future__ import annotations

import datetime
import asyncio
from bisect import insort
import hashlib
import json
import re
import time
from enum import Enum
from typing import Any, Optional

from src.common.logger import get_logger
from src.llm_models.embedding import embedding_source_hash
from src.manager.async_task_manager import AsyncTask
from src.memory.atom import (
    MemoryAtom as MemoryAtomDC,
    AtomType,
    DecayType,
    apply_dream_consolidation,
    compute_weight,
)
from src.memory.schema import (
    ConflictObservation,
    InsightPool,
    DreamRun,
    GraphEdge,
    MemoryAtom as MemoryAtomModel,
    MemoryTraceChain,
    NoisePool,
    RawMessageArchive,
    SemanticDetail as SemanticDetailModel,
    memory_db,
)
from src.memory.forgetting import _safe_timestamp
from src.memory.embedding_utils import generate_embedding
from src.memory.store import MemoryStore

logger = get_logger("memory.dream")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

IDLE_THRESHOLD: int = 300
"""空闲判定阈值（秒），所有 ChatStream 的最后活动时间距今超过此值才算空闲"""

DREAM_TASK_FIRST_DELAY_SECONDS: int = 1800
"""程序启动后首次检查记忆维护任务前的延迟"""

DREAM_TASK_POLL_INTERVAL_SECONDS: int = 3600
"""记忆维护任务检查日、周、月周期是否到期的固定间隔"""

CONSOLIDATION_BOOST: float = 0.15
"""梦境巩固提升量，传给 apply_dream_consolidation() 的 boost 参数"""

SIMILARITY_THRESHOLD: float = 0.4
"""图谱边相似度阈值（暂未用于基于向量的相似度，保留供未来使用）"""

IMPORTANCE_MIN: float = 0.6
"""需要巩固的最小重要性——低于此值的记忆不参与巩固"""

WEIGHT_MAX: float = 0.4
"""需要巩固的最大权重——高于此值的记忆已足够强，无需巩固"""

CONSOLIDATION_BATCH_SIZE: int = 50
"""单次梦境巩固的最大原子数量"""

GRAPH_ATOMS_LIMIT: int = 20
"""参与图谱构建的 Top-N 原子数（按 weight 降序）"""

NOISE_CLEANUP_DAYS: int = 30
"""噪声池默认保留天数，超过此期限的噪声条目才会被清理"""

WEEKLY_NOISE_RECYCLE_DAYS: int = 14
"""每周恍然大悟机制扫描的噪声窗口"""

MONTHLY_NOISE_RECYCLE_DAYS: int = 30
"""月度恍然大悟机制扫描的噪声窗口"""

SCORE_REASSESS_BATCH_SIZE: int = 200
"""单次梦境评分重估的最大原子数量"""

EVIDENCE_CONFIDENCE_STEP: float = 0.05
"""每条额外语义证据带来的置信度提升"""

MAX_EVIDENCE_CONFIDENCE_BOOST: float = 0.2
"""语义证据对置信度的最大提升"""

EVIDENCE_IMPORTANCE_STEP: float = 0.03
"""每条额外语义证据带来的重要性提升"""

MAX_EVIDENCE_IMPORTANCE_BOOST: float = 0.15
"""语义证据对重要性的最大提升"""

PENDING_CONFLICT_CONFIDENCE_PENALTY: float = 0.15
"""每个未决冲突对置信度的折扣"""

MAX_PENDING_CONFLICT_CONFIDENCE_PENALTY: float = 0.45
"""未决冲突对置信度的最大折扣"""

SCORE_EPSILON: float = 1e-6
"""评分变更判定阈值"""

RAW_TRIAGE_BATCH_SIZE: int = 200
"""单次梦境原始消息分诊的最大数量"""

HIGH_SIGNIFICANCE_THRESHOLD: float = 0.75
"""高显著性阈值，进入完整梦境处理链"""

MEDIUM_SIGNIFICANCE_THRESHOLD: float = 0.35
"""中显著性阈值，直接提取为情景记忆"""

MEMORY_SIGNAL_KEYWORDS: tuple[str, ...] = (
    "喜欢",
    "讨厌",
    "开始",
    "正在",
    "总是",
    "以后",
    "计划",
    "决定",
    "记得",
    "忘了",
    "想要",
)
"""用于识别可长期记忆信息的轻量关键词"""

SELF_REPORT_KEYWORDS: tuple[str, ...] = ("说", "表示", "告诉", "承认", "提到")
"""用于识别用户自述或转述的轻量关键词"""

LOW_VALUE_CHARS: set[str] = set("哈啊嗯哦呃诶嘿哇唉。.!！?？~～…")
"""常见低信息量短消息字符集合"""

RAW_REPLY_PREFIX_RE = re.compile(r"^\[回复.*?\]\s*，说：\s*", re.DOTALL)
"""消息归档中的引用回复前缀；评分时不能把被引用内容算作新信号。"""

RAW_MENTION_RE = re.compile(r"@<[^>]+>")
"""消息归档中的用户提及标记。"""

RAW_MEDIA_ONLY_RE = re.compile(r"^(?:\[(?:表情|表情包|图片|语音|视频)[^\]]*\]\s*)+$")
"""只有表情或媒体占位符的即时反应。"""

SUMMARY_CHAT_TYPES: set[str] = {"summary", "topic_summary", "group_summary", "private_summary"}
"""归档表中代表对话摘要/话题摘要的 chat_type 值"""

PRIVACY_REASSESS_BATCH_SIZE: int = 200
"""单次梦境隐私重评的最大原子数量"""

USER_MEMORY_SOFT_CAP: int = 120
"""单个主实体/用户默认活跃记忆软上限"""

SOFT_CAP_BATCH_SIZE: int = 500
"""单次软上限合并最多归档的原子数量"""

SOFT_CAP_SOURCE_PREFIX: str = "dream_soft_cap:"
"""软上限泛化摘要的 source_id 前缀"""

SOFT_CAP_MERGEABLE_TYPES: set[str] = {"episodic", "factual", "relational", "preference"}
"""可被软上限合并归档的原子类型，保留 planned 避免误删未来计划"""

PUBLIC_UNLOCK_ATOM_TYPES: set[str] = {"factual", "preference", "relational"}
"""允许在跨场景证据充分时解锁为 public 的原子类型"""

PUBLIC_UNLOCK_MIN_CONFIDENCE: float = 0.75
"""解锁跨场景公开记忆所需的最低置信度"""

PRIVATE_LOCK_KEYWORDS: tuple[str, ...] = (
    "身份证",
    "手机号",
    "手机号码",
    "电话号码",
    "电话",
    "住址",
    "地址",
    "密码",
    "验证码",
    "银行卡",
    "账号",
    "病历",
    "诊断",
    "药物",
    "收入",
    "欠款",
)
"""私聊敏感信息关键词，命中时梦境隐私重评会收紧为 private"""

RAW_ARCHIVE_SOURCE_RE = re.compile(r"raw_message_archive:(\d+)")
"""从证据文本中提取 raw_message_archive 来源 ID"""


def _clamp01(value: float) -> float:
    """将评分限制在 0.0-1.0。"""
    return max(0.0, min(1.0, value))


# ---------------------------------------------------------------------------
# 周期枚举
# ---------------------------------------------------------------------------


class DreamCycleType(Enum):
    """梦境周期类型 — 区分日常/每周/月度三种粒度的维护周期"""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


# 周期间隔（超过此时间未运行对应周期则触发，优先级: daily > weekly > monthly）
DAILY_CYCLE_HOURS: int = 24
WEEKLY_CYCLE_DAYS: int = 7
MONTHLY_CYCLE_DAYS: int = 30

DREAM_SLEEP_PHASE_ORDER: tuple[str, ...] = ("N2", "N3", "REM")
"""梦境摘要中的睡眠阶段顺序"""

DAILY_DREAM_PHASES: dict[str, tuple[str, ...]] = {
    "N2": ("原始分诊", "评分重估"),
    "N3": ("冲突扫描", "模式提炼", "隐私重评", "记忆巩固", "遗忘维护"),
    "REM": ("情绪重演", "噪声沉淀"),
}
"""日常梦境阶段映射"""

WEEKLY_DREAM_PHASES: dict[str, tuple[str, ...]] = {
    "N2": ("冲突仲裁", "评分重估"),
    "N3": ("隐私重评", "全量巩固", "软上限合并", "图谱构建"),
    "REM": ("跨日模式", "洞见编织", "噪声回收"),
}
"""每周梦境阶段映射"""

MONTHLY_DREAM_PHASES: dict[str, tuple[str, ...]] = {
    "N2": ("全量审计", "健康诊断"),
    "N3": ("画像审计", "隐私重评", "软上限合并", "关系重建", "遗忘维护"),
    "REM": ("跨域洞察", "噪声回收", "月度报告"),
}
"""月度梦境阶段映射"""


# ---------------------------------------------------------------------------
# DreamTask
# ---------------------------------------------------------------------------


class DreamTask(AsyncTask):
    """梦境维护后台任务 — 闲时运行记忆巩固、仲裁、遗忘维护、洞见与噪声回收

    工作在系统空闲时段（所有 ChatStream 静默超过 IDLE_THRESHOLD 秒），
    由自身的日、周、月周期记录决定本轮是否需要执行：

    Phase 1 — Consolidation: 提升重要但正在衰减的记忆原子的权重
    Phase 2 — Conflict arbitration: 集中处理待观察冲突
    Phase 3 — Forgetting sweep: 归档/删除低权重记忆
    Phase 4 — Graph/insight/recycle: 图谱构建、洞见生成、噪声回收与清理

    Attributes:
        _store: MemoryStore 实例
        _forgetting_manager: 可选的 ForgettingManager
        _graph_store: 可选的 GraphStore（未实现时通过 Peewee 直接操作表）
    """

    def __init__(
        self,
        store: MemoryStore,
        forgetting_manager: Optional[Any] = None,
        graph_store: Optional[Any] = None,
        dream_weaver: Optional[Any] = None,
    ):
        """初始化 DreamTask

        Args:
            store: MemoryStore 实例（承载 SQLite + Qdrant）
            forgetting_manager: ForgettingManager 实例（可选，用于获取衰减统计）
            graph_store: GraphStore 实例（可选，暂未实现时可为 None）
            dream_weaver: DreamWeaver 实例（可选，用于梦呓编织洞见生成）
        """
        super().__init__(
            task_name="dream_task",
            wait_before_start=DREAM_TASK_FIRST_DELAY_SECONDS,
            run_interval=DREAM_TASK_POLL_INTERVAL_SECONDS,
        )
        self._store = store
        self._forgetting_manager = forgetting_manager
        self._graph_store = graph_store
        self._dream_weaver = dream_weaver

    # ── 主循环 ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        """执行一次梦境维护周期 — 自动判定周期类型并分发

        流程:
        1. 检查系统是否空闲
        2. 判定当前应运行的周期类型（daily > weekly > monthly）
        3. 分发到对应的周期处理器
        """
        # 1. 检查空闲状态
        if not await self._check_idle():
            logger.debug("聊天流活跃中，跳过本轮记忆维护")
            return

        # 2. 判定周期类型
        cycle_type = self._determine_cycle_type()
        if cycle_type is None:
            logger.debug("所有周期均未到期，跳过本轮记忆维护")
            return

        # 3. 分发到对应的周期处理器
        logger.info(f"开始记忆维护周期: type={cycle_type.value}")
        try:
            if cycle_type == DreamCycleType.DAILY:
                await self._run_daily_cycle()
            elif cycle_type == DreamCycleType.WEEKLY:
                await self._run_weekly_cycle()
            elif cycle_type == DreamCycleType.MONTHLY:
                await self._run_monthly_cycle()
        except Exception as e:
            logger.exception(f"记忆维护周期执行异常: type={cycle_type.value}, error={e}")

    # ── 周期判定 ────────────────────────────────────────────────────────

    def _determine_cycle_type(self) -> DreamCycleType | None:
        """判定当前应运行的梦境周期

        按优先级 daily > weekly > monthly 检查上次运行时间。
        若某种周期已超过其间隔阈值未运行，则返回该周期类型。

        Returns:
            DreamCycleType 或 None（所有周期均未到期时跳过）
        """
        try:
            with memory_db:
                last_daily: DreamRun | None = (
                    DreamRun.select()
                    .where(DreamRun.run_type == "daily", DreamRun.status == "completed")
                    .order_by(DreamRun.start_time.desc())
                    .first()
                )
                last_weekly: DreamRun | None = (
                    DreamRun.select()
                    .where(DreamRun.run_type == "weekly", DreamRun.status == "completed")
                    .order_by(DreamRun.start_time.desc())
                    .first()
                )
                last_monthly: DreamRun | None = (
                    DreamRun.select()
                    .where(DreamRun.run_type == "monthly", DreamRun.status == "completed")
                    .order_by(DreamRun.start_time.desc())
                    .first()
                )
        except Exception as e:
            logger.error(f"查询上次梦境运行记录失败: {e}")
            return DreamCycleType.DAILY

        now = datetime.datetime.now()

        if last_daily is None or (now - last_daily.start_time).total_seconds() > DAILY_CYCLE_HOURS * 3600:
            return DreamCycleType.DAILY

        if last_weekly is None or (now - last_weekly.start_time).days >= WEEKLY_CYCLE_DAYS:
            return DreamCycleType.WEEKLY

        if last_monthly is None or (now - last_monthly.start_time).days >= MONTHLY_CYCLE_DAYS:
            return DreamCycleType.MONTHLY

        return None

    def _create_dream_run(self, run_type: str) -> int | None:
        """创建 DreamRun 记录并返回 ID

        Args:
            run_type: 运行类型（daily/weekly/monthly）

        Returns:
            DreamRun 记录 ID，失败返回 None
        """
        try:
            with memory_db:
                record = DreamRun.create(
                    run_type=run_type,
                    start_time=datetime.datetime.now(),
                    status="running",
                )
                return record.id
        except Exception as e:
            logger.error(f"创建 DreamRun 记录失败: {e}")
            return None

    @staticmethod
    def _get_atoms_modified_since(days: int) -> list:
        """查询最近 N 天内修改过的活跃原子

        Args:
            days: 天数范围

        Returns:
            MemoryAtomModel 实例列表（按 weight 降序）
        """
        cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
        try:
            with memory_db:
                return list(
                    MemoryAtomModel.select()
                    .where(
                        MemoryAtomModel.last_accessed_at >= cutoff,
                        MemoryAtomModel.status == "active",
                    )
                    .order_by(MemoryAtomModel.weight.desc())
                )
        except Exception as e:
            logger.error(f"查询近期原子失败: {e}")
            return []

    # ── 空闲检测 ────────────────────────────────────────────────────────

    async def _check_idle(self) -> bool:
        """检查系统是否空闲

        查询 ChatStreams 表，若所有 stream 的 last_active_time 距今均超过
        IDLE_THRESHOLD 秒则视为空闲。

        Returns:
            True 表示系统空闲（可以进行梦境），False 表示仍有活跃 ChatStream
        """
        try:
            from src.common.database.database_model import ChatStreams

            now = time.time()
            threshold = now - IDLE_THRESHOLD

            active_count = ChatStreams.select().where(ChatStreams.last_active_time > threshold).count()

            return active_count == 0

        except Exception as e:
            logger.warning(f"空闲检测失败（跳过本轮梦境维护）: {e}")
            return False

    # ── Phase 1: 记忆巩固 ─────────────────────────────────────────────

    async def _consolidate(self, max_age_days: int | None = None, batch_size: int | None = None) -> int:
        """Phase 1 — 提升重要但正在衰减的记忆

        选取 status='active'、importance >= IMPORTANCE_MIN、weight <= WEIGHT_MAX
        的原子，调用 apply_dream_consolidation() 为其权重应用 consolidation boost。

        Args:
            max_age_days: 若指定，仅处理最近 N 天内修改过的原子
            batch_size: 处理原子数量上限，默认使用 CONSOLIDATION_BATCH_SIZE

        Returns:
            成功巩固的原子数量
        """
        limit = batch_size or CONSOLIDATION_BATCH_SIZE
        count = 0
        now_ts = time.time()
        qdrant_updates: list[tuple[str, dict[str, Any]]] = []

        try:
            with memory_db:
                conditions: list[Any] = [
                    MemoryAtomModel.status == "active",
                    MemoryAtomModel.importance >= IMPORTANCE_MIN,
                    MemoryAtomModel.weight <= WEIGHT_MAX,
                ]
                if max_age_days is not None:
                    cutoff = datetime.datetime.now() - datetime.timedelta(days=max_age_days)
                    conditions.append(MemoryAtomModel.last_accessed_at >= cutoff)

                query = MemoryAtomModel.select().where(*conditions).order_by(MemoryAtomModel.weight.asc()).limit(limit)

                for atom_model in query:
                    try:
                        atom_dc = MemoryAtomDC(
                            atom_id=atom_model.atom_id,
                            atom_type=AtomType(atom_model.atom_type),
                            content=atom_model.content or "",
                            importance=atom_model.importance,
                            confidence=atom_model.confidence,
                            weight=atom_model.weight,
                            created_at=_safe_timestamp(atom_model.created_at, now_ts),
                            last_accessed_at=_safe_timestamp(atom_model.last_accessed_at, now_ts),
                            ttl_days=float(atom_model.ttl_days or 7),
                            decay_type=DecayType(atom_model.decay_type),
                            reinforcement_count=atom_model.reinforcement_count or 0,
                            source_scene=atom_model.source_scene or "unknown",
                            source_id=atom_model.source_id,
                            privacy_level=atom_model.privacy_level or "context_sensitive",
                            status=atom_model.status,
                        )

                        updated = apply_dream_consolidation(atom_dc, boost=CONSOLIDATION_BOOST)

                        last_accessed_at = datetime.datetime.fromtimestamp(updated.last_accessed_at)
                        MemoryAtomModel.update(
                            weight=updated.weight,
                            last_accessed_at=last_accessed_at,
                        ).where(MemoryAtomModel.atom_id == atom_model.atom_id).execute()
                        qdrant_updates.append(
                            (
                                atom_model.atom_id,
                                {
                                    "weight": updated.weight,
                                    "last_accessed_at": last_accessed_at.isoformat(),
                                },
                            )
                        )
                        count += 1

                    except Exception as e:
                        logger.error(f"巩固原子失败 ({atom_model.atom_id}): {e}")

        except Exception as e:
            logger.error(f"记忆巩固阶段异常: {e}")

        notify_bm25 = getattr(self._store, "_notify_bm25_update", None)
        if callable(notify_bm25):
            for atom_id, updates in qdrant_updates:
                notify_bm25(atom_id, updates)

        for atom_id, payload in qdrant_updates:
            try:
                await self._store.qdrant.set_atom_payload(atom_id, payload)
            except Exception:
                pass  # Qdrant 同步是尽力而为的最佳操作

        if count > 0:
            logger.info(f"梦境巩固: {count} 个原子权重已提升 (boost={CONSOLIDATION_BOOST})")
        return count

    # ── Phase 1: 原始增量分诊 ─────────────────────────────────────────

    def _ingest_topic_bridge_summaries(
        self,
        max_age_days: int | None = 1,
        batch_size: int | None = None,
    ) -> int:
        """把未闭合话题摘要只读导入 raw archive，作为梦境分诊材料。"""
        if not self._raw_archive_supports_dream_fields():
            return 0

        try:
            from src.memory.layer1_summarizer import UnclosedTopicBridge
        except Exception as e:
            logger.debug(f"话题摘要桥不可用，跳过摘要导入: {e}")
            return 0

        try:
            bridge = UnclosedTopicBridge()
            bridge_data = getattr(bridge, "_data", {}) or {}
        except Exception as e:
            logger.warning(f"话题摘要桥读取失败: {e}")
            return 0

        if not isinstance(bridge_data, dict) or not bridge_data:
            return 0

        now_ts = time.time()
        cutoff = now_ts - max_age_days * 86400 if max_age_days is not None else None
        remaining = batch_size or RAW_TRIAGE_BATCH_SIZE
        ingested = 0

        for stream_id, topics in bridge_data.items():
            if remaining <= 0:
                break
            if not isinstance(topics, list):
                continue
            for topic in topics:
                if remaining <= 0:
                    break
                if not isinstance(topic, dict):
                    continue
                summary = str(topic.get("summary") or "").strip()
                if not summary:
                    continue
                last_active = self._safe_topic_timestamp(topic.get("last_active"), now_ts)
                if cutoff is not None and last_active < cutoff:
                    continue

                topic_id = str(topic.get("topic_id") or topic.get("topic_name") or "summary")
                digest = hashlib.sha1(
                    f"{stream_id}|{topic_id}|{last_active}|{summary}".encode("utf-8"), usedforsecurity=False
                ).hexdigest()
                message_id = f"topic_summary:{digest[:16]}"

                try:
                    with memory_db.atomic():
                        if RawMessageArchive.select().where(RawMessageArchive.message_id == message_id).exists():
                            continue
                        RawMessageArchive.create(
                            stream_id=str(stream_id),
                            message_id=message_id,
                            user_id="system",
                            content=f"话题摘要：{summary}",
                            timestamp=last_active,
                            chat_type="topic_summary",
                        )
                    ingested += 1
                    remaining -= 1
                except Exception as e:
                    logger.warning(f"话题摘要导入 raw archive 失败 stream={stream_id} topic={topic_id}: {e}")

        if ingested:
            logger.info(f"话题摘要梦境导入: {ingested} 条")
        return ingested

    @staticmethod
    def _raw_archive_supports_dream_fields() -> bool:
        """确认 raw archive 表结构支持梦境分诊字段。"""
        required_columns = {
            "dream_status",
            "dream_route",
            "dream_significance",
            "dream_processed_at",
            "dream_run_id",
        }
        try:
            if not memory_db.table_exists(RawMessageArchive):
                return False
            rows = memory_db.execute_sql("PRAGMA table_info(raw_message_archive)").fetchall()
        except Exception as e:
            logger.debug(f"原始消息归档表结构检查失败，跳过摘要导入: {e}")
            return False
        return required_columns.issubset({row[1] for row in rows})

    @staticmethod
    def _safe_topic_timestamp(value: Any, fallback: float) -> float:
        """将话题桥中的时间字段转换为秒级时间戳。"""
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    async def _archive_legacy_direct_raw_atoms(self, batch_size: int | None = None) -> int:
        """归档旧版本直接从原始消息生成的原子，并移除对应向量。"""
        limit = batch_size or RAW_TRIAGE_BATCH_SIZE
        try:
            with memory_db:
                atom_ids = [
                    atom.atom_id
                    for atom in (
                        MemoryAtomModel.select(MemoryAtomModel.atom_id)
                        .where(
                            MemoryAtomModel.status == "active",
                            MemoryAtomModel.atom_id.startswith("dream-raw-"),
                            MemoryAtomModel.source_id.startswith("raw_message_archive:"),
                        )
                        .limit(limit)
                    )
                ]
        except Exception as e:
            logger.warning("旧版原始消息原子读取失败: %s", e)
            return 0

        archived = 0
        for atom_id in atom_ids:
            try:
                if await self._store.archive_atom(atom_id):
                    archived += 1
            except Exception as e:
                logger.warning("旧版原始消息原子归档失败 atom_id=%s: %s", atom_id, e)

        if archived:
            logger.info("已归档 %d 条旧版原始消息直写原子", archived)
        return archived

    async def _triage_raw_archive(
        self,
        max_age_days: int | None = 1,
        batch_size: int | None = None,
        dream_run_id: int | None = None,
    ) -> dict[str, int]:
        """扫描原始消息归档并按显著性分流。

        所有原始消息只进入 NoisePool 候选层，不能绕过语义提取器直接成为记忆原子。
        """
        stats = {"high": 0, "medium": 0, "low": 0, "skipped": 0}
        limit = batch_size or RAW_TRIAGE_BATCH_SIZE

        try:
            conditions: list[Any] = [
                (RawMessageArchive.dream_status == "pending") | RawMessageArchive.dream_status.is_null(True)
            ]
            if max_age_days is not None:
                cutoff = time.time() - max_age_days * 86400
                conditions.append(RawMessageArchive.timestamp >= cutoff)

            with memory_db:
                records = list(
                    RawMessageArchive.select()
                    .where(*conditions)
                    .order_by(RawMessageArchive.timestamp.asc())
                    .limit(limit)
                )
        except Exception as e:
            logger.warning("原始消息分诊读取失败: %s", e)
            return stats

        for record in records:
            try:
                route, significance, emotion_tags = self._route_raw_message(record)
                if route == "skipped":
                    self._mark_raw_triaged(record, route, significance, dream_run_id)
                    stats["skipped"] += 1
                    continue

                created = self._record_raw_candidate_and_mark_triaged(
                    record,
                    route,
                    significance,
                    dream_run_id,
                )
                if created:
                    stats[route] += 1
                else:
                    stats["skipped"] += 1
            except Exception as e:
                logger.warning("原始消息分诊失败 id=%s: %s", getattr(record, "id", "?"), e)
                stats["skipped"] += 1

        if any(stats.values()):
            logger.info(
                f"原始消息梦境分诊: 高{stats['high']}条/中{stats['medium']}条/"
                f"低{stats['low']}条/跳过{stats['skipped']}条"
            )
        return stats

    @classmethod
    def _route_raw_message(cls, record: RawMessageArchive) -> tuple[str, float, list[str]]:
        """返回 raw message 的分诊路由、显著性和情绪标签。"""
        content = (record.content or "").strip()
        if not content or str(record.chat_type or "").startswith("memory_archive"):
            return "skipped", 0.0, []

        significance, emotion_tags = cls._score_raw_message(content, record.chat_type)
        if significance >= HIGH_SIGNIFICANCE_THRESHOLD:
            return "high", significance, emotion_tags
        if significance >= MEDIUM_SIGNIFICANCE_THRESHOLD:
            return "medium", significance, emotion_tags
        return "low", significance, emotion_tags

    @staticmethod
    def _score_raw_message(content: str, chat_type: str | None = None) -> tuple[float, list[str]]:
        """轻量启发式显著性评分。

        这里先提供可解释、可测试的 baseline：情绪词、自述/转述、偏好/计划等信号
        会提升显著性；纯短应答会被压低。
        """
        text = DreamTask._raw_signal_text(content)
        if not text:
            return 0.0, []

        if RAW_MEDIA_ONLY_RE.fullmatch(text):
            return 0.05, []

        compact = "".join(text.split())
        if len(compact) <= 6 and set(compact) <= LOW_VALUE_CHARS:
            return 0.05, []

        emotion_tags = DreamTask._extract_emotion_tags(text)
        score = 0.15
        if len(compact) >= 10:
            score += 0.1
        if any(keyword in text for keyword in SELF_REPORT_KEYWORDS):
            score += 0.15
        if any(keyword in text for keyword in MEMORY_SIGNAL_KEYWORDS):
            score += 0.2
        if emotion_tags:
            score += 0.35
        if any(keyword in text for keyword in ("再也不", "不想", "受不了", "出事", "生病", "危险")):
            score += 0.1
        if chat_type == "private":
            score += 0.05
        if DreamTask._is_summary_chat_type(chat_type):
            score += 0.1

        return _clamp01(score), emotion_tags

    @staticmethod
    def _raw_signal_text(content: str) -> str:
        """移除引用和提及包装，仅对当前消息正文做显著性评分。"""
        text = RAW_REPLY_PREFIX_RE.sub("", content.strip(), count=1)
        return RAW_MENTION_RE.sub("", text).strip()

    @staticmethod
    def _is_summary_chat_type(chat_type: str | None) -> bool:
        """判断归档记录是否为对话摘要材料。"""
        return str(chat_type or "").lower() in SUMMARY_CHAT_TYPES

    @staticmethod
    def _extract_emotion_tags(content: str) -> list[str]:
        """从文本中提取粗粒度情绪标签。"""
        tags: list[str] = []
        emotion_map = {
            "distress": ("崩溃", "压力", "绝望", "焦虑", "受不了"),
            "sadness": ("大哭", "哭", "难过"),
            "anger": ("生气", "愤怒"),
            "fear": ("害怕", "危险"),
            "joy": ("开心", "激动"),
        }
        for tag, keywords in emotion_map.items():
            if any(keyword in content for keyword in keywords):
                tags.append(tag)
        return tags

    @staticmethod
    def _raw_source_scene(record: RawMessageArchive) -> str:
        """把原始归档 chat_type 映射为记忆分区。"""
        if record.chat_type == "group":
            return "group_chat"
        if record.chat_type == "private":
            return "private_chat"
        if DreamTask._is_summary_chat_type(record.chat_type):
            return "summary"
        return "unknown"

    @staticmethod
    def _raw_source_id(record: RawMessageArchive) -> str:
        """生成可追溯的原始消息来源 ID。"""
        return f"raw_message_archive:{record.id}"

    def _record_raw_candidate_and_mark_triaged(
        self,
        record: RawMessageArchive,
        route: str,
        significance: float,
        dream_run_id: int | None = None,
    ) -> bool:
        """原子地写入候选并确认分诊状态，失败时保留 pending 供重试。"""
        source_id = self._raw_source_id(record)
        created = False
        with memory_db.atomic():
            if not NoisePool.select().where(NoisePool.source_id == source_id).exists():
                NoisePool.create(
                    content=(record.content or "")[:200],
                    source_scene=self._raw_source_scene(record),
                    source_id=source_id,
                    significance=significance,
                    ttl_days=NOISE_CLEANUP_DAYS,
                )
                created = True
            self._mark_raw_triaged(record, route, significance, dream_run_id)
        return created

    @staticmethod
    def _mark_raw_triaged(
        record: RawMessageArchive,
        route: str,
        significance: float,
        dream_run_id: int | None = None,
    ) -> None:
        """标记 raw message 已被梦境分诊，防止重复处理。"""
        status = "skipped" if route == "skipped" else "triaged"
        RawMessageArchive.update(
            dream_status=status,
            dream_route=route,
            dream_significance=significance,
            dream_processed_at=datetime.datetime.now(),
            dream_run_id=dream_run_id,
        ).where(RawMessageArchive.id == record.id).execute()

    # ── 核心维护阶段 ─────────────────────────────────────────────────

    async def _resolve_conflicts(self) -> int:
        """Phase 2 — 梦境期集中处理待观察冲突。"""
        try:
            from src.memory.conflict_arbitration import ConflictArbiter

            arbiter = ConflictArbiter(self._store)
            resolved = await arbiter.check_and_resolve()
            if resolved > 0:
                logger.info(f"梦境冲突仲裁: 解决 {resolved} 个冲突")
            return resolved
        except ImportError:
            logger.debug("ConflictArbiter 不可用，跳过冲突仲裁")
            return 0
        except Exception as e:
            logger.warning("梦境冲突仲裁阶段异常: %s", e)
            return 0

    async def _reassess_memory_scores(
        self,
        max_age_days: int | None = None,
        batch_size: int | None = None,
    ) -> int:
        """Phase 3 — 根据证据和未决冲突重估 importance/confidence。

        梦境期不直接相信单次编码结果。多条语义证据会小幅增强记忆，
        未决冲突会降低置信度；随后按现有权重公式重新计算 weight。
        """
        limit = batch_size or SCORE_REASSESS_BATCH_SIZE
        now_ts = time.time()
        updated_count = 0
        qdrant_updates: list[tuple[str, dict[str, Any]]] = []

        try:
            with memory_db:
                conditions: list[Any] = [MemoryAtomModel.status == "active"]
                if max_age_days is not None:
                    cutoff = datetime.datetime.now() - datetime.timedelta(days=max_age_days)
                    conditions.append(MemoryAtomModel.last_accessed_at >= cutoff)

                query = (
                    MemoryAtomModel.select()
                    .where(*conditions)
                    .order_by(MemoryAtomModel.last_accessed_at.desc())
                    .limit(limit)
                )

                for atom_model in query:
                    try:
                        evidence_count = self._semantic_evidence_count(atom_model.atom_id)
                        pending_conflicts = self._pending_conflict_count(atom_model.atom_id)
                        new_importance, new_confidence = self._reassessed_scores(
                            atom_model.importance,
                            atom_model.confidence,
                            evidence_count,
                            pending_conflicts,
                        )

                        if (
                            abs(new_importance - atom_model.importance) <= SCORE_EPSILON
                            and abs(new_confidence - atom_model.confidence) <= SCORE_EPSILON
                        ):
                            continue

                        atom_dc = MemoryAtomDC(
                            atom_id=atom_model.atom_id,
                            atom_type=AtomType(atom_model.atom_type),
                            content=atom_model.content or "",
                            importance=new_importance,
                            confidence=new_confidence,
                            weight=atom_model.weight,
                            created_at=_safe_timestamp(atom_model.created_at, now_ts),
                            last_accessed_at=_safe_timestamp(atom_model.last_accessed_at, now_ts),
                            ttl_days=float(atom_model.ttl_days or 7),
                            decay_type=DecayType(atom_model.decay_type),
                            reinforcement_count=atom_model.reinforcement_count or 0,
                            source_scene=atom_model.source_scene or "unknown",
                            source_id=atom_model.source_id,
                            privacy_level=atom_model.privacy_level or "context_sensitive",
                            status=atom_model.status,
                        )
                        new_weight = compute_weight(atom_dc, current_time=now_ts)

                        MemoryAtomModel.update(
                            importance=new_importance,
                            confidence=new_confidence,
                            weight=new_weight,
                        ).where(MemoryAtomModel.atom_id == atom_model.atom_id).execute()
                        qdrant_updates.append(
                            (
                                atom_model.atom_id,
                                {
                                    "importance": new_importance,
                                    "confidence": new_confidence,
                                    "weight": new_weight,
                                },
                            )
                        )
                        updated_count += 1
                    except Exception as e:
                        logger.error(f"重估原子评分失败 ({atom_model.atom_id}): {e}")
        except Exception as e:
            logger.error(f"梦境评分重估阶段异常: {e}")

        notify_bm25 = getattr(self._store, "_notify_bm25_update", None)
        if callable(notify_bm25):
            for atom_id, updates in qdrant_updates:
                notify_bm25(atom_id, updates)

        for atom_id, payload in qdrant_updates:
            try:
                await self._store.qdrant.set_atom_payload(atom_id, payload)
            except Exception:
                pass

        if updated_count > 0:
            logger.info(f"梦境评分重估: 更新 {updated_count} 个原子")
        return updated_count

    @staticmethod
    def _semantic_evidence_count(atom_id: str) -> int:
        """读取语义证据计数，兼容 evidence_list 比 counter 更新更完整的情况。"""
        detail = SemanticDetailModel.get_or_none(SemanticDetailModel.atom == atom_id)
        if detail is None:
            return 0

        count = int(detail.evidence_counter or 0)
        if detail.evidence_list:
            try:
                parsed = json.loads(detail.evidence_list)
                if isinstance(parsed, list):
                    count = max(count, len(parsed))
            except (json.JSONDecodeError, TypeError):
                pass
        return count

    @staticmethod
    def _pending_conflict_count(atom_id: str) -> int:
        """统计该原子仍处于 pending 的冲突观察数量。"""
        return (
            ConflictObservation.select()
            .where(
                ConflictObservation.status == "pending",
                (ConflictObservation.atom_a_id == atom_id) | (ConflictObservation.atom_b_id == atom_id),
            )
            .count()
        )

    @staticmethod
    def _reassessed_scores(
        importance: float,
        confidence: float,
        evidence_count: int,
        pending_conflicts: int,
    ) -> tuple[float, float]:
        """根据语义证据和未决冲突计算新的 importance/confidence。"""
        new_importance = float(importance)
        new_confidence = float(confidence)

        extra_evidence = max(0, evidence_count - 1)
        if extra_evidence:
            new_confidence += min(MAX_EVIDENCE_CONFIDENCE_BOOST, extra_evidence * EVIDENCE_CONFIDENCE_STEP)
            new_importance += min(MAX_EVIDENCE_IMPORTANCE_BOOST, extra_evidence * EVIDENCE_IMPORTANCE_STEP)

        if pending_conflicts:
            penalty = min(
                MAX_PENDING_CONFLICT_CONFIDENCE_PENALTY,
                pending_conflicts * PENDING_CONFLICT_CONFIDENCE_PENALTY,
            )
            new_confidence *= 1.0 - penalty

        return _clamp01(new_importance), _clamp01(new_confidence)

    async def _adjust_privacy_levels(
        self,
        max_age_days: int | None = None,
        batch_size: int | None = None,
    ) -> dict[str, int]:
        """Phase 4 — 按跨场景证据和敏感性动态调整隐私等级。

        规则保持保守：
        - 私聊来源且包含敏感关键词的记忆会收紧为 private；
        - 非敏感、置信度高、且有群聊/私聊跨场景原始证据的事实类记忆，
          可从 context_sensitive 解锁为 public；
        - 不会把已经标记为 private 的记忆自动解锁。
        """
        stats = {"locked_private": 0, "unlocked_public": 0}
        limit = batch_size or PRIVACY_REASSESS_BATCH_SIZE
        qdrant_updates: list[tuple[str, dict[str, Any]]] = []

        try:
            with memory_db:
                conditions: list[Any] = [MemoryAtomModel.status == "active"]
                if max_age_days is not None:
                    cutoff = datetime.datetime.now() - datetime.timedelta(days=max_age_days)
                    conditions.append(MemoryAtomModel.last_accessed_at >= cutoff)

                query = (
                    MemoryAtomModel.select()
                    .where(*conditions)
                    .order_by(MemoryAtomModel.last_accessed_at.desc())
                    .limit(limit)
                )

                for atom_model in query:
                    try:
                        target_level = self._target_privacy_level(atom_model)
                        if target_level is None or target_level == atom_model.privacy_level:
                            continue

                        MemoryAtomModel.update(privacy_level=target_level).where(
                            MemoryAtomModel.atom_id == atom_model.atom_id
                        ).execute()
                        qdrant_updates.append((atom_model.atom_id, {"privacy_level": target_level}))

                        if target_level == "private":
                            stats["locked_private"] += 1
                        elif target_level == "public":
                            stats["unlocked_public"] += 1
                    except Exception as e:
                        logger.error(f"隐私重评失败 ({atom_model.atom_id}): {e}")
        except Exception as e:
            logger.error(f"梦境隐私重评阶段异常: {e}")

        notify_bm25 = getattr(self._store, "_notify_bm25_update", None)
        if callable(notify_bm25):
            for atom_id, updates in qdrant_updates:
                notify_bm25(atom_id, updates)

        for atom_id, payload in qdrant_updates:
            try:
                await self._store.qdrant.set_atom_payload(atom_id, payload)
            except Exception:
                pass

        if stats["locked_private"] or stats["unlocked_public"]:
            logger.info(f"梦境隐私重评: 上锁{stats['locked_private']}条, 解锁{stats['unlocked_public']}条")
        return stats

    def _target_privacy_level(self, atom_model: MemoryAtomModel) -> str | None:
        """返回原子应调整到的隐私等级；无需调整时返回 None。"""
        current = atom_model.privacy_level or "context_sensitive"
        content = atom_model.content or ""
        source_scene = atom_model.source_scene or "unknown"

        if (
            source_scene == "private_chat"
            and current != "private"
            and self._contains_private_sensitive_content(content)
        ):
            return "private"

        if current != "context_sensitive":
            return None
        if atom_model.atom_type not in PUBLIC_UNLOCK_ATOM_TYPES:
            return None
        if float(atom_model.confidence or 0.0) < PUBLIC_UNLOCK_MIN_CONFIDENCE:
            return None
        if self._contains_private_sensitive_content(content):
            return None
        if self._pending_conflict_count(atom_model.atom_id) > 0:
            return None

        evidence_scenes = self._evidence_scene_types(atom_model)
        if {"group_chat", "private_chat"}.issubset(evidence_scenes):
            return "public"
        return None

    @staticmethod
    def _contains_private_sensitive_content(content: str) -> bool:
        """判断内容是否包含应保守上锁的私密信息。"""
        text = content or ""
        if any(keyword in text for keyword in PRIVATE_LOCK_KEYWORDS):
            return True
        digit_count = sum(ch.isdigit() for ch in text)
        return digit_count >= 11 and any(keyword in text for keyword in ("号", "码", "电话", "手机"))

    def _evidence_scene_types(self, atom_model: MemoryAtomModel) -> set[str]:
        """沿语义证据和追溯链回查原始归档，收集证据来自哪些场景。"""
        scenes: set[str] = set()
        source_scene = self._canonical_scene_type(atom_model.source_scene)
        if source_scene:
            scenes.add(source_scene)

        for raw_id in self._raw_archive_ids_for_atom(atom_model.atom_id):
            try:
                raw = RawMessageArchive.get_or_none(RawMessageArchive.id == raw_id)
            except Exception:
                raw = None
            raw_scene = self._raw_source_scene(raw) if raw is not None else None
            if raw_scene in ("group_chat", "private_chat"):
                scenes.add(raw_scene)
        return scenes

    def _raw_archive_ids_for_atom(self, atom_id: str) -> set[int]:
        """从 SemanticDetail.evidence_list 与 MemoryTraceChain.input_source 提取原始消息 ID。"""
        raw_ids: set[int] = set()

        detail = SemanticDetailModel.get_or_none(SemanticDetailModel.atom == atom_id)
        if detail is not None and detail.evidence_list:
            try:
                parsed = json.loads(detail.evidence_list)
            except (json.JSONDecodeError, TypeError):
                parsed = []
            if isinstance(parsed, list):
                for item in parsed:
                    raw_ids.update(self._raw_archive_ids_from_text(str(item)))

        traces = MemoryTraceChain.select().where(MemoryTraceChain.atom_id == atom_id)
        for trace in traces:
            if trace.input_source:
                raw_ids.update(self._raw_archive_ids_from_text(trace.input_source))
            if trace.output_summary:
                raw_ids.update(self._raw_archive_ids_from_text(trace.output_summary))

        return raw_ids

    @staticmethod
    def _raw_archive_ids_from_text(text: str) -> set[int]:
        """从任意证据文本中提取 raw_message_archive:<id>。"""
        ids: set[int] = set()
        for match in RAW_ARCHIVE_SOURCE_RE.finditer(text or ""):
            try:
                ids.add(int(match.group(1)))
            except (TypeError, ValueError):
                continue
        return ids

    @staticmethod
    def _canonical_scene_type(scene: str | None) -> str | None:
        """标准化来源场景名。"""
        if scene in ("group", "group_chat"):
            return "group_chat"
        if scene in ("private", "private_chat"):
            return "private_chat"
        return None

    async def _merge_overflowing_user_memories(
        self,
        soft_cap: int | None = None,
        batch_size: int | None = None,
    ) -> dict[str, int]:
        """Phase 5 — 单用户记忆超过软上限时合并泛化。

        对每个主实体/用户统计活跃记忆数量。若超过软上限，则选择低权重、
        较旧、可合并的碎片归档，并生成或更新一条 dream 来源的泛化摘要。
        """
        cap = soft_cap or USER_MEMORY_SOFT_CAP
        archive_limit = batch_size or SOFT_CAP_BATCH_SIZE
        stats = {"users_compacted": 0, "atoms_archived": 0, "summaries_created": 0, "summaries_updated": 0}
        if cap <= 0 or archive_limit <= 0:
            return stats

        try:
            counts, candidate_ids = await asyncio.to_thread(self._scan_soft_cap_candidates, archive_limit)
        except Exception as e:
            logger.error(f"梦境软上限扫描失败: {e}")
            return stats

        remaining_archive_budget = archive_limit
        for entity, active_count in counts.items():
            if remaining_archive_budget <= 0:
                break
            try:
                existing_summary = self._get_soft_cap_summary(entity)
                if active_count <= cap:
                    continue

                ids = candidate_ids.get(entity, [])
                with memory_db:
                    candidates = list(
                        MemoryAtomModel.select().where(
                            MemoryAtomModel.atom_id.in_(ids),
                            MemoryAtomModel.status == "active",
                        )
                    )
                # The threaded snapshot can be stale: an atom may have been
                # reassigned or promoted to a protected type while scanning.
                candidates = [
                    atom
                    for atom in candidates
                    if atom.atom_type in SOFT_CAP_MERGEABLE_TYPES
                    and not self._is_soft_cap_summary(atom)
                    and self._primary_memory_entity(atom) == entity
                ]
                if not candidates:
                    continue

                archive_target = active_count - cap
                if existing_summary is None:
                    archive_target += 1
                archive_count = min(len(candidates), archive_target, remaining_archive_budget)
                if archive_count <= 0:
                    continue

                to_archive = sorted(
                    candidates,
                    key=lambda atom: (
                        float(atom.weight or 0.0),
                        atom.last_accessed_at or atom.created_at or datetime.datetime.min,
                    ),
                )[:archive_count]

                summary, created = await self._write_soft_cap_summary(entity, to_archive, existing_summary)
                await self._archive_soft_cap_atoms(to_archive, summary.atom_id)

                stats["users_compacted"] += 1
                stats["atoms_archived"] += len(to_archive)
                remaining_archive_budget -= len(to_archive)
                if created:
                    stats["summaries_created"] += 1
                else:
                    stats["summaries_updated"] += 1
            except Exception as e:
                logger.error(f"软上限合并失败 entity={entity}: {e}")

        if stats["users_compacted"]:
            logger.info(
                f"梦境软上限合并: 压缩{stats['users_compacted']}个用户, "
                f"归档{stats['atoms_archived']}条, "
                f"新建{stats['summaries_created']}条, 更新{stats['summaries_updated']}条"
            )
        return stats

    def _scan_soft_cap_candidates(self, archive_limit: int) -> tuple[dict[str, int], dict[str, list[str]]]:
        """流式统计全部历史，只保留每个实体本轮可能归档的候选 ID。"""
        counts: dict[str, int] = {}
        candidates: dict[str, list[tuple]] = {}
        with memory_db:
            query = (
                MemoryAtomModel.select(
                    MemoryAtomModel.atom_id,
                    MemoryAtomModel.atom_type,
                    MemoryAtomModel.entities,
                    MemoryAtomModel.source_id,
                    MemoryAtomModel.source_scene,
                    MemoryAtomModel.weight,
                    MemoryAtomModel.last_accessed_at,
                    MemoryAtomModel.created_at,
                )
                .where(MemoryAtomModel.status == "active")
                .order_by(MemoryAtomModel.last_accessed_at.asc())
            )
            for index, atom in enumerate(query.iterator()):
                entity = self._primary_memory_entity(atom)
                if not entity:
                    continue
                counts[entity] = counts.get(entity, 0) + 1
                if atom.atom_type not in SOFT_CAP_MERGEABLE_TYPES or self._is_soft_cap_summary(atom):
                    continue
                selected = candidates.setdefault(entity, [])
                insort(
                    selected,
                    (
                        float(atom.weight or 0.0),
                        atom.last_accessed_at or atom.created_at or datetime.datetime.min,
                        index,
                        atom.atom_id,
                    ),
                )
                if len(selected) > archive_limit:
                    selected.pop()
        return counts, {entity: [item[-1] for item in items] for entity, items in candidates.items()}

    @staticmethod
    def _primary_memory_entity(atom_model: MemoryAtomModel) -> str | None:
        """取原子实体列表中的第一个实体，作为当前记忆归属的主用户/实体。"""
        if not atom_model.entities:
            return None
        try:
            parsed = json.loads(atom_model.entities)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(parsed, list) or not parsed:
            return None
        entity = str(parsed[0]).strip()
        return entity or None

    @staticmethod
    def _soft_cap_digest(entity: str) -> str:
        """生成软上限摘要稳定 ID 的短哈希。"""
        return hashlib.sha1(entity.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]

    @classmethod
    def _soft_cap_source_id(cls, entity: str) -> str:
        """生成软上限摘要 source_id。"""
        return f"{SOFT_CAP_SOURCE_PREFIX}{cls._soft_cap_digest(entity)}"

    @classmethod
    def _soft_cap_atom_id(cls, entity: str) -> str:
        """生成软上限摘要 atom_id。"""
        return f"dream-soft-cap-{cls._soft_cap_digest(entity)}"

    @staticmethod
    def _is_soft_cap_summary(atom_model: MemoryAtomModel) -> bool:
        """判断原子是否为软上限泛化摘要。"""
        return atom_model.source_scene == "dream" and str(atom_model.source_id or "").startswith(SOFT_CAP_SOURCE_PREFIX)

    def _get_soft_cap_summary(self, entity: str) -> MemoryAtomModel | None:
        """查找某实体已有的软上限泛化摘要。"""
        source_id = self._soft_cap_source_id(entity)
        return MemoryAtomModel.get_or_none(
            MemoryAtomModel.source_scene == "dream",
            MemoryAtomModel.source_id == source_id,
            MemoryAtomModel.status == "active",
        )

    async def _write_soft_cap_summary(
        self,
        entity: str,
        archived_atoms: list[MemoryAtomModel],
        existing_summary: MemoryAtomModel | None,
    ) -> tuple[MemoryAtomModel, bool]:
        """创建或更新软上限泛化摘要，并写入追溯链。"""
        now = datetime.datetime.now()
        now_ts = now.timestamp()
        source_id = self._soft_cap_source_id(entity)
        atom_id = existing_summary.atom_id if existing_summary is not None else self._soft_cap_atom_id(entity)
        content = self._build_soft_cap_summary_content(entity, archived_atoms)
        privacy_level = self._most_restrictive_privacy(atom.privacy_level for atom in archived_atoms)
        entities = self._merged_entities(entity, archived_atoms)
        importance = _clamp01(max([float(atom.importance or 0.5) for atom in archived_atoms], default=0.5))
        confidence = _clamp01(
            sum(float(atom.confidence or 0.5) for atom in archived_atoms) / max(1, len(archived_atoms))
        )

        atom_dc = MemoryAtomDC(
            atom_id=atom_id,
            atom_type=AtomType.FACTUAL,
            content=content,
            entities=entities,
            importance=importance,
            confidence=confidence,
            weight=0.5,
            created_at=now_ts,
            last_accessed_at=now_ts,
            last_reinforced_at=now_ts,
            ttl_days=180,
            decay_type=DecayType.EXPONENTIAL,
            reinforcement_count=0,
            source_scene="dream",
            source_id=source_id,
            privacy_level=privacy_level,
            status="active",
        )
        weight = compute_weight(atom_dc, current_time=now_ts)

        with memory_db.atomic():
            if existing_summary is None:
                summary = MemoryAtomModel.create(
                    atom_id=atom_id,
                    atom_type=AtomType.FACTUAL.value,
                    content=content,
                    entities=json.dumps(entities, ensure_ascii=False),
                    importance=importance,
                    confidence=confidence,
                    weight=weight,
                    created_at=now,
                    last_accessed_at=now,
                    last_reinforced_at=now,
                    ttl_days=180,
                    decay_type=DecayType.EXPONENTIAL.value,
                    reinforcement_count=0,
                    source_scene="dream",
                    source_id=source_id,
                    privacy_level=privacy_level,
                    status="active",
                    embedding_id=atom_id,
                )
                created = True
            else:
                MemoryAtomModel.update(
                    content=content,
                    entities=json.dumps(entities, ensure_ascii=False),
                    importance=importance,
                    confidence=confidence,
                    weight=weight,
                    last_accessed_at=now,
                    last_reinforced_at=now,
                    privacy_level=privacy_level,
                ).where(MemoryAtomModel.atom_id == atom_id).execute()
                summary = MemoryAtomModel.get(MemoryAtomModel.atom_id == atom_id)
                created = False

            MemoryTraceChain.create(
                atom_id=atom_id,
                step_number=self._next_trace_step(atom_id),
                agent_name="DreamSoftCapAgent",
                operation_type="merge",
                input_source=json.dumps([atom.atom_id for atom in archived_atoms], ensure_ascii=False),
                output_summary=json.dumps(
                    {
                        "entity": entity,
                        "archived_count": len(archived_atoms),
                        "summary_atom": atom_id,
                    },
                    ensure_ascii=False,
                ),
                confidence_decay=0.9,
            )

        invalidate_bm25 = getattr(self._store, "_invalidate_bm25_index", None)
        if callable(invalidate_bm25):
            invalidate_bm25()

        qdrant_payload = {
            "atom_id": atom_id,
            "atom_type": AtomType.FACTUAL.value,
            "weight": weight,
            "importance": importance,
            "confidence": confidence,
            "status": "active",
            "source_scene": "dream",
            "source_id": source_id,
            "privacy_level": privacy_level,
            "embedding_source_hash": embedding_source_hash(content),
        }
        embedding = await generate_embedding(content)
        if not embedding:
            raise RuntimeError(f"软上限摘要 embedding 生成失败: {atom_id}")
        if not await self._store.qdrant.upsert_atom_vector(
            point_id=atom_id,
            vector=embedding,
            payload=qdrant_payload,
        ):
            raise RuntimeError(f"软上限摘要 Qdrant 写入失败: {atom_id}")

        return summary, created

    async def _archive_soft_cap_atoms(self, atoms: list[MemoryAtomModel], summary_atom_id: str) -> None:
        """归档已被软上限摘要吸收的原子。"""
        now = datetime.datetime.now()
        for atom in atoms:
            MemoryAtomModel.update(status="archived", last_accessed_at=now).where(
                MemoryAtomModel.atom_id == atom.atom_id
            ).execute()
            notify_bm25 = getattr(self._store, "_notify_bm25_update", None)
            if callable(notify_bm25):
                notify_bm25(atom.atom_id, {"status": "archived", "last_accessed_at": now})
            MemoryTraceChain.create(
                atom_id=atom.atom_id,
                step_number=self._next_trace_step(atom.atom_id),
                agent_name="DreamSoftCapAgent",
                operation_type="archive",
                input_source=summary_atom_id,
                output_summary=f"已被软上限泛化摘要 {summary_atom_id} 吸收",
                confidence_decay=1.0,
            )
            if not await self._store.qdrant.delete_atom_vector(atom.atom_id):
                logger.warning("软上限归档原子向量删除失败 atom_id=%s", atom.atom_id)

    @staticmethod
    def _build_soft_cap_summary_content(entity: str, atoms: list[MemoryAtomModel]) -> str:
        """生成软上限泛化摘要内容。"""
        snippets: list[str] = []
        for atom in atoms[:5]:
            compact = " ".join(str(atom.content or "").split())
            if compact:
                snippets.append(compact[:80])
        suffix = "；".join(snippets) if snippets else "若干低权重记忆片段"
        return f"关于 {entity} 的泛化记忆摘要：这些低权重片段共同指向：{suffix}"

    @staticmethod
    def _merged_entities(entity: str, atoms: list[MemoryAtomModel]) -> list[str]:
        """合并摘要原子的实体列表，保持主实体在首位。"""
        merged: list[str] = [entity]
        for atom in atoms:
            if not atom.entities:
                continue
            try:
                parsed = json.loads(atom.entities)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(parsed, list):
                continue
            for item in parsed:
                text = str(item).strip()
                if text and text not in merged:
                    merged.append(text)
        return merged[:20]

    @staticmethod
    def _most_restrictive_privacy(levels: Any) -> str:
        """返回一组隐私等级中最保守的一档。"""
        rank = {"public": 0, "context_sensitive": 1, "private": 2}
        best = "public"
        for level in levels:
            text = str(level or "context_sensitive")
            if text not in rank:
                text = "context_sensitive"
            if rank.get(text, 1) > rank.get(best, 0):
                best = text
        return best

    @staticmethod
    def _next_trace_step(atom_id: str) -> int:
        """计算追溯链下一步序号。"""
        try:
            max_step = (
                MemoryTraceChain.select()
                .where(MemoryTraceChain.atom_id == atom_id)
                .order_by(MemoryTraceChain.step_number.desc())
                .first()
            )
            if max_step is None:
                return 1
            return int(max_step.step_number or 0) + 1
        except Exception:
            return 1

    async def _run_forgetting_sweep(self) -> dict[str, int]:
        """Phase 4 — 执行遗忘扫描，维护低权重记忆生态。"""
        try:
            if self._forgetting_manager is None:
                from src.memory.forgetting import ForgettingManager

                self._forgetting_manager = ForgettingManager(self._store)

            result = await self._forgetting_manager.run_sweep()
            if any(result.values()):
                logger.info(
                    f"梦境遗忘扫描: 衰减{result.get('decayed', 0)}条, "
                    f"归档{result.get('archived', 0)}条, 删除{result.get('deleted', 0)}条"
                )
            return result
        except Exception as e:
            logger.warning("梦境遗忘扫描阶段异常: %s", e)
            return {"decayed": 0, "archived": 0, "deleted": 0}

    async def _recycle_noise(self, retention_days: int) -> tuple[int, int, int]:
        """Phase 5 — 从噪声池回收可能被误判的伏笔。"""
        try:
            from src.memory.inspiration_engine import InspirationEngine
            from src.memory.layer3_retrieval import MemoryWriter

            writer = MemoryWriter(store=self._store)
            engine = InspirationEngine(
                store=self._store,
                writer=writer,
                retention_days=retention_days,
            )
            result = await engine.recycle()
            promoted = result.get("promoted", 0)
            discarded = result.get("discarded", 0)
            insights = result.get("insights", 0)
            if promoted > 0 or discarded > 0 or insights > 0:
                logger.info(f"噪声回收: 晋升 {promoted} 条，丢弃 {discarded} 条，伏笔洞见 {insights} 条")
            return promoted, discarded, insights
        except ImportError:
            logger.debug("InspirationEngine 不可用，跳过噪声回收")
            return 0, 0, 0
        except Exception as e:
            logger.warning("噪声回收阶段异常: %s", e)
            return 0, 0, 0

    async def _clean_noise(self, older_than_days: int | None = None) -> int:
        """Phase 2 — 清理噪声池中过期的噪声条目

        删除 NoisePool 中创建时间距今超过 older_than_days 的记录。

        Returns:
            清理的噪声条目数量
        """
        try:
            retention_days = older_than_days if older_than_days is not None else NOISE_CLEANUP_DAYS
            cutoff = datetime.datetime.now() - datetime.timedelta(days=retention_days)

            with memory_db:
                deleted = NoisePool.delete().where(NoisePool.created_at < cutoff).execute()

            if deleted > 0:
                logger.info(f"噪声清理: {deleted} 条过期噪声已清除 (ttl={retention_days}天)")
            return deleted

        except Exception as e:
            logger.error(f"噪声清理阶段异常: {e}")
            return 0

    # ── Phase 3: 图谱构建 ─────────────────────────────────────────────

    async def _build_graph(self, limit: int | None = None) -> tuple[int, int]:
        """Phase 3 — 构建知识图谱节点/边，提取语义三元组

        选取 weight 最高的 N 个活跃原子 (N=limit or GRAPH_ATOMS_LIMIT)：
        1. 从原子 entities 字段提取实体，创建/获取 GraphNode
        2. 对出现在同一原子中的实体对，创建 GraphEdge (predicate='related_to')
        3. 从 SemanticDetail 表提取 SPO 三元组，写入 GraphEntry

        Args:
            limit: 参与的原子数量上限，默认使用 GRAPH_ATOMS_LIMIT

        Returns:
            tuple[int, int]: (创建的边数, 创建的三元组数)
        """
        atom_limit = limit or GRAPH_ATOMS_LIMIT
        edges_created = 0
        entries_created = 0

        if self._graph_store is None:
            logger.debug("GraphStore 不可用，跳过图谱构建")
            return 0, 0

        try:
            with memory_db:
                top_atoms = list(
                    MemoryAtomModel.select()
                    .where(MemoryAtomModel.status == "active")
                    .order_by(MemoryAtomModel.weight.desc())
                    .limit(atom_limit)
                )

                if not top_atoms:
                    return 0, 0

                # 提取实体并构建节点/边
                atom_entities: dict[str, list[str]] = {}
                for atom_model in top_atoms:
                    entities: list[str] = []
                    if atom_model.entities:
                        try:
                            parsed = json.loads(atom_model.entities)
                            if isinstance(parsed, list):
                                for item in parsed:
                                    entity = str(item).strip()
                                    if entity and entity not in entities:
                                        entities.append(entity)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    atom_entities[atom_model.atom_id] = entities

                # 为每个 unique 实体创建/获取 GraphNode，并缓存 node_id
                all_entities = set()
                for ent_list in atom_entities.values():
                    all_entities.update(ent_list)

                entity_node_map: dict[str, int] = {}
                for entity in sorted(all_entities):
                    nid = self._graph_store.find_or_create_node("entity", entity)
                    entity_node_map[entity] = nid

                # 对同一原子中的实体对创建 GraphEdge
                for ent_list in atom_entities.values():
                    if len(ent_list) < 2:
                        continue
                    node_ids = [entity_node_map[e] for e in ent_list if e in entity_node_map]

                    for i in range(len(node_ids)):
                        for j in range(i + 1, len(node_ids)):
                            if not self._graph_store.edge_exists(node_ids[i], node_ids[j], "related_to"):
                                self._graph_store.add_edge(node_ids[i], node_ids[j], "related_to", confidence=0.6)
                                edges_created += 1

                # 从 SemanticDetail 提取 SPO 三元组
                atom_ids_entries = [a.atom_id for a in top_atoms]
                details = SemanticDetailModel.select().where(SemanticDetailModel.atom.in_(atom_ids_entries))

                for detail in details:
                    if not detail.attr_name or not detail.attr_value:
                        continue
                    # 精确匹配检查避免重复
                    existing = self._graph_store.search_entries(
                        subject=detail.attr_category,
                        predicate=detail.attr_name,
                        obj=detail.attr_value,
                    )
                    # search_entries 的 subject/obj 使用 LIKE 匹配，需二次精确过滤
                    exact_match = [
                        e for e in existing if e["subject"] == detail.attr_category and e["object"] == detail.attr_value
                    ]
                    if not exact_match:
                        self._graph_store.add_entry(
                            subject=detail.attr_category,
                            predicate=detail.attr_name,
                            obj=detail.attr_value,
                            evidence=f"atom:{detail.atom}",
                            confidence=0.7,
                        )
                        entries_created += 1

        except Exception as e:
            logger.error(f"图谱构建阶段异常: {e}")

        if edges_created > 0 or entries_created > 0:
            logger.info(f"图谱构建: {edges_created} 条边, {entries_created} 条三元组 (atoms={atom_limit})")
        return edges_created, entries_created

    # ── 日常周期 (24h) ──────────────────────────────────────────────────

    async def _run_daily_cycle(self) -> None:
        """日常梦境周期 — 作用域: 最近 24 小时修改的原子

        Phase 1: 原始消息分诊
        Phase 2: 冲突仲裁
        Phase 3: 证据驱动评分重估
        Phase 4: 记忆巩固（importance≥0.6, weight≤0.4, max 50）
        Phase 5: 遗忘生态维护
        Phase 6: 噪声清理（NoisePool > 30 天）
        """
        dream_run_id = self._create_dream_run("daily")
        if dream_run_id is None:
            return

        atoms_processed = 0
        noise_cleaned = 0
        triage_stats: dict[str, int] = {}
        conflicts_resolved = 0
        scores_reassessed = 0
        privacy_stats: dict[str, int] = {}
        forgetting_stats: dict[str, int] = {}

        try:
            summary_ingested = self._ingest_topic_bridge_summaries(max_age_days=1)
            legacy_raw_archived = await self._archive_legacy_direct_raw_atoms()
            triage_stats = await self._triage_raw_archive(max_age_days=1, dream_run_id=dream_run_id)
            if summary_ingested:
                triage_stats["summary_imported"] = summary_ingested
            if legacy_raw_archived:
                triage_stats["legacy_raw_archived"] = legacy_raw_archived
            conflicts_resolved = await self._resolve_conflicts()
            scores_reassessed = await self._reassess_memory_scores(max_age_days=1)
            privacy_stats = await self._adjust_privacy_levels(max_age_days=1)
            atoms_processed = await self._consolidate(max_age_days=1)
            forgetting_stats = await self._run_forgetting_sweep()
            noise_cleaned = await self._clean_noise()
        except Exception as e:
            logger.exception(f"日常梦境周期异常: {e}")
            self._finalize_dream_run(dream_run_id, "failed", atoms_processed, str(e))
            return

        summary = self._build_summary(
            atoms_processed,
            noise_cleaned,
            0,
            0,
            conflicts_resolved=conflicts_resolved,
            scores_reassessed=scores_reassessed,
            privacy_stats=privacy_stats,
            forgetting_stats=forgetting_stats,
            triage_stats=triage_stats,
            phase_summaries=DAILY_DREAM_PHASES,
        )
        self._finalize_dream_run(dream_run_id, "completed", atoms_processed, summary)
        logger.info(f"日常梦境完成: {summary}")

    # ── 每周周期 (7天) ──────────────────────────────────────────────────

    async def _run_weekly_cycle(self) -> None:
        """每周梦境周期 — 作用域: 最近 7 天修改的原子

        Phase 1: 冲突仲裁
        Phase 2: 证据驱动评分重估
        Phase 3: 全量巩固（max 100 个原子）
        Phase 4: 遗忘生态维护
        Phase 5: 图谱构建（top-30 原子）
        Phase 6: 跨日模式检测 — 发现同一实体的不同类型原子，记录到 InsightPool
        Phase 7: 梦呓编织（可选）— 通过 DreamWeaver 从 NoisePool 提取洞见
        Phase 8: 噪声回收（14 天窗口）
        Phase 9: 噪声清理（30 天外）
        """
        dream_run_id = self._create_dream_run("weekly")
        if dream_run_id is None:
            return

        atoms_processed = 0
        noise_cleaned = 0
        edges_created = 0
        entries_created = 0
        patterns_found = 0
        conflicts_resolved = 0
        scores_reassessed = 0
        privacy_stats: dict[str, int] = {}
        forgetting_stats: dict[str, int] = {}

        # 噪声回收计数（Phase 6 产出）
        recycled_promoted = 0
        recycled_discarded = 0
        recycled_insights = 0

        try:
            # Phase 1: 冲突仲裁
            conflicts_resolved = await self._resolve_conflicts()

            # Phase 2: 证据驱动评分重估
            scores_reassessed = await self._reassess_memory_scores(max_age_days=7, batch_size=100)

            # Phase 3: 动态隐私重评
            privacy_stats = await self._adjust_privacy_levels(max_age_days=7, batch_size=100)

            # Phase 4: 全量巩固
            atoms_processed = await self._consolidate(max_age_days=7, batch_size=100)

            # Phase 5: 单用户软上限合并泛化
            soft_cap_stats = await self._merge_overflowing_user_memories(batch_size=200)

            # Phase 6: 遗忘生态维护
            forgetting_stats = await self._run_forgetting_sweep()

            # Phase 7: 图谱构建 (top-30)
            edges_created, entries_created = await self._build_graph(limit=30)

            # Phase 8: 跨日模式检测
            patterns_found = await self._detect_cross_day_patterns()

            # Phase 9: 梦呓编织（可选，依赖 DreamWeaver + LLM）
            weaver_insights = 0
            if self._dream_weaver is not None:
                try:
                    insights = await self._dream_weaver.weave()
                    weaver_insights = len(insights)
                except Exception as we:
                    logger.warning("洞见编织阶段异常: %s", we)

            # Phase 10: 噪声回收必须先于清理，避免把可回收伏笔提前删掉
            recycled_promoted, recycled_discarded, recycled_insights = await self._recycle_noise(
                WEEKLY_NOISE_RECYCLE_DAYS
            )

            # Phase 11: 清理超出月度回收窗口的旧噪声
            noise_cleaned = await self._clean_noise(older_than_days=NOISE_CLEANUP_DAYS)

        except Exception as e:
            logger.exception(f"每周梦境周期异常: {e}")
            self._finalize_dream_run(dream_run_id, "failed", atoms_processed, str(e))
            return

        if patterns_found > 0:
            logger.info(f"跨日模式检测: 发现 {patterns_found} 个模式")
        if weaver_insights > 0:
            logger.info(f"洞见编织: 生成 {weaver_insights} 条洞见")
        if recycled_promoted > 0 or recycled_insights > 0:
            logger.info(
                f"噪声回收: 晋升 {recycled_promoted} 条，丢弃 {recycled_discarded} 条，伏笔洞见 {recycled_insights} 条"
            )

        summary = self._build_summary(
            atoms_processed,
            noise_cleaned,
            edges_created,
            entries_created,
            conflicts_resolved=conflicts_resolved,
            scores_reassessed=scores_reassessed,
            privacy_stats=privacy_stats,
            soft_cap_stats=soft_cap_stats,
            forgetting_stats=forgetting_stats,
            phase_summaries=WEEKLY_DREAM_PHASES,
        )
        if weaver_insights:
            summary += f"，{weaver_insights}条洞见"
        if recycled_promoted:
            summary += f"，回收{recycled_promoted}条"
        if recycled_insights:
            summary += f"，伏笔洞见{recycled_insights}条"
        self._finalize_dream_run(dream_run_id, "completed", atoms_processed, summary)
        logger.info(f"每周梦境完成: {summary}")

    async def _detect_cross_day_patterns(self) -> int:
        """跨日模式检测 — 扫描同一实体关联的不同类型原子

        查询最近 7 天的活跃原子，按实体分组；
        若某实体出现在多种 atom_type 的原子中，记录到 InsightPool。

        Returns:
            发现的跨日模式数量
        """
        patterns = 0
        recent_atoms = self._get_atoms_modified_since(7)
        if not recent_atoms:
            return 0

        # 按实体分组，记录每个实体出现的 atom_type 集合
        entity_types: dict[str, set[str]] = {}
        entity_atoms: dict[str, list[str]] = {}

        for atom in recent_atoms:
            if not atom.entities:
                continue
            try:
                entities = json.loads(atom.entities)
                if not isinstance(entities, list):
                    continue
            except (json.JSONDecodeError, TypeError):
                continue

            for entity in entities:
                e = str(entity)
                if e not in entity_types:
                    entity_types[e] = set()
                    entity_atoms[e] = []
                entity_types[e].add(atom.atom_type)
                entity_atoms[e].append(atom.atom_id)

        # 检查哪些实体有多个 atom_type；相同实体、类型和来源原子集合只记录一次。
        for entity, types in entity_types.items():
            if len(types) < 2:
                continue

            source_atom_ids = sorted(set(entity_atoms[entity]))
            content = f"跨日模式: 实体 '{entity}' 出现在 {len(types)} 种记忆类型中 ({', '.join(sorted(types))})"
            source_atoms = json.dumps(source_atom_ids, ensure_ascii=False)
            try:
                with memory_db:
                    existing = InsightPool.get_or_none(
                        InsightPool.agent_name == "dream_weekly",
                        InsightPool.content == content,
                        InsightPool.source_atoms == source_atoms,
                    )
                    if existing is not None:
                        continue
                    InsightPool.create(
                        content=content,
                        source_atoms=source_atoms,
                        agent_name="dream_weekly",
                        confidence=0.5,
                    )
                patterns += 1
            except Exception as e:
                logger.error(f"写入跨日模式到 InsightPool 失败: {e}")

        return patterns

    # ── 月度周期 (30天) ─────────────────────────────────────────────────

    async def _run_monthly_cycle(self) -> None:
        """月度梦境周期 — 作用域: 全部活跃原子

        Phase 1: 全量审计 — 按类型/状态/权重范围统计原子分布
        Phase 2: 画像审计 — 对每个有画像的用户，验证画像事实与存储原子的一致性
        Phase 3: 冲突仲裁、评分重估和遗忘生态维护
        Phase 4: 关系网络重建 — 全量图谱构建 (top-50) + 对比边数变化
        Phase 5: 健康诊断 — 检测孤儿原子、过期噪声、空分区
        Phase 6: 月度恍然大悟（InsightEngine 跨域扫描，可选依赖）
        Phase 7: 噪声回收（30天窗口，可选依赖 InspirationEngine）
        Phase 8: 噪声清理
        Phase 9: 生成月度总结报告
        """
        dream_run_id = self._create_dream_run("monthly")
        if dream_run_id is None:
            return

        audit_stats: dict[str, Any] = {}
        health_issues: list[str] = []
        recycled_promoted = 0
        recycled_discarded = 0
        recycled_insights = 0
        noise_cleaned = 0
        conflicts_resolved = 0
        scores_reassessed = 0
        privacy_stats: dict[str, int] = {}
        soft_cap_stats: dict[str, int] = {}
        forgetting_stats: dict[str, int] = {}

        try:
            # Phase 1: 全量审计
            audit_stats = self._audit_atom_distribution()
            logger.info(
                f"月度审计: {audit_stats.get('total_active', 0)} 个活跃原子, "
                f"{len(audit_stats.get('type_distribution', {}))} 种类型"
            )

            # Phase 2: 画像审计
            profile_discrepancies = self._audit_profiles()
            if profile_discrepancies:
                health_issues.append(f"画像差异: {profile_discrepancies} 条不匹配")

            # Phase 3: 冲突仲裁和遗忘生态维护
            conflicts_resolved = await self._resolve_conflicts()
            scores_reassessed = await self._reassess_memory_scores(batch_size=500)
            privacy_stats = await self._adjust_privacy_levels(batch_size=500)
            soft_cap_stats = await self._merge_overflowing_user_memories(batch_size=SOFT_CAP_BATCH_SIZE)
            forgetting_stats = await self._run_forgetting_sweep()

            # Phase 4: 关系网络重建
            old_edge_count = self._count_graph_edges()
            edges_created, entries_created = await self._build_graph(limit=50)
            new_edge_count = self._count_graph_edges()
            edge_delta = new_edge_count - old_edge_count
            audit_stats["edge_count_before"] = old_edge_count
            audit_stats["edge_count_after"] = new_edge_count
            audit_stats["edge_delta"] = edge_delta
            logger.info(f"关系网络重建: {old_edge_count} → {new_edge_count} 条边 ({edge_delta:+d})")

            # Phase 5: 健康诊断
            orphaned = self._count_orphaned_atoms()
            stale_noise = self._count_stale_noise(MONTHLY_NOISE_RECYCLE_DAYS)
            empty_partitions = audit_stats.get("empty_types", [])
            if orphaned:
                health_issues.append(f"孤儿原子: {orphaned} 个")
            if stale_noise:
                health_issues.append(f"过期噪声: {stale_noise} 条 (>30天)")
            if empty_partitions:
                health_issues.append(f"空分区: {', '.join(empty_partitions)}")

            # Phase 6: 月度恍然大悟（InsightEngine 跨域扫描）
            insight_count = 0
            try:
                from src.memory.insight_engine import InsightEngine

                engine = InsightEngine(self._store)
                insights = await engine.generate_monthly_insights()
                insight_count = len(insights)
                if insight_count > 0:
                    logger.info(f"月度洞察: 发现 {insight_count} 条跨域洞察")
            except ImportError:
                logger.debug("InsightEngine 不可用，跳过月度洞察")
            except Exception as ie:
                logger.warning("月度洞察阶段异常: %s", ie)

            # Phase 7: 噪声回收（30天窗口），先回收再清理
            recycled_promoted, recycled_discarded, recycled_insights = await self._recycle_noise(
                MONTHLY_NOISE_RECYCLE_DAYS
            )
            if recycled_promoted > 0 or recycled_discarded > 0 or recycled_insights > 0:
                health_issues.append(
                    f"噪声回收: 晋升{recycled_promoted}条, 丢弃{recycled_discarded}条, 伏笔洞见{recycled_insights}条"
                )

            # Phase 8: 清理月度回收窗口之外的旧噪声
            noise_cleaned = await self._clean_noise(older_than_days=NOISE_CLEANUP_DAYS)
            if noise_cleaned > 0:
                health_issues.append(f"噪声清理: 删除{noise_cleaned}条")

            # Phase 9: 生成月度报告
            report = self._build_monthly_report(audit_stats, health_issues)
            if insight_count:
                report_lines = report.split("\n")
                report_lines.insert(-1, f"跨域洞察: {insight_count} 条")
                report = "\n".join(report_lines)
            if recycled_promoted:
                report_lines = report.split("\n")
                report_lines.insert(-1, f"噪声回收: 晋升{recycled_promoted}条")
                report = "\n".join(report_lines)
            if recycled_insights:
                report_lines = report.split("\n")
                report_lines.insert(-1, f"伏笔洞见: {recycled_insights} 条")
                report = "\n".join(report_lines)
            if conflicts_resolved:
                report_lines = report.split("\n")
                report_lines.insert(-1, f"冲突仲裁: 解决{conflicts_resolved}个")
                report = "\n".join(report_lines)
            if scores_reassessed:
                report_lines = report.split("\n")
                report_lines.insert(-1, f"评分重估: 更新{scores_reassessed}条")
                report = "\n".join(report_lines)
            if privacy_stats and any(privacy_stats.values()):
                report_lines = report.split("\n")
                report_lines.insert(
                    -1,
                    "隐私重评: "
                    f"上锁{privacy_stats.get('locked_private', 0)}条, "
                    f"解锁{privacy_stats.get('unlocked_public', 0)}条",
                )
                report = "\n".join(report_lines)
            if soft_cap_stats and any(soft_cap_stats.values()):
                report_lines = report.split("\n")
                report_lines.insert(
                    -1,
                    "软上限合并: "
                    f"压缩{soft_cap_stats.get('users_compacted', 0)}个用户, "
                    f"归档{soft_cap_stats.get('atoms_archived', 0)}条",
                )
                report = "\n".join(report_lines)
            if any(forgetting_stats.values()):
                report_lines = report.split("\n")
                report_lines.insert(
                    -1,
                    "遗忘维护: "
                    f"衰减{forgetting_stats.get('decayed', 0)}条, "
                    f"归档{forgetting_stats.get('archived', 0)}条, "
                    f"删除{forgetting_stats.get('deleted', 0)}条",
                )
                report = "\n".join(report_lines)
            self._write_monthly_report_insight(dream_run_id, report)
            logger.info(f"月度梦境报告:\n{report}")

        except Exception as e:
            logger.exception(f"月度梦境周期异常: {e}")
            self._finalize_dream_run(dream_run_id, "failed", audit_stats.get("total_active", 0), str(e))
            return

        recycle_part = f", 回收{recycled_promoted}条" if recycled_promoted else ""
        foreshadow_part = f", 伏笔洞见{recycled_insights}条" if recycled_insights else ""
        insight_part = f", {insight_count}条洞察" if insight_count else ""
        conflict_part = f", 仲裁{conflicts_resolved}个冲突" if conflicts_resolved else ""
        score_part = f", 重估{scores_reassessed}条评分" if scores_reassessed else ""
        privacy_total = privacy_stats.get("locked_private", 0) + privacy_stats.get("unlocked_public", 0)
        privacy_part = f", 隐私调整{privacy_total}条" if privacy_total else ""
        soft_cap_part = (
            f", 软上限压缩{soft_cap_stats.get('users_compacted', 0)}个用户"
            if soft_cap_stats.get("users_compacted", 0)
            else ""
        )
        cleanup_part = f", 清理{noise_cleaned}条噪声" if noise_cleaned else ""
        summary = (
            f"审计{audit_stats.get('total_active', 0)}原子, "
            f"{len(health_issues)}个问题"
            f"{insight_part}{recycle_part}{foreshadow_part}{conflict_part}{score_part}"
            f"{privacy_part}{soft_cap_part}{cleanup_part}"
        )
        phase_summary = self._format_phase_summary(MONTHLY_DREAM_PHASES)
        if phase_summary:
            summary = f"{phase_summary}，{summary}"
        self._finalize_dream_run(dream_run_id, "completed", audit_stats.get("total_active", 0), summary)
        logger.info(f"月度梦境完成: {summary}")

    # ── 月度审计工具 ────────────────────────────────────────────────────

    def _audit_atom_distribution(self) -> dict[str, Any]:
        """全量审计 — 统计所有内存原子按类型/状态/权重范围的分布

        Returns:
            包含统计信息的字典
        """
        stats: dict[str, Any] = {}
        try:
            with memory_db:
                total = MemoryAtomModel.select().count()
                active = MemoryAtomModel.select().where(MemoryAtomModel.status == "active").count()
                archived = MemoryAtomModel.select().where(MemoryAtomModel.status == "archived").count()
                forgotten = MemoryAtomModel.select().where(MemoryAtomModel.status == "forgotten").count()

                type_dist: dict[str, int] = {}
                for t in ("episodic", "factual", "relational", "preference", "planned"):
                    c = (
                        MemoryAtomModel.select()
                        .where(
                            MemoryAtomModel.status == "active",
                            MemoryAtomModel.atom_type == t,
                        )
                        .count()
                    )
                    if c > 0:
                        type_dist[t] = c

                weight_ranges: dict[str, int] = {
                    "low(0-0.3)": MemoryAtomModel.select()
                    .where(
                        MemoryAtomModel.status == "active",
                        MemoryAtomModel.weight < 0.3,
                    )
                    .count(),
                    "mid(0.3-0.7)": MemoryAtomModel.select()
                    .where(
                        MemoryAtomModel.status == "active",
                        MemoryAtomModel.weight >= 0.3,
                        MemoryAtomModel.weight < 0.7,
                    )
                    .count(),
                    "high(0.7-1.0)": MemoryAtomModel.select()
                    .where(
                        MemoryAtomModel.status == "active",
                        MemoryAtomModel.weight >= 0.7,
                    )
                    .count(),
                }

                # 发现没有活跃原子的空类型
                empty_types: list[str] = []
                for t in ("episodic", "factual", "relational", "preference", "planned"):
                    if (
                        MemoryAtomModel.select()
                        .where(
                            MemoryAtomModel.status == "active",
                            MemoryAtomModel.atom_type == t,
                        )
                        .count()
                        == 0
                    ):
                        empty_types.append(t)

                stats = {
                    "total": total,
                    "total_active": active,
                    "total_archived": archived,
                    "total_forgotten": forgotten,
                    "type_distribution": type_dist,
                    "weight_ranges": weight_ranges,
                    "empty_types": empty_types,
                }
        except Exception as e:
            logger.error(f"全量审计失败: {e}")
            stats = {"error": str(e)}
        return stats

    def _audit_profiles(self) -> int:
        """画像审计 — 用活跃语义记忆重建画像并记录差异

        对有画像的用户，按当前 active 的 factual/preference 原子重建语义画像；
        同时保留情绪历史、表达风格和统计信息等非语义字段。

        Returns:
            发现的不匹配数
        """
        discrepancies = 0
        try:
            from src.memory.user_profile import ProfileBuilder, ProfileStore

            profile_store = ProfileStore()
            profile_builder = ProfileBuilder(profile_store)
            user_ids = profile_store.list_profiles()
            if not user_ids:
                return 0

            for uid in user_ids:
                profile = profile_store.get_profile(uid)
                if not profile:
                    continue
                source_atom_ids = self._profile_source_atom_ids(profile)
                rebuilt_profile = profile_builder.build_profile(uid)
                changes = self._profile_audit_changes(profile, rebuilt_profile)
                self._restore_profile_runtime_fields(profile, rebuilt_profile)
                profile_store.save_profile(rebuilt_profile)
                if not changes:
                    continue

                discrepancies += len(changes)
                self._write_profile_audit_insight(uid, changes, source_atom_ids)
                logger.debug("画像审计: 用户 %s 发现 %d 条差异", uid, len(changes))
        except Exception as e:
            logger.error(f"画像审计失败: {e}")
        return discrepancies

    @staticmethod
    def _restore_profile_runtime_fields(previous: Any, rebuilt: Any) -> None:
        """重建语义字段后保留非语义画像字段。"""
        rebuilt.created_at = previous.created_at
        rebuilt_sources = (rebuilt.stats or {}).get("_profile_field_sources")
        rebuilt.stats = dict(previous.stats or {})
        if rebuilt_sources:
            rebuilt.stats["_profile_field_sources"] = rebuilt_sources
        rebuilt.mood_history = list(previous.mood_history or [])
        rebuilt.expression_style = previous.expression_style
        rebuilt.expression_patterns = dict(previous.expression_patterns or {})

    @staticmethod
    def _profile_audit_changes(previous: Any, rebuilt: Any) -> list[str]:
        """比较画像语义字段，返回人类可读差异描述。"""
        changes: list[str] = []
        dict_sections = (
            ("facts", "事实"),
            ("preferences", "偏好"),
            ("traits", "特征"),
        )
        for field_name, label in dict_sections:
            old_items = getattr(previous, field_name, {}) or {}
            new_items = getattr(rebuilt, field_name, {}) or {}
            for key in sorted(old_items):
                old_value = old_items[key]
                if key not in new_items:
                    changes.append(f"{label}.{key}: 移除无活跃证据的 {old_value}")
                elif str(new_items[key]) != str(old_value):
                    changes.append(f"{label}.{key}: {old_value} -> {new_items[key]}")
            for key in sorted(new_items):
                if key not in old_items:
                    changes.append(f"{label}.{key}: 新增 {new_items[key]}")

        old_interests = [str(item) for item in (previous.interests or [])]
        new_interests = [str(item) for item in (rebuilt.interests or [])]
        removed_interests = [item for item in old_interests if item not in new_interests]
        added_interests = [item for item in new_interests if item not in old_interests]
        if removed_interests:
            changes.append(f"兴趣: 移除无活跃证据的 {', '.join(removed_interests[:5])}")
        if added_interests:
            changes.append(f"兴趣: 新增 {', '.join(added_interests[:5])}")
        return changes

    def _write_profile_audit_insight(self, user_id: str, changes: list[str], source_atom_ids: list[str]) -> None:
        """写入月度画像审计洞见，便于后续报告和追溯。"""
        change_summary = "；".join(changes[:8])
        if len(changes) > 8:
            change_summary += f"；另有{len(changes) - 8}条差异"
        try:
            with memory_db:
                InsightPool.create(
                    content=f"画像审计: 用户 {user_id} 的画像已按活跃语义记忆重建；差异：{change_summary}",
                    source_atoms=json.dumps(source_atom_ids, ensure_ascii=False),
                    agent_name="dream_profile_audit",
                    confidence=0.65,
                )
        except Exception as e:
            logger.error(f"写入画像审计洞见失败 user={user_id}: {e}")

    @staticmethod
    def _profile_source_atom_ids(profile: Any) -> list[str]:
        """获取某用户参与画像重建的活跃语义原子 ID。"""
        try:
            query = (
                MemoryAtomModel.select()
                .where(
                    MemoryAtomModel.status == "active",
                    MemoryAtomModel.atom_type.in_(
                        [
                            AtomType.PREFERENCE.value,
                            AtomType.FACTUAL.value,
                        ]
                    ),
                )
                .order_by(MemoryAtomModel.weight.desc())
            )
            profile_id = str(getattr(profile, "profile_id", "") or "")
            platform = str(getattr(profile, "platform", "legacy") or "legacy")
            if profile_id and platform != "legacy":
                detail_atom_ids = SemanticDetailModel.select(SemanticDetailModel.atom).where(
                    SemanticDetailModel.subject_key == profile_id
                )
                rows = query.where(MemoryAtomModel.atom_id.in_(detail_atom_ids)).limit(50)
                return [atom.atom_id for atom in rows]

            user_id = str(getattr(profile, "user_id", profile) or "")
            entity_token = json.dumps(user_id, ensure_ascii=False)
            rows = query.where(MemoryAtomModel.entities.contains(entity_token))
            return [atom.atom_id for atom in rows if DreamTask._atom_entities_include(atom, user_id)][:50]
        except Exception as e:
            logger.error(f"画像审计来源原子扫描失败 profile={profile}: {e}")
            return []

    @staticmethod
    def _atom_entities_include(atom_model: MemoryAtomModel, entity: str) -> bool:
        """判断原子的 entities 是否包含指定实体。"""
        if not atom_model.entities:
            return False
        try:
            parsed = json.loads(atom_model.entities)
        except (json.JSONDecodeError, TypeError):
            return entity in str(atom_model.entities)
        if not isinstance(parsed, list):
            return False
        return entity in {str(item) for item in parsed}

    @staticmethod
    def _count_graph_edges() -> int:
        """统计当前图谱边总数

        Returns:
            边数
        """
        try:
            with memory_db:
                return GraphEdge.select().count()
        except Exception as e:
            logger.error(f"统计图谱边数失败: {e}")
            return 0

    @staticmethod
    def _count_orphaned_atoms() -> int:
        """统计孤儿原子 — 无实体、weight 为 0、或从未被访问

        Returns:
            孤儿原子数量
        """
        try:
            with memory_db:
                orphaned = (
                    MemoryAtomModel.select()
                    .where(
                        MemoryAtomModel.status == "active",
                        (
                            (MemoryAtomModel.entities.is_null())
                            | (MemoryAtomModel.entities == "")
                            | (MemoryAtomModel.entities == "[]")
                            | (MemoryAtomModel.weight == 0)
                        ),
                    )
                    .count()
                )
                return orphaned
        except Exception as e:
            logger.error(f"统计孤儿原子失败: {e}")
            return 0

    @staticmethod
    def _count_stale_noise(max_days: int) -> int:
        """统计过期噪声条目，不执行删除

        Args:
            max_days: 超过此天数的视为过期

        Returns:
            过期噪声数量
        """
        try:
            cutoff = datetime.datetime.now() - datetime.timedelta(days=max_days)
            with memory_db:
                return NoisePool.select().where(NoisePool.created_at < cutoff).count()
        except Exception as e:
            logger.error(f"统计过期噪声失败: {e}")
            return 0

    @staticmethod
    def _build_monthly_report(stats: dict[str, Any], health_issues: list[str]) -> str:
        """生成月度梦境总结报告

        Args:
            stats: 审计统计字典
            health_issues: 健康问题列表

        Returns:
            格式化的报告字符串
        """
        lines = ["═══ 月度梦境报告 ═══"]
        if "error" in stats:
            lines.append(f"审计异常: {stats['error']}")
            return "\n".join(lines)

        lines.append(f"原子总量: {stats.get('total', 0)}")
        lines.append(
            f"活跃: {stats.get('total_active', 0)} | "
            f"归档: {stats.get('total_archived', 0)} | "
            f"遗忘: {stats.get('total_forgotten', 0)}"
        )

        type_dist = stats.get("type_distribution", {})
        if type_dist:
            type_parts = [f"{k}: {v}" for k, v in type_dist.items()]
            lines.append(f"类型分布: {' | '.join(type_parts)}")

        weight_ranges = stats.get("weight_ranges", {})
        if weight_ranges:
            wr_parts = [f"{k}: {v}" for k, v in weight_ranges.items()]
            lines.append(f"权重分布: {' | '.join(wr_parts)}")

        edge_before = stats.get("edge_count_before", 0)
        edge_after = stats.get("edge_count_after", 0)
        if edge_before or edge_after:
            lines.append(f"关系网络: {edge_before} → {edge_after} 条边")

        if health_issues:
            lines.append(f"健康问题 ({len(health_issues)}):")
            for issue in health_issues:
                lines.append(f"  • {issue}")
        else:
            lines.append("健康状态: 无异常")

        lines.append("═══ 报告结束 ═══")
        return "\n".join(lines)

    @staticmethod
    def _write_monthly_report_insight(dream_run_id: int, report: str) -> None:
        """持久化月度报告，便于后续观察和审计。"""
        try:
            with memory_db:
                InsightPool.create(
                    content=report,
                    source_atoms=json.dumps([f"dream_run:{dream_run_id}"], ensure_ascii=False),
                    agent_name="dream_monthly_report",
                    confidence=0.8,
                )
        except Exception as e:
            logger.error(f"写入月度梦境报告失败 dream_run_id={dream_run_id}: {e}")

    # ── 工具方法 ────────────────────────────────────────────────────────

    @staticmethod
    def _format_phase_summary(phase_summaries: dict[str, tuple[str, ...]] | None) -> str | None:
        """生成 N2/N3/REM 阶段摘要。"""
        if not phase_summaries:
            return None

        parts: list[str] = []
        for phase in DREAM_SLEEP_PHASE_ORDER:
            labels = tuple(label for label in phase_summaries.get(phase, ()) if label)
            if labels:
                parts.append(f"{phase}({'+'.join(labels)})")
        if not parts:
            return None
        return f"睡眠阶段: {' | '.join(parts)}"

    @staticmethod
    def _build_summary(
        consolidated: int,
        noise_cleaned: int,
        edges: int,
        entries: int,
        *,
        conflicts_resolved: int = 0,
        scores_reassessed: int = 0,
        privacy_stats: dict[str, int] | None = None,
        soft_cap_stats: dict[str, int] | None = None,
        forgetting_stats: dict[str, int] | None = None,
        triage_stats: dict[str, int] | None = None,
        phase_summaries: dict[str, tuple[str, ...]] | None = None,
    ) -> str:
        """生成梦境运行摘要文本

        Args:
            consolidated: 巩固的原子数
            noise_cleaned: 清理的噪声数
            edges: 创建的图谱边数
            entries: 创建的三元组数
            conflicts_resolved: 解决的冲突数
            scores_reassessed: 重估评分的原子数
            privacy_stats: 隐私重评结果
            soft_cap_stats: 单用户软上限合并结果
            forgetting_stats: 遗忘扫描结果
            triage_stats: 原始消息分诊结果
            phase_summaries: N2/N3/REM 阶段标签

        Returns:
            中文摘要字符串
        """
        parts: list[str] = []
        phase_summary = DreamTask._format_phase_summary(phase_summaries)
        if phase_summary:
            parts.append(phase_summary)
        if triage_stats:
            imported_summaries = triage_stats.get("summary_imported", 0)
            if imported_summaries:
                parts.append(f"导入{imported_summaries}条对话摘要")
            legacy_raw_archived = triage_stats.get("legacy_raw_archived", 0)
            if legacy_raw_archived:
                parts.append(f"归档{legacy_raw_archived}条旧版直写原子")
            triaged = triage_stats.get("high", 0) + triage_stats.get("medium", 0) + triage_stats.get("low", 0)
            if triaged:
                parts.append(
                    "分诊"
                    f"{triaged}条原始消息"
                    f"(高{triage_stats.get('high', 0)}/中{triage_stats.get('medium', 0)}/低{triage_stats.get('low', 0)})"
                )
        if consolidated:
            parts.append(f"巩固{consolidated}条记忆")
        if conflicts_resolved:
            parts.append(f"仲裁{conflicts_resolved}个冲突")
        if scores_reassessed:
            parts.append(f"重估{scores_reassessed}条评分")
        if privacy_stats:
            locked = privacy_stats.get("locked_private", 0)
            unlocked = privacy_stats.get("unlocked_public", 0)
            if locked or unlocked:
                parts.append(f"隐私重评: 上锁{locked}条/解锁{unlocked}条")
        if soft_cap_stats:
            compacted = soft_cap_stats.get("users_compacted", 0)
            archived = soft_cap_stats.get("atoms_archived", 0)
            created = soft_cap_stats.get("summaries_created", 0)
            updated = soft_cap_stats.get("summaries_updated", 0)
            if compacted or archived or created or updated:
                parts.append(f"软上限合并: 压缩{compacted}个用户/归档{archived}条/摘要{created + updated}条")
        if forgetting_stats:
            decayed = forgetting_stats.get("decayed", 0)
            archived = forgetting_stats.get("archived", 0)
            deleted = forgetting_stats.get("deleted", 0)
            if decayed or archived or deleted:
                parts.append(f"遗忘维护: 衰减{decayed}条/归档{archived}条/删除{deleted}条")
        if noise_cleaned:
            parts.append(f"清理{noise_cleaned}条噪声")
        if edges:
            parts.append(f"构建{edges}条图谱边")
        if entries:
            parts.append(f"提取{entries}条三元组")
        return "，".join(parts) if parts else "无操作"

    def _finalize_dream_run(
        self,
        dream_run_id: int,
        status: str,
        atoms_processed: int,
        summary_or_error: str,
    ) -> None:
        """更新 DreamRun 运行记录

        Args:
            dream_run_id: DreamRun 记录 ID
            status: 结束状态（completed / failed）
            atoms_processed: 处理的原子数
            summary_or_error: 成功时传入摘要，失败时传入错误信息
        """
        try:
            with memory_db:
                DreamRun.update(
                    status=status,
                    end_time=datetime.datetime.now(),
                    atoms_processed=atoms_processed,
                    summary=summary_or_error,
                ).where(DreamRun.id == dream_run_id).execute()
        except Exception as e:
            logger.error(f"更新 DreamRun 记录失败 (id={dream_run_id}): {e}")
