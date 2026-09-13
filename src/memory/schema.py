"""记忆系统数据库模型 — Peewee ORM 定义

所有记忆相关的数据库模型定义，使用独立的 memory.db 数据库。
"""

import datetime
import json
import os

from peewee import (
    Model,
    SqliteDatabase,
    TextField,
    FloatField,
    IntegerField,
    DateTimeField,
    AutoField,
    DoubleField,
)

from src.common.logger import get_logger

logger = get_logger("memory.schema")

# ---------------------------------------------------------------------------
# 数据库连接 — 独立的 memory.db，与主 RiyaBot.db 分离
# ---------------------------------------------------------------------------

_ROOT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DB_DIR = os.path.join(_ROOT_PATH, "data")
_MEMORY_DB_FILE = os.path.join(_DB_DIR, "memory.db")
_SQLITE_PRAGMAS = {
    "journal_mode": "wal",
    "cache_size": -64 * 1000,
    "foreign_keys": 1,
    "ignore_check_constraints": 0,
    "synchronous": 1,  # WAL + NORMAL 保持崩溃后的数据库一致性
    "busy_timeout": 1000,
}

os.makedirs(_DB_DIR, exist_ok=True)

memory_db = SqliteDatabase(
    _MEMORY_DB_FILE,
    pragmas=_SQLITE_PRAGMAS,
)


def configure_memory_database(sqlite_path: str) -> None:
    """按配置重设 memory_db 路径。"""
    global _MEMORY_DB_FILE
    if not sqlite_path:
        return

    resolved_path = sqlite_path
    if not os.path.isabs(resolved_path):
        resolved_path = os.path.join(_ROOT_PATH, resolved_path)
    resolved_path = os.path.abspath(resolved_path)

    current_path = os.path.abspath(memory_db.database)
    if current_path == resolved_path:
        return

    os.makedirs(os.path.dirname(resolved_path), exist_ok=True)
    if not memory_db.is_closed():
        memory_db.close()
    memory_db.init(resolved_path, pragmas=_SQLITE_PRAGMAS)
    _MEMORY_DB_FILE = resolved_path
    logger.info(f"记忆数据库路径已配置: {_MEMORY_DB_FILE}")


class BaseModel(Model):
    """记忆系统基础模型，所有模型继承自此"""

    class Meta:
        database = memory_db


# ---------------------------------------------------------------------------
# 1. 记忆原子主表
# ---------------------------------------------------------------------------


class MemoryAtom(BaseModel):
    """记忆原子主表 — 所有类型记忆的通用基础字段"""

    atom_id = TextField(primary_key=True)  # 原子唯一 ID（UUID）
    atom_type = TextField(index=True)  # 类型: episodic/factual/relational/preference/planned
    content = TextField()  # 原子内容（客观事实描述）
    entities = TextField(null=True)  # 涉及的实体列表 JSON
    importance = FloatField(default=0.5)  # 重要性评分 0-1
    confidence = FloatField(default=0.5)  # 置信度 0-1
    weight = FloatField(default=0.5)  # 当前权重（综合重要性、衰减、激活）
    created_at = DateTimeField(default=datetime.datetime.now)  # 创建时间
    last_accessed_at = DateTimeField(default=datetime.datetime.now)  # 最后访问时间
    last_reinforced_at = DateTimeField(default=datetime.datetime.now)  # 最后强化时间
    ttl_days = IntegerField(default=7)  # TTL（天）
    decay_type = TextField(default="exponential")  # 衰减类型: linear/exponential/step
    reinforcement_count = IntegerField(default=0)  # 强化次数
    source_scene = TextField(default="chat")  # 来源场景: chat/private/dream
    privacy_level = TextField(default="public")  # 隐私级别: public/context_sensitive/private
    trace_chain_id = TextField(null=True)  # 追溯链 ID
    status = TextField(default="active")  # 状态: active/archived/forgotten
    embedding_id = TextField(null=True)  # Qdrant point ID
    source_id = TextField(null=True, index=True)  # 来源聊天流 ID（ChatStream.stream_id）

    class Meta:
        table_name = "memory_atoms"


# ---------------------------------------------------------------------------
# 2. 情景记忆扩展表
# ---------------------------------------------------------------------------


class EpisodicDetail(BaseModel):
    """情景记忆扩展表 — 与 MemoryAtom 1:1 关系"""

    id = TextField(primary_key=True)  # 同 atom_id
    atom = TextField(index=True)  # FK → MemoryAtom.atom_id
    event_time = DateTimeField(null=True)  # 事件发生时间
    participants = TextField(null=True)  # 参与者列表 JSON
    emotion_tags = TextField(null=True)  # 情绪标签 JSON
    sensory_tags = TextField(null=True)  # 感官标签 JSON
    temporal_context = TextField(null=True)  # 时间语境描述（如"深夜"）

    class Meta:
        table_name = "episodic_details"


# ---------------------------------------------------------------------------
# 3. 语义记忆扩展表
# ---------------------------------------------------------------------------


class SemanticDetail(BaseModel):
    """语义记忆扩展表 — 与 MemoryAtom 1:1 关系"""

    id = TextField(primary_key=True)  # 同 atom_id
    atom = TextField(index=True)  # FK → MemoryAtom.atom_id
    attr_category = TextField()  # 属性分类
    attr_name = TextField()  # 属性名
    attr_value = TextField()  # 属性值
    subject_key = TextField(null=True, index=True)  # 平台化人物画像 ID
    evidence_list = TextField(null=True)  # 证据列表 JSON
    evidence_counter = IntegerField(default=1)  # 证据计数器

    class Meta:
        table_name = "semantic_details"


# ---------------------------------------------------------------------------
# 4. 记忆追溯链表
# ---------------------------------------------------------------------------


class MemoryTraceChain(BaseModel):
    """记忆追溯链表 — 记录记忆原子从原始数据到入库的完整加工路径"""

    id = AutoField()
    atom_id = TextField(index=True)  # FK → MemoryAtom.atom_id
    step_number = IntegerField()  # 步骤序号
    agent_name = TextField()  # Agent 名称
    operation_type = TextField()  # 操作类型: extract/transform/verify/merge
    input_source = TextField(null=True)  # 输入来源（文本摘要）
    output_summary = TextField(null=True)  # 输出摘要
    confidence_decay = FloatField(default=1.0)  # 置信度衰减因子
    timestamp = DateTimeField(default=datetime.datetime.now)  # 操作时间

    class Meta:
        table_name = "memory_trace_chain"


# ---------------------------------------------------------------------------
# 5. 原始消息归档表
# ---------------------------------------------------------------------------


class RawMessageArchive(BaseModel):
    """原始消息归档表 — 供梦境系统回放使用"""

    id = AutoField()
    stream_id = TextField(index=True)  # 聊天流 ID
    message_id = TextField(index=True)  # 原始消息 ID
    user_id = TextField()  # 用户 ID
    platform = TextField(default="legacy", index=True)  # 发送者平台
    nickname = TextField(default="")  # 平台昵称
    cardname = TextField(default="")  # 群名片
    group_id = TextField(default="")  # 群 ID
    group_name = TextField(default="")  # 群名称
    content = TextField()  # 消息内容
    timestamp = DoubleField()  # 消息时间戳
    chat_type = TextField()  # 聊天类型: group/private
    dream_status = TextField(default="pending", index=True)  # 梦境处理状态: pending/triaged/skipped
    dream_route = TextField(null=True, index=True)  # 梦境分诊路由: high/medium/low/skipped
    dream_significance = FloatField(null=True)  # 梦境分诊显著性评分
    dream_processed_at = DateTimeField(null=True)  # 梦境分诊完成时间
    dream_run_id = IntegerField(null=True, index=True)  # 关联的 DreamRun.id；历史记录可为空

    class Meta:
        table_name = "raw_message_archive"
        indexes = ((("stream_id", "message_id", "chat_type"), True),)


# ---------------------------------------------------------------------------
# 6. 冲突观察区
# ---------------------------------------------------------------------------


class ConflictObservation(BaseModel):
    """冲突观察区 — 待仲裁的矛盾记忆"""

    id = AutoField()
    atom_a_id = TextField(index=True)  # FK → MemoryAtom.atom_id
    atom_b_id = TextField(index=True)  # FK → MemoryAtom.atom_id
    conflict_type = TextField()  # 冲突类型
    description = TextField()  # 冲突描述
    status = TextField(default="pending")  # 状态: pending/resolved/ignored
    created_at = DateTimeField(default=datetime.datetime.now)  # 发现时间

    class Meta:
        table_name = "conflict_observations"


# ---------------------------------------------------------------------------
# 7. 噪声池
# ---------------------------------------------------------------------------


class NoisePool(BaseModel):
    """噪声池 — 低显著性信息，等待梦境回收"""

    id = AutoField()
    content = TextField()  # 内容
    source_scene = TextField(default="chat")  # 来源场景
    source_id = TextField(null=True, index=True)  # 来源 ID，如 raw_message_archive:<id>
    significance = FloatField(default=0.0)  # 显著性评分
    created_at = DateTimeField(default=datetime.datetime.now)  # 创建时间
    ttl_days = IntegerField(default=7)  # 保留天数

    class Meta:
        table_name = "noise_pool"


# ---------------------------------------------------------------------------
# 8. 洞见池
# ---------------------------------------------------------------------------


class InsightPool(BaseModel):
    """洞见池 — 梦境编织者等产出的洞见记忆"""

    id = AutoField()
    content = TextField()  # 洞见内容
    source_atoms = TextField(null=True)  # 来源原子 ID 列表 JSON
    agent_name = TextField()  # 产出 Agent 名称
    confidence = FloatField(default=0.5)  # 置信度
    created_at = DateTimeField(default=datetime.datetime.now)  # 创建时间

    class Meta:
        table_name = "insight_pool"


# ---------------------------------------------------------------------------
# 9-11. 图记忆相关表
# ---------------------------------------------------------------------------


class GraphNode(BaseModel):
    """图记忆节点表"""

    id = AutoField()
    node_type = TextField()  # 节点类型
    label = TextField(index=True)  # 节点标签
    properties = TextField(null=True)  # 属性 JSON

    class Meta:
        table_name = "graph_nodes"


class GraphEdge(BaseModel):
    """图记忆边表"""

    id = AutoField()
    source_node_id = TextField(index=True)  # FK → GraphNode.id
    target_node_id = TextField(index=True)  # FK → GraphNode.id
    predicate = TextField()  # 关系谓词
    confidence = FloatField(default=0.5)  # 置信度

    class Meta:
        table_name = "graph_edges"


class GraphEntry(BaseModel):
    """图记忆条目表 — 三元组 + 原文证据"""

    id = AutoField()
    subject = TextField(index=True)  # 主语
    predicate = TextField()  # 谓词
    object = TextField()  # 宾语
    evidence = TextField(null=True)  # 原文证据
    confidence = FloatField(default=0.5)  # 置信度

    class Meta:
        table_name = "graph_entries"


# ---------------------------------------------------------------------------
# 12. 原子关联表
# ---------------------------------------------------------------------------


class AtomAssociationModel(BaseModel):
    """原子关联表 — 记忆原子之间的显式关联边

    记录原子之间的直接关联关系，支持共现、因果、时序、梦境发现四种类型。
    每条关联记录包含权重和证据计数，支持增量更新。
    """

    id = AutoField()
    atom_a_id = TextField(index=True)  # FK → MemoryAtom.atom_id
    atom_b_id = TextField(index=True)  # FK → MemoryAtom.atom_id
    association_type = TextField()  # 关联类型: co_occurrence/causal/sequential/dream_discovered
    weight = FloatField(default=0.5)  # 关联强度 0-1
    evidence_count = IntegerField(default=1)  # 证据计数，每次 upsert 递增
    created_at = DateTimeField(default=datetime.datetime.now)  # 创建时间

    class Meta:
        table_name = "atom_associations"
        indexes = (
            (("atom_a_id", "atom_b_id", "association_type"), True),  # unique composite
        )


# ---------------------------------------------------------------------------
# 13. 梦境运行记录
# ---------------------------------------------------------------------------


class DreamRun(BaseModel):
    """梦境运行记录表"""

    id = AutoField()
    run_type = TextField()  # 运行类型: daily/weekly/monthly
    start_time = DateTimeField()  # 开始时间
    end_time = DateTimeField(null=True)  # 结束时间
    status = TextField(default="running")  # 状态: running/completed/failed
    atoms_processed = IntegerField(default=0)  # 处理的原子数
    atoms_created = IntegerField(default=0)  # 创建的原子数
    summary = TextField(null=True)  # 运行摘要

    class Meta:
        table_name = "dream_runs"


# ---------------------------------------------------------------------------
# 14. 向量索引迁移状态
# ---------------------------------------------------------------------------


class VectorIndexState(BaseModel):
    """Persisted state for versioned embedding collection migrations."""

    index_name = TextField(primary_key=True)
    active_signature = TextField(null=True)
    active_dimension = IntegerField(null=True)
    active_collection = TextField(null=True)
    target_signature = TextField(null=True)
    target_dimension = IntegerField(null=True)
    target_collection = TextField(null=True)
    status = TextField(default="ready")  # ready/migrating/failed
    last_processed_id = TextField(null=True)
    migrated_count = IntegerField(default=0)
    total_count = IntegerField(default=0)
    last_error = TextField(null=True)
    updated_at = DateTimeField(default=datetime.datetime.now)

    class Meta:
        table_name = "vector_index_state"


# ---------------------------------------------------------------------------
# 15. 编码写入 Outbox
# ---------------------------------------------------------------------------


class PendingAtomWrite(BaseModel):
    """已完成 Layer 2 校验、等待 Layer 3 提交的固定原子写入单元。"""

    atom_id = TextField(primary_key=True)
    stream_id = TextField(index=True)
    payload = TextField()
    created_at = DateTimeField(default=datetime.datetime.now)
    updated_at = DateTimeField(default=datetime.datetime.now)

    class Meta:
        table_name = "pending_atom_writes"


# ---------------------------------------------------------------------------
# 模型注册表 & 自动建表
# ---------------------------------------------------------------------------

MODELS = [
    MemoryAtom,
    EpisodicDetail,
    SemanticDetail,
    MemoryTraceChain,
    RawMessageArchive,
    ConflictObservation,
    NoisePool,
    InsightPool,
    GraphNode,
    GraphEdge,
    GraphEntry,
    AtomAssociationModel,
    DreamRun,
    VectorIndexState,
    PendingAtomWrite,
]


def _ensure_indexes():
    """创建常用查询索引（幂等，CREATE INDEX IF NOT EXISTS）

    针对 MemoryAtomModel、RawMessageArchiveModel、ConflictObservation
    的高频查询模式添加复合索引，避免全表扫描。
    """
    index_defs: list[tuple[str, str]] = [
        (
            "idx_memory_atoms_status_weight",
            "CREATE INDEX IF NOT EXISTS idx_memory_atoms_status_weight ON memory_atoms(status, weight)",
        ),
        (
            "idx_memory_atoms_weight",
            "CREATE INDEX IF NOT EXISTS idx_memory_atoms_weight ON memory_atoms(weight)",
        ),
        (
            "idx_memory_atoms_type_status_weight",
            "CREATE INDEX IF NOT EXISTS idx_memory_atoms_type_status_weight ON memory_atoms(atom_type, status, weight)",
        ),
        (
            "idx_memory_atoms_created_status",
            "CREATE INDEX IF NOT EXISTS idx_memory_atoms_created_status ON memory_atoms(created_at, status)",
        ),
        (
            "idx_memory_atoms_source_scene_id",
            "CREATE INDEX IF NOT EXISTS idx_memory_atoms_source_scene_id ON memory_atoms(source_scene, source_id, status, weight)",
        ),
        (
            "idx_semantic_details_subject_key",
            "CREATE INDEX IF NOT EXISTS idx_semantic_details_subject_key ON semantic_details(subject_key)",
        ),
        (
            "idx_raw_archive_stream_ts",
            "CREATE INDEX IF NOT EXISTS idx_raw_archive_stream_ts ON raw_message_archive(stream_id, timestamp)",
        ),
        (
            "idx_raw_archive_dream_status_ts",
            "CREATE INDEX IF NOT EXISTS idx_raw_archive_dream_status_ts ON raw_message_archive(dream_status, timestamp)",
        ),
        (
            "idx_raw_archive_dream_run_processed",
            "CREATE INDEX IF NOT EXISTS idx_raw_archive_dream_run_processed "
            "ON raw_message_archive(dream_run_id, dream_processed_at)",
        ),
        (
            "idx_raw_archive_unique_message",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_archive_unique_message "
            "ON raw_message_archive(stream_id, message_id, chat_type)",
        ),
        (
            "idx_conflict_status_created",
            "CREATE INDEX IF NOT EXISTS idx_conflict_status_created ON conflict_observations(status, created_at)",
        ),
        (
            "idx_insight_pool_identity",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_insight_pool_identity "
            "ON insight_pool(agent_name, content, COALESCE(source_atoms, ''))",
        ),
    ]
    for idx_name, ddl in index_defs:
        try:
            memory_db.execute_sql(ddl)
        except Exception as e:
            logger.warning("创建索引 %s 失败: %s", idx_name, e)


def _dedupe_raw_message_archive() -> int:
    """合并历史重复归档记录，确保唯一索引可安全创建。"""
    if not memory_db.table_exists(RawMessageArchive):
        return 0

    duplicates = memory_db.execute_sql(
        """
        SELECT stream_id, message_id, chat_type
        FROM raw_message_archive
        GROUP BY stream_id, message_id, chat_type
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    removed = 0
    for stream_id, message_id, chat_type in duplicates:
        rows = list(
            RawMessageArchive.select()
            .where(
                RawMessageArchive.stream_id == stream_id,
                RawMessageArchive.message_id == message_id,
                RawMessageArchive.chat_type == chat_type,
            )
            .order_by(RawMessageArchive.id.asc())
        )
        if len(rows) <= 1:
            continue

        keep = rows[0]
        for row in rows[1:]:
            if _raw_archive_dream_state_rank(row.dream_status) > _raw_archive_dream_state_rank(keep.dream_status):
                keep.dream_status = row.dream_status
                keep.dream_route = row.dream_route
                keep.dream_significance = row.dream_significance
                keep.dream_processed_at = row.dream_processed_at
                keep.dream_run_id = row.dream_run_id
            elif keep.dream_processed_at is None and row.dream_processed_at is not None:
                keep.dream_route = row.dream_route
                keep.dream_significance = row.dream_significance
                keep.dream_processed_at = row.dream_processed_at
                keep.dream_run_id = row.dream_run_id
            row.delete_instance()
            removed += 1
        keep.save()

    if removed:
        logger.warning("原始消息归档表已合并重复记录", removed=removed)
    return removed


def _normalize_insight_sources(source_atoms: str | None) -> str | None:
    """规范洞见来源集合，使等价 JSON 在数据库中使用同一身份。"""
    if source_atoms is None:
        return None
    try:
        parsed = json.loads(source_atoms)
    except (TypeError, json.JSONDecodeError):
        return source_atoms
    if not isinstance(parsed, list):
        return source_atoms
    return json.dumps(
        sorted({str(source_id) for source_id in parsed}),
        ensure_ascii=False,
    )


def _dedupe_insight_pool() -> int:
    """规范化并合并历史重复洞见，确保唯一索引可安全创建。"""
    if not memory_db.table_exists(InsightPool):
        return 0

    removed = 0
    grouped: dict[tuple[str, str, str], list[tuple[InsightPool, str | None]]] = {}
    rows = list(InsightPool.select().order_by(InsightPool.id.asc()))
    for row in rows:
        normalized_sources = _normalize_insight_sources(row.source_atoms)
        identity = (
            str(row.agent_name),
            str(row.content),
            normalized_sources or "",
        )
        grouped.setdefault(identity, []).append((row, normalized_sources))

    for group in grouped.values():
        keep, normalized_sources = group[0]
        highest_confidence = max(row.confidence for row, _ in group)
        for row, _ in group[1:]:
            row.delete_instance()
            removed += 1

        changed_fields = []
        if keep.source_atoms != normalized_sources:
            keep.source_atoms = normalized_sources
            changed_fields.append(InsightPool.source_atoms)
        if keep.confidence != highest_confidence:
            keep.confidence = highest_confidence
            changed_fields.append(InsightPool.confidence)
        if changed_fields:
            keep.save(only=changed_fields)

    if removed:
        logger.warning("洞见池已合并重复记录", removed=removed)
    return removed


def _raw_archive_dream_state_rank(status: str | None) -> int:
    return {
        "pending": 0,
        "skipped": 1,
        "triaged": 2,
    }.get(status or "pending", 0)


def _ensure_columns():
    """补齐已存在数据库缺失的新列。"""
    try:
        if not memory_db.table_exists(MemoryAtom):
            return
        atom_rows = memory_db.execute_sql("PRAGMA table_info(memory_atoms)").fetchall()
        atom_columns = {row[1] for row in atom_rows}
        if "source_id" not in atom_columns:
            memory_db.execute_sql("ALTER TABLE memory_atoms ADD COLUMN source_id TEXT")
            logger.info("记忆表 memory_atoms 已补充 source_id 列")

        if memory_db.table_exists(SemanticDetail):
            semantic_rows = memory_db.execute_sql("PRAGMA table_info(semantic_details)").fetchall()
            semantic_columns = {row[1] for row in semantic_rows}
            if "subject_key" not in semantic_columns:
                memory_db.execute_sql("ALTER TABLE semantic_details ADD COLUMN subject_key TEXT")
                logger.info("语义明细表 semantic_details 已补充 subject_key 列")

        if memory_db.table_exists(RawMessageArchive):
            raw_rows = memory_db.execute_sql("PRAGMA table_info(raw_message_archive)").fetchall()
            raw_columns = {row[1] for row in raw_rows}
            raw_column_defs = {
                "platform": "ALTER TABLE raw_message_archive ADD COLUMN platform TEXT DEFAULT 'legacy'",
                "nickname": "ALTER TABLE raw_message_archive ADD COLUMN nickname TEXT DEFAULT ''",
                "cardname": "ALTER TABLE raw_message_archive ADD COLUMN cardname TEXT DEFAULT ''",
                "group_id": "ALTER TABLE raw_message_archive ADD COLUMN group_id TEXT DEFAULT ''",
                "group_name": "ALTER TABLE raw_message_archive ADD COLUMN group_name TEXT DEFAULT ''",
                "dream_status": "ALTER TABLE raw_message_archive ADD COLUMN dream_status TEXT DEFAULT 'pending'",
                "dream_route": "ALTER TABLE raw_message_archive ADD COLUMN dream_route TEXT",
                "dream_significance": "ALTER TABLE raw_message_archive ADD COLUMN dream_significance REAL",
                "dream_processed_at": "ALTER TABLE raw_message_archive ADD COLUMN dream_processed_at DATETIME",
                "dream_run_id": "ALTER TABLE raw_message_archive ADD COLUMN dream_run_id INTEGER",
            }
            for column, ddl in raw_column_defs.items():
                if column not in raw_columns:
                    memory_db.execute_sql(ddl)
                    logger.info("原始消息归档表 raw_message_archive 已补充 %s 列", column)

        if memory_db.table_exists(NoisePool):
            noise_rows = memory_db.execute_sql("PRAGMA table_info(noise_pool)").fetchall()
            noise_columns = {row[1] for row in noise_rows}
            if "source_id" not in noise_columns:
                memory_db.execute_sql("ALTER TABLE noise_pool ADD COLUMN source_id TEXT")
                logger.info("噪声池 noise_pool 已补充 source_id 列")
    except Exception as e:
        logger.warning("补齐记忆表字段失败: %s", e)


def create_tables():
    """创建所有记忆数据库表，同时创建附加索引"""
    with memory_db:
        memory_db.create_tables(MODELS, safe=True)
        _ensure_columns()
        _dedupe_raw_message_archive()
        _dedupe_insight_pool()
        _ensure_indexes()
    logger.info(f"记忆数据库表已创建: {_MEMORY_DB_FILE}")


def initialize_database():
    """初始化记忆数据库，自动创建缺失的表，并确保索引存在"""
    try:
        with memory_db:
            for model in MODELS:
                if not memory_db.table_exists(model):
                    logger.warning(f"记忆表 '{model._meta.table_name}' 未找到，正在创建...")
                    memory_db.create_tables([model], safe=True)
                    logger.info(f"记忆表 '{model._meta.table_name}' 创建成功")
            _ensure_columns()
            _dedupe_raw_message_archive()
            _dedupe_insight_pool()
            _ensure_indexes()
        logger.info("记忆数据库初始化完成")
    except Exception as e:
        logger.exception(f"记忆数据库初始化失败: {e}")
