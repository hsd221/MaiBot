"""
InsightEngine — 月度恍然大悟引擎 (Phase 3.2)

跨域模式扫描：从原子、画像、关联网络、梦境洞见中发现非显而易见的
模式和"aha moment"式的洞察。

所有发现基于启发式/统计算法，不调用 LLM。
"""

from __future__ import annotations

import json
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from src.common.logger import get_logger
from src.memory.schema import (
    InsightPool,
    MemoryAtom as MemoryAtomModel,
    AtomAssociationModel,
    memory_db,
)
from src.memory.store import MemoryStore
from src.memory.types import InsightItem
from src.memory.user_profile import ProfileStore

logger = get_logger("memory.insight")

# 预期原子分布比例（基于 AtomType 的 TTL 和用途估算）
_EXPECTED_DISTRIBUTION: dict[str, float] = {
    "episodic": 0.35,
    "factual": 0.25,
    "relational": 0.15,
    "preference": 0.15,
    "planned": 0.10,
}

# 洞察的最低置信度
_DEFAULT_CONFIDENCE: float = 0.6


class InsightEngine:
    """月度恍然大悟引擎

    跨 4 个维度进行模式扫描，发现隐藏在原子、画像、关联网络和梦境中的
    非显而易见模式。每次调用 generate_monthly_insights() 产出多条洞察，
    写入 InsightPool 后返回。

    Attributes:
        _store: MemoryStore 实例
        _profile_store: ProfileStore 实例
    """

    def __init__(self, store: MemoryStore):
        self._store = store
        self._profile_store = ProfileStore()

    # ── 主入口 ──────────────────────────────────────────────────────────

    async def generate_monthly_insights(self) -> list[InsightItem]:
        """运行所有 4 种跨域扫描，产出洞察列表

        Returns:
            list[InsightItem]: 洞察条目列表
        """
        # Only the read-heavy scans run in a worker. If the caller cancels
        # during scanning, the discarded result can never reach persistence.
        all_insights = await asyncio.to_thread(self._scan_monthly_insights)

        # 幂等写入 InsightPool，只返回本轮实际新增的洞见。
        saved_insights: list[InsightItem] = []
        for insight in all_insights:
            try:
                content = str(insight["content"]).strip()
                if not content:
                    continue
                source_atoms = insight.get("source_atoms")
                if source_atoms:
                    try:
                        parsed_sources = json.loads(source_atoms)
                        if isinstance(parsed_sources, list):
                            source_atoms = json.dumps(
                                sorted({str(atom_id) for atom_id in parsed_sources}),
                                ensure_ascii=False,
                            )
                    except (TypeError, json.JSONDecodeError):
                        pass
                with memory_db.atomic():
                    existing = InsightPool.get_or_none(
                        InsightPool.agent_name == "insight_engine",
                        InsightPool.content == content,
                        InsightPool.source_atoms == source_atoms,
                    )
                    if existing is not None:
                        continue
                    InsightPool.create(
                        content=content,
                        source_atoms=source_atoms,
                        agent_name="insight_engine",
                        confidence=insight.get("confidence", _DEFAULT_CONFIDENCE),
                    )
                saved_insights.append(insight)
            except Exception as e:
                logger.warning("写入洞察失败: %s", e)

        logger.info("月度洞察: %d 条新洞察已保存", len(saved_insights))
        return saved_insights

    def _scan_monthly_insights(self) -> list[InsightItem]:
        all_insights: list[InsightItem] = []

        try:
            insights_1 = self._scan_atomic_patterns()
            all_insights.extend(insights_1)
            logger.info("Scan 1 (原子模式) → %d 条洞察", len(insights_1))
        except Exception as e:
            logger.error("Scan 1 (原子模式) 异常: %s", e)

        try:
            insights_2 = self._scan_profile_evolution()
            all_insights.extend(insights_2)
            logger.info("Scan 2 (画像演化) → %d 条洞察", len(insights_2))
        except Exception as e:
            logger.error("Scan 2 (画像演化) 异常: %s", e)

        try:
            insights_3 = self._scan_association_network()
            all_insights.extend(insights_3)
            logger.info("Scan 3 (关联网络) → %d 条洞察", len(insights_3))
        except Exception as e:
            logger.error("Scan 3 (关联网络) 异常: %s", e)

        try:
            insights_4 = self._scan_dream_synthesis()
            all_insights.extend(insights_4)
            logger.info("Scan 4 (梦境综合) → %d 条洞察", len(insights_4))
        except Exception as e:
            logger.error("Scan 4 (梦境综合) 异常: %s", e)

        return all_insights

    # ── Scan 1: 原子模式发现 ────────────────────────────────────────────

    def _scan_atomic_patterns(self) -> list[InsightItem]:
        """扫描 1 — 原子模式发现

        查询所有活跃原子，按类型分组检查分布偏差；
        查找跨 3+ 类型出现的多面实体。
        """
        insights: list[InsightItem] = []

        with memory_db:
            type_counts: dict[str, int] = Counter()
            entity_types: dict[str, set[str]] = defaultdict(set)
            entity_atoms: dict[str, list[str]] = defaultdict(list)
            active_query = MemoryAtomModel.select(
                MemoryAtomModel.atom_id, MemoryAtomModel.atom_type, MemoryAtomModel.entities
            ).where(MemoryAtomModel.status == "active")
            for atom in active_query.iterator():
                type_counts[atom.atom_type] += 1
                if not atom.entities:
                    continue
                try:
                    entities = json.loads(atom.entities)
                    if not isinstance(entities, list):
                        continue
                except (json.JSONDecodeError, TypeError):
                    continue
                for ent in entities:
                    entity = str(ent)
                    entity_types[entity].add(atom.atom_type)
                    entity_atoms[entity].append(atom.atom_id)

            total = sum(type_counts.values())
            if not total:
                return insights

            # 发现过度/不足代表的类型
            for atype, expected_pct in _EXPECTED_DISTRIBUTION.items():
                actual_count = type_counts.get(atype, 0)
                actual_pct = actual_count / total if total > 0 else 0
                diff = actual_pct - expected_pct

                if diff > 0.12:
                    insights.append(
                        {
                            "content": (
                                f"过去一个月中，{atype}类型记忆占比{actual_pct:.0%}，"
                                f"显著高于预期（{expected_pct:.0%}），"
                                f"可能反映了近期高强度的{_atype_label(atype)}活动"
                            ),
                            "source_atoms": None,
                            "confidence": 0.65,
                        }
                    )
                elif diff < -0.08 and actual_count > 0:
                    insights.append(
                        {
                            "content": (
                                f"过去一个月中，{atype}类型记忆占比仅{actual_pct:.0%}，"
                                f"低于预期（{expected_pct:.0%}），"
                                f"可能存在{_atype_label(atype)}方面的信息缺失"
                            ),
                            "source_atoms": None,
                            "confidence": 0.55,
                        }
                    )

            # 查找跨 3+ 类型的多面实体
            for entity, types in entity_types.items():
                if len(types) >= 3:
                    insights.append(
                        {
                            "content": (
                                f"在过去的记忆积累中，与「{entity}」的互动覆盖了"
                                f"{', '.join(sorted(types))}等{len(types)}个不同维度，"
                                f"是一个多面且丰富的记忆主题"
                            ),
                            "source_atoms": json.dumps(entity_atoms[entity], ensure_ascii=False),
                            "confidence": 0.7,
                        }
                    )

        return insights

    # ── Scan 2: 画像演化检测 ────────────────────────────────────────────

    def _scan_profile_evolution(self) -> list[InsightItem]:
        """扫描 2 — 画像演化检测

        查看所有 UserProfile 的情绪历史轨迹和风格演变。
        """
        insights: list[InsightItem] = []

        user_ids = self._profile_store.list_profiles()
        if not user_ids:
            return insights

        for uid in user_ids:
            profile = self._profile_store.get_profile(uid)
            if not profile:
                continue

            # 检查情绪历史
            mood_history = getattr(profile, "mood_history", [])
            if len(mood_history) >= 3:
                # 简单趋势检测：最近 3 条的情绪 vs 更早的
                recent_moods = mood_history[-3:]
                older_moods = mood_history[:-3]

                recent_positive = sum(
                    1 for m in recent_moods if any("joy" in str(t).lower() for t in m.get("emotion_tags", []))
                )
                recent_negative = sum(
                    1 for m in recent_moods if any(t in ("sadness", "anger", "fear") for t in m.get("emotion_tags", []))
                )

                if recent_positive >= 2 and older_moods:
                    insights.append(
                        {
                            "content": (
                                f"你注意到{_display_name(uid)}最近情绪更加积极阳光，近期记录中正面情绪占比明显提升"
                            ),
                            "source_atoms": None,
                            "confidence": 0.6,
                        }
                    )
                elif recent_negative >= 2 and older_moods:
                    insights.append(
                        {
                            "content": (
                                f"你注意到{_display_name(uid)}最近似乎有些低落，近期情绪记录中负面情绪占比偏高"
                            ),
                            "source_atoms": None,
                            "confidence": 0.6,
                        }
                    )

            # 检查表达风格变化
            current_style = getattr(profile, "expression_style", "") or ""
            expression_patterns = getattr(profile, "expression_patterns", {}) or {}

            if current_style and expression_patterns:
                style_parts = current_style.split(",")
                if len(style_parts) >= 2:
                    insights.append(
                        {
                            "content": (
                                f"你注意到{_display_name(uid)}最近表达风格偏向"
                                f"「{style_parts[0].strip()}」，"
                                f"和以往相比有些不一样了"
                            ),
                            "source_atoms": None,
                            "confidence": 0.55,
                        }
                    )

        return insights

    # ── Scan 3: 关联网络分析 ───────────────────────────────────────────

    def _scan_association_network(self) -> list[InsightItem]:
        """扫描 3 — 关联网络分析

        分析 AtomAssociation 记录，寻找 hub 原子和密集子图。
        """
        insights: list[InsightItem] = []

        with memory_db:
            active_atom_ids = MemoryAtomModel.select(MemoryAtomModel.atom_id).where(MemoryAtomModel.status == "active")
            associations = AtomAssociationModel.select(
                AtomAssociationModel.atom_a_id,
                AtomAssociationModel.atom_b_id,
                AtomAssociationModel.association_type,
            ).where(
                AtomAssociationModel.atom_a_id.in_(active_atom_ids),
                AtomAssociationModel.atom_b_id.in_(active_atom_ids),
            )
            # 统计每个原子的关联度数
            degree: Counter[str] = Counter()
            type_counts: Counter[str] = Counter()
            for assoc in associations.iterator():
                degree[assoc.atom_a_id] += 1
                degree[assoc.atom_b_id] += 1
                type_counts[assoc.association_type] += 1

            # 查找 hub 原子（度 ≥ 4）
            if degree:
                hub_atoms = degree.most_common(5)
                for atom_id, deg in hub_atoms:
                    if deg < 4:
                        continue
                    atom_model = MemoryAtomModel.select().where(MemoryAtomModel.atom_id == atom_id).first()
                    if atom_model and atom_model.content:
                        content_preview = atom_model.content[:40]
                        insights.append(
                            {
                                "content": (
                                    f"围绕「{content_preview}」形成了一个记忆集群，该原子关联了{deg}条其他记忆"
                                ),
                                "source_atoms": json.dumps([atom_id], ensure_ascii=False),
                                "confidence": 0.65,
                            }
                        )

            # 按关联类型统计
            if type_counts:
                dominant_type, dominant_count = type_counts.most_common(1)[0]
                total_assoc = sum(type_counts.values())
                dominant_pct = dominant_count / total_assoc * 100
                if dominant_pct > 50:
                    insights.append(
                        {
                            "content": (
                                f"记忆关联网络中以{_assoc_label(dominant_type)}关系为主"
                                f"（{dominant_count}/{total_assoc}），"
                                f"说明记忆之间存在大量{_assoc_label(dominant_type)}连接"
                            ),
                            "source_atoms": None,
                            "confidence": 0.6,
                        }
                    )

        return insights

    # ── Scan 4: 梦境洞见综合 ───────────────────────────────────────────

    def _scan_dream_synthesis(self) -> list[InsightItem]:
        """扫描 4 — 梦境洞见综合

        收集最近 30 天 DreamWeaver 产生的洞见，检查反复出现
        的主题/情绪，合成元洞见。
        """
        insights: list[InsightItem] = []

        cutoff = datetime.now() - timedelta(days=30)

        with memory_db:
            dream_insights = list(
                InsightPool.select()
                .where(
                    InsightPool.agent_name == "dream_weaver",
                    InsightPool.created_at >= cutoff,
                )
                .order_by(InsightPool.created_at.desc())
            )

            if len(dream_insights) < 3:
                return insights

            # 简单主题聚类：检查内容关键词重复
            contents = [di.content for di in dream_insights if di.content]
            if not contents:
                return insights

            # 提取常见中文主题词（2-4 字）
            # 使用简单的词频统计
            all_words: list[str] = []
            stop_words = {"一个", "这个", "那个", "什么", "没有", "可以", "还是", "就是", "不是", "但是"}

            for c in contents:
                # 简单分词：按空格/逗号/句号切分，提取 2-4 字片段
                for sep in ("，", "。", "！", "？", " ", "、", "：", "；"):
                    c = c.replace(sep, "|")
                for part in c.split("|"):
                    part = part.strip()
                    if 2 <= len(part) <= 8 and part not in stop_words:
                        all_words.append(part)

            word_freq = Counter(all_words)
            common_themes = word_freq.most_common(5)
            significant_themes = [(w, c) for w, c in common_themes if c >= 3]

            if significant_themes:
                theme_str = "、".join(w for w, _ in significant_themes[:3])
                source_ids = json.dumps([str(di.id) for di in dream_insights[:5]], ensure_ascii=False)
                insights.append(
                    {
                        "content": (
                            f"最近一个月梦境系统反复触及「{theme_str}」相关主题，"
                            f"累计在{len(significant_themes)}个主题词上出现了高频重复，"
                            f"这些主题可能反映了你在潜意识层面的持续关注"
                        ),
                        "source_atoms": source_ids,
                        "confidence": 0.55,
                    }
                )

        return insights


# ── 工具函数 ──────────────────────────────────────────────────────────


def _atype_label(atype: str) -> str:
    """返回 atom_type 的中文标签"""
    labels = {
        "episodic": "情景",
        "factual": "事实",
        "relational": "关系",
        "preference": "偏好",
        "planned": "计划",
    }
    return labels.get(atype, atype)


def _assoc_label(atype: str) -> str:
    """返回关联类型的中文标签"""
    labels = {
        "co_occurrence": "共现",
        "causal": "因果",
        "sequential": "时序",
        "dream_discovered": "梦境发现",
    }
    return labels.get(atype, atype)


def _display_name(user_id: str) -> str:
    """从 user_id 生成可读的用户名"""
    if user_id.startswith("user_"):
        return f"用户{user_id[5:]}"
    return user_id
