"""
记忆原子数据模型 — 记忆系统的最小存储单元

定义 MemoryAtom 及其相关类型、权重计算、衰减函数和强化逻辑。
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Optional
import math

from src.common.logger import get_logger

logger = get_logger("memory.atom")


class AtomType(str, Enum):
    """记忆原子类型"""

    EPISODIC = "episodic"  # 情景记忆：具体事件，7天基础TTL，指数衰减
    FACTUAL = "factual"  # 事实记忆：客观知识，180天基础TTL，指数衰减
    RELATIONAL = "relational"  # 关系记忆：实体间关系，90天基础TTL，线性衰减
    PREFERENCE = "preference"  # 偏好记忆：用户偏好，60天基础TTL，指数衰减
    PLANNED = "planned"  # 计划记忆：未来计划，2天基础TTL，阶梯衰减


class DecayType(str, Enum):
    """衰减类型"""

    LINEAR = "linear"
    EXPONENTIAL = "exponential"
    STEP = "step"


# 每种原子类型的默认TTL（天）
DEFAULT_TTL = {
    AtomType.EPISODIC: 7,
    AtomType.FACTUAL: 180,
    AtomType.RELATIONAL: 90,
    AtomType.PREFERENCE: 60,
    AtomType.PLANNED: 2,
}

# 每种原子类型的默认衰减方式
DEFAULT_DECAY = {
    AtomType.EPISODIC: DecayType.EXPONENTIAL,
    AtomType.FACTUAL: DecayType.EXPONENTIAL,
    AtomType.RELATIONAL: DecayType.LINEAR,
    AtomType.PREFERENCE: DecayType.EXPONENTIAL,
    AtomType.PLANNED: DecayType.STEP,
}


@dataclass
class EpisodicDetail:
    """情景记忆扩展 — 事件时间、参与者、情绪标签、感官标签、时间语境

    Attributes:
        atom_id: 关联的记忆原子ID
        event_time: 事件发生时间戳（可选）
        participants: 参与者列表
        emotion_tags: 情绪标签列表
        sensory_tags: 感官标签列表（可选值：visual, auditory, tactile, olfactory,
            gustatory, emotional:joy, emotional:sadness, emotional:anger,
            emotional:surprise, emotional:fear）
        temporal_context: 时间语境描述（如"深夜"、"午后"、"清晨"等，可选）
    """

    atom_id: str
    event_time: Optional[float] = None
    participants: list[str] = field(default_factory=list)
    emotion_tags: list[str] = field(default_factory=list)
    sensory_tags: list[str] = field(default_factory=list)
    temporal_context: str = ""


@dataclass
class SemanticDetail:
    """语义记忆扩展 — 属性分类、证据计数

    Attributes:
        atom_id: 关联的记忆原子ID
        attr_category: 属性分类（如 personality, interest, habit 等）
        attr_name: 属性名
        attr_value: 属性值
        subject_key: 属性所属的平台化人物画像 ID
        evidence_list: 证据来源列表
        evidence_counter: 证据计数（同一属性被确认的次数）
    """

    atom_id: str
    attr_category: str = "general"
    attr_name: str = ""
    attr_value: str = ""
    subject_key: str = ""
    evidence_list: list[str] = field(default_factory=list)
    evidence_counter: int = 1


@dataclass
class MemoryAtom:
    """记忆原子 — 记忆系统的最小存储单元

    每条记忆原子代表一个独立的事实片段，具有独立的权重、生命周期和衰减特性。
    遵循客观性原则：content 只存储客观事实描述，不包含主观解读。

    Attributes:
        atom_id: 原子唯一ID
        atom_type: 原子类型（episodic/factual/relational/preference/planned）
        content: 客观事实描述
        entities: 涉及的实体列表
        importance: 重要性评分 0-1
        confidence: 置信度 0-1
        weight: 当前权重（综合重要性、衰减、激活次数计算得出）
        created_at: 创建时间戳
        last_accessed_at: 最后访问时间戳
        last_reinforced_at: 最后强化时间戳
        ttl_days: TTL（天），根据原子类型有不同默认值
        decay_type: 衰减类型（linear/exponential/step）
        reinforcement_count: 强化次数
        source_scene: 来源场景（group_chat/private_chat/dream）
        source_id: 来源聊天流 ID（ChatStream.stream_id）
        privacy_level: 隐私级别（public/context_sensitive/private）
        trace_chain_id: 追溯链ID，关联 memory_trace_chain 表
        status: 状态（active/archived/forgotten）
        embedding: 向量嵌入（可选，由外部编码生成）
        episodic_detail: 情景记忆扩展（可选，episodic 类型时填充）
        semantic_detail: 语义记忆扩展（可选，factual/preference 类型时填充）
    """

    atom_id: str
    atom_type: AtomType
    content: str
    entities: list[str] = field(default_factory=list)
    importance: float = 0.5
    confidence: float = 0.5
    weight: float = 0.5
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())
    last_accessed_at: float = field(default_factory=lambda: datetime.now().timestamp())
    last_reinforced_at: Optional[float] = None
    ttl_days: float = 7.0
    decay_type: DecayType = DecayType.EXPONENTIAL
    reinforcement_count: int = 0
    source_scene: str = "unknown"
    source_id: Optional[str] = None
    privacy_level: str = "context_sensitive"
    trace_chain_id: Optional[str] = None
    status: str = "active"
    embedding: Optional[list[float]] = None

    # Extended fields (populated from extended tables)
    episodic_detail: Optional[EpisodicDetail] = None
    semantic_detail: Optional[SemanticDetail] = None


# ── 时间工具函数 ──────────────────────────────────────────────

# 时间表示约定：
#   - 内存层（dataclass / 计算）统一使用 Unix 时间戳 float
#   - 持久层（Peewee ORM）使用 DateTimeField，通过 to_datetime() 转换
#   - 避免混用 datetime.now() 和 time.time()


def _now() -> float:
    """获取当前 Unix 时间戳（float，秒）"""
    return datetime.now().timestamp()


def to_timestamp(value: object) -> float:
    """将多种时间表示统一转为 Unix 时间戳 float

    兼容:
      - datetime 对象（Peewee DateTimeField 返回值）
      - float / int（原生时间戳）
      - ISO 格式字符串（'2024-01-01T00:00:00'）
      - None（返回当前时间）

    Args:
        value: 任意时间表示

    Returns:
        Unix 时间戳（float）
    """
    if value is None:
        return _now()
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except (ValueError, TypeError):
            return _now()
    return _now()


def to_datetime(ts: float | None) -> datetime:
    """将 Unix 时间戳（秒）转换为 datetime 对象

    Args:
        ts: Unix 时间戳，None 时返回当前时间

    Returns:
        datetime 对象
    """
    if ts is not None:
        return datetime.fromtimestamp(ts)
    return datetime.now()


def days_since(timestamp: float, current_time: Optional[float] = None) -> float:
    """计算从给定时间戳到当前时间的间隔天数

    Args:
        timestamp: 起始时间戳
        current_time: 结束时间戳（默认当前时间）

    Returns:
        间隔天数，最小为 0.0
    """
    current = current_time if current_time is not None else _now()
    return max(0.0, (current - timestamp) / 86400.0)


def days_since_last_access(atom: MemoryAtom, current_time: Optional[float] = None) -> float:
    """计算距离上次访问的天数

    Args:
        atom: 记忆原子
        current_time: 当前时间戳（可选，用于测试）

    Returns:
        距离上次访问的天数
    """
    return days_since(atom.last_accessed_at, current_time)


# ── 衰减与权重计算 ────────────────────────────────────────────


def compute_decay_factor(atom: MemoryAtom, current_time: Optional[float] = None) -> float:
    """计算时间衰减因子

    根据衰减类型和 TTL 计算衰减系数：

    - LINEAR:     max(0.0, 1.0 - elapsed / (ttl * 2))
    - EXPONENTIAL: exp(-elapsed / ttl)
    - STEP:       1.0 (elapsed < ttl) / 0.1 (elapsed < ttl * 2) / 0.0

    当 ttl <= 0 时视为完全遗忘，返回 0.0。

    Args:
        atom: 记忆原子
        current_time: 当前时间戳（可选，用于测试）

    Returns:
        衰减因子，范围 0.0-1.0
    """
    elapsed = days_since_last_access(atom, current_time)
    ttl = atom.ttl_days

    if ttl <= 0:
        return 0.0

    if atom.decay_type == DecayType.LINEAR:
        return max(0.0, 1.0 - elapsed / (ttl * 2))
    elif atom.decay_type == DecayType.EXPONENTIAL:
        return math.exp(-elapsed / ttl)
    elif atom.decay_type == DecayType.STEP:
        if elapsed < ttl:
            return 1.0
        elif elapsed < ttl * 2:
            return 0.1
        else:
            return 0.0
    else:
        return 1.0


def compute_weight(
    atom: MemoryAtom,
    consolidation_factor: float = 1.0,
    current_time: Optional[float] = None,
) -> float:
    """计算记忆原子的综合权重

    公式:
        权重 = 基础权重 × 时间衰减因子 × 激活因子 × 整理因子

    其中:
        基础权重 = importance × confidence
        时间衰减因子 = compute_decay_factor(atom)
        激活因子 = min(2.0, 1.0 + reinforcement_count × 0.1)
        整理因子 = consolidation_factor（默认 1.0，梦境整理后 1.0-1.3）

    Args:
        atom: 记忆原子
        consolidation_factor: 梦境整理因子（1.0-1.3）
        current_time: 当前时间戳（可选，用于测试）

    Returns:
        综合权重值，范围 0.0-1.0
    """
    base_weight = atom.importance * atom.confidence
    decay_factor = compute_decay_factor(atom, current_time)
    activation_factor = min(2.0, 1.0 + atom.reinforcement_count * 0.1)

    weight = base_weight * decay_factor * activation_factor * consolidation_factor
    return max(0.0, min(1.0, weight))


def update_weight(
    atom: MemoryAtom,
    current_time: Optional[float] = None,
    consolidation_factor: float = 1.0,
) -> MemoryAtom:
    """更新记忆原子权重并刷新访问时间

    返回新的 MemoryAtom（不修改原对象），同时更新 last_accessed_at 和 weight。

    Args:
        atom: 原记忆原子
        current_time: 当前时间戳（可选，用于测试）
        consolidation_factor: 梦境整理因子

    Returns:
        更新后的新 MemoryAtom
    """
    now = current_time if current_time is not None else _now()
    new_weight = compute_weight(atom, consolidation_factor, now)
    return replace(
        atom,
        last_accessed_at=now,
        weight=new_weight,
    )


# ── 强化与整理 ────────────────────────────────────────────────


def reinforce_memory(atom: MemoryAtom, level: str = "normal") -> MemoryAtom:
    """强化记忆

    根据强化等级调整记忆权重：

    - none:   权重微降 0.05（不低于 0.0），不增加强化计数
    - normal: 强化计数 +1，更新 last_reinforced_at，重新计算权重
    - strong: 强化计数 +2，更新 last_reinforced_at，重新计算权重

    对应再认强化机制：
        none   → 检索出来但回复中完全没用到
        normal → 检索出来且回复中用到了
        strong → 检索出来且回复中重点提及

    Args:
        atom: 原记忆原子
        level: 强化等级（none/normal/strong）

    Returns:
        强化后的新 MemoryAtom
    """
    now = _now()

    if level == "none":
        new_weight = max(0.0, atom.weight - 0.05)
        return replace(atom, weight=new_weight, last_accessed_at=now)

    elif level == "normal":
        new_atom = replace(
            atom,
            reinforcement_count=atom.reinforcement_count + 1,
            last_reinforced_at=now,
            last_accessed_at=now,
        )
        new_weight = compute_weight(new_atom)
        return replace(new_atom, weight=new_weight)

    elif level == "strong":
        new_atom = replace(
            atom,
            reinforcement_count=atom.reinforcement_count + 2,
            last_reinforced_at=now,
            last_accessed_at=now,
        )
        new_weight = compute_weight(new_atom)
        return replace(new_atom, weight=new_weight)

    else:
        logger.warning("无效的强化级别: %s, 仅支持 none/normal/strong", level)
        raise ValueError(f"Unknown reinforcement level: {level}")


def apply_dream_consolidation(atom: MemoryAtom, boost: float = 0.0) -> MemoryAtom:
    """梦境整理后提升权重

    梦境整理因子作为 consolidation_factor 传入 compute_weight，
    在默认 1.0 基础上加 boost，总整理因子范围 1.0-1.3。

    Args:
        atom: 原记忆原子
        boost: 梦境整理提升量（0.0-0.3）

    Returns:
        整理后的新 MemoryAtom
    """
    if not 0.0 <= boost <= 0.3:
        logger.warning("无效的巩固值: %s, 应在 0-0.3 范围内", boost)
        raise ValueError(f"Dream consolidation boost must be in range 0.0-0.3, got {boost}")

    consolidation_factor = 1.0 + boost
    new_weight = compute_weight(atom, consolidation_factor=consolidation_factor)
    now = _now()
    return replace(
        atom,
        weight=new_weight,
        last_accessed_at=now,
    )


# ── 褪色效果 ──────────────────────────────────────────────────


def get_fade_level(weight: float) -> str:
    """根据权重返回记忆褪色等级

    记忆的呈现粒度随权重降低而逐渐变粗（从完整内容到仅存提示）：

    - >0.7: 完整  — 完整内容可呈现
    - >0.3: 摘要  — 仅返回摘要版本
    - >0.1: 模糊  — 仅返回关键词和模糊印象
    - ≤0.1: 残影  — 仅返回"似乎有过这么一件事"的提示

    Args:
        weight: 记忆原子权重

    Returns:
        褪色等级（完整/摘要/模糊/残影）
    """
    if weight > 0.7:
        return "完整"
    elif weight > 0.3:
        return "摘要"
    elif weight > 0.1:
        return "模糊"
    else:
        return "残影"
