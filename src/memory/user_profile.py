"""用户画像系统 — 从记忆原子聚合用户偏好/属性/事实

基于记忆系统中的 PREFERENCE 和 FACTUAL 类型原子，
通过 SemanticDetail 扩展表提取结构化属性数据，
构建持久化的用户画像（UserProfile）。纯算法聚合，不涉及 LLM。

使用方式:
    from src.memory import ProfileStore, ProfileBuilder, ProfileRetriever, UserProfile

    store = ProfileStore()
    builder = ProfileBuilder(store)
    retriever = ProfileRetriever(store)

    # 全量构建
    profile = builder.build_profile("user_123")

    # 增量更新
    builder.update_profile_from_atom("user_123", atom)

    # LLM 上下文
    context = retriever.get_profile_context("user_123")
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Optional

from src.memory.types import MoodEntry

from peewee import (
    CharField,
    DateTimeField,
    IntegerField,
    Model,
    TextField,
)

from src.common.logger import get_logger
from src.memory.atom import MemoryAtom, AtomType

logger = get_logger("memory.profile")

PROFILE_VERSION = 2
LEGACY_PLATFORM = "legacy"
_LEGACY_EXPRESSION_ANALYSIS_KEYS = {
    "avg_message_length",
    "emoji_ratio",
    "question_ratio",
}


def _is_legacy_expression_analysis(patterns: dict[str, Any]) -> bool:
    """识别旧桥接算法写入的、没有版本标记的不可信表达统计。"""
    if not patterns or "analysis_version" in patterns:
        return False
    keys = set(patterns)
    return bool(keys & _LEGACY_EXPRESSION_ANALYSIS_KEYS) or {
        "favorite_expressions",
        "analyzed_message_count",
        "updated_at",
    }.issubset(keys)


def make_profile_id(platform: str, user_id: str) -> str:
    """构造跨平台稳定画像 ID；遗留画像继续使用原始 ID。"""
    normalized_platform = str(platform or LEGACY_PLATFORM).strip().lower() or LEGACY_PLATFORM
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        raise ValueError("user_id 不能为空")
    if normalized_platform == LEGACY_PLATFORM:
        return normalized_user_id
    return f"{normalized_platform}:{normalized_user_id}"


@dataclass(frozen=True)
class PersonIdentity:
    """由可信消息发送者信息建立的平台身份。"""

    platform: str
    user_id: str
    nickname: str = ""
    cardname: str = ""
    group_id: str = ""
    group_name: str = ""
    person_type: str = "person"
    identity_source: str = "message_sender"
    verification_status: str = "verified"

    def __post_init__(self) -> None:
        platform = str(self.platform or "").strip().lower()
        user_id = str(self.user_id or "").strip()
        if not platform or not user_id:
            raise ValueError("platform 和 user_id 不能为空")
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "user_id", user_id)
        if platform == LEGACY_PLATFORM and self.verification_status == "verified":
            object.__setattr__(self, "verification_status", "unverified")
        for field_name in ("nickname", "cardname", "group_id", "group_name"):
            object.__setattr__(self, field_name, str(getattr(self, field_name) or "").strip())

    @property
    def profile_id(self) -> str:
        return make_profile_id(self.platform, self.user_id)

    def merged_with(self, newer: "PersonIdentity") -> "PersonIdentity":
        """合并同一平台身份的展示信息，后来的非空字段优先。"""
        if self.profile_id != newer.profile_id:
            raise ValueError("只能合并同一平台用户的身份信息")
        return PersonIdentity(
            platform=self.platform,
            user_id=self.user_id,
            nickname=newer.nickname or self.nickname,
            cardname=newer.cardname or self.cardname,
            group_id=newer.group_id or self.group_id,
            group_name=newer.group_name or self.group_name,
            person_type=newer.person_type or self.person_type,
            identity_source=newer.identity_source or self.identity_source,
            verification_status=newer.verification_status or self.verification_status,
        )


# ---------------------------------------------------------------------------
# 分类关键词 — 用于将 SemanticDetail.attr_category 映射到 UserProfile 字段
# ---------------------------------------------------------------------------

_PREFERENCE_KEYWORDS = {"preference", "偏好", "兴趣", "like", "dislike", "hobby", "interest", "喜欢", "讨厌", "爱好"}
_FACT_KEYWORDS = {
    "fact",
    "profile",
    "general",
    "basic",
    "基本信息",
    "属性",
    "factual",
    "attribute",
    "trait",
    "info",
    "事实",
    "资料",
    "personality",
    "性格",
    "habit",
    "习惯",
    "skill",
    "技能",
}
_INTEREST_CATEGORIES = {"interest", "hobby", "兴趣", "爱好"}
_PERSONALITY_CATEGORIES = {"personality", "trait", "character", "性格", "个性", "人格"}
_HABIT_CATEGORIES = {"habit", "routine", "习惯", "作息"}
_SKILL_CATEGORIES = {"skill", "ability", "技能", "能力", "擅长"}
_POSITIVE_PREFERENCE_VALUES = {"like", "liked", "love", "prefer", "yes", "喜欢", "偏好", "热爱"}
_NON_INTEREST_PREFERENCE_VALUES = {
    "dislike",
    "disliked",
    "hate",
    "neutral",
    "no",
    "不喜欢",
    "讨厌",
    "反感",
    "中立",
}

# ---------------------------------------------------------------------------
# UserProfile 数据模型
# ---------------------------------------------------------------------------


@dataclass
class UserProfile:
    """用户画像数据模型

    聚合记忆系统中的偏好和事实信息，形成用户的结构化画像。

    Attributes:
        user_id: 用户唯一标识
        version: 画像版本号，随字段结构变更递增
        traits: 人格特征，键=特征名，值=置信度 0-1
        interests: 兴趣/话题列表
        preferences: 显式偏好，键=类别（如 food/music），值=偏好描述
        facts: 事实属性，键=属性名（如 age/location），值=属性值
        stats: 行为统计数据，如 message_count, active_hours
        mood_history: 感官/情绪历史记录，每条含 sensory_tags, emotion_tags, timestamp, content
        impression: 自然语言印象总结
        created_at: 首次创建时间
        updated_at: 最后更新时间
        last_extracted_at: 最后从记忆原子提取的时间
    """

    user_id: str
    # 保留旧位置参数顺序，兼容仍按位置传入 version 和语义集合的调用方。
    version: int = PROFILE_VERSION
    traits: dict[str, float] = field(default_factory=dict)
    interests: list[str] = field(default_factory=list)
    preferences: dict[str, str] = field(default_factory=dict)
    facts: dict[str, str] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    mood_history: list[MoodEntry] = field(default_factory=list)
    impression: str = ""
    expression_style: str = ""
    expression_patterns: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    last_extracted_at: Optional[datetime] = None
    platform: str = LEGACY_PLATFORM
    nickname: str = ""
    cardname: str = ""
    group_nicknames: list[dict[str, str]] = field(default_factory=list)
    person_type: str = "person"
    identity_source: str = "manual"
    verification_status: str = "verified"

    @property
    def profile_id(self) -> str:
        """平台隔离后的稳定画像主键。"""
        return make_profile_id(self.platform, self.user_id)


# ---------------------------------------------------------------------------
# Peewee 数据库模型
# ---------------------------------------------------------------------------


class UserProfileModel(Model):
    """用户画像数据库模型 — 字典字段序列化为 JSON 文本存储"""

    profile_id = CharField(primary_key=True, max_length=320, column_name="user_id")
    user_id = CharField(max_length=256, column_name="account_id", index=True)
    platform = CharField(default=LEGACY_PLATFORM, max_length=64, index=True)
    nickname = TextField(default="")
    cardname = TextField(default="")
    group_nicknames_json = TextField(default="[]")
    person_type = CharField(default="unknown", max_length=32, index=True)
    identity_source = CharField(default="legacy_entity", max_length=64)
    verification_status = CharField(default="unverified", max_length=32, index=True)
    version = IntegerField(default=PROFILE_VERSION)
    traits_json = TextField()
    interests_json = TextField()
    preferences_json = TextField()
    facts_json = TextField()
    stats_json = TextField()
    mood_history_json = TextField(default="[]")
    impression = TextField(default="")
    created_at = DateTimeField()
    updated_at = DateTimeField()
    last_extracted_at = DateTimeField(null=True)

    class Meta:
        database = None  # 由 ProfileStore 绑定时设置
        table_name = "user_profiles"


# ---------------------------------------------------------------------------
# ProfileStore — CRUD
# ---------------------------------------------------------------------------


class ProfileStore:
    """用户画像存储 — UserProfile 的 CRUD + 序列化

    管理 user_profiles 表，负责 dataclass ↔ DB 的转换。
    """

    def __init__(self):
        """初始化并绑定数据库，确保 user_profiles 表存在"""
        from src.memory.schema import memory_db as _memory_db

        self._memory_db = _memory_db
        # 绑定数据库
        UserProfileModel._meta.database = self._memory_db
        self._ensure_table()

    def _ensure_table(self) -> None:
        """确保 user_profiles 表已创建，并原地补齐身份层字段。"""
        try:
            if not UserProfileModel.table_exists():
                UserProfileModel.create_table()
                logger.info("用户画像表 'user_profiles' 已创建")

            rows = self._memory_db.execute_sql("PRAGMA table_info(user_profiles)").fetchall()
            columns = {row[1] for row in rows}
            account_id_added = False
            column_defs = {
                "account_id": "ALTER TABLE user_profiles ADD COLUMN account_id TEXT",
                "platform": "ALTER TABLE user_profiles ADD COLUMN platform TEXT NOT NULL DEFAULT 'legacy'",
                "nickname": "ALTER TABLE user_profiles ADD COLUMN nickname TEXT NOT NULL DEFAULT ''",
                "cardname": "ALTER TABLE user_profiles ADD COLUMN cardname TEXT NOT NULL DEFAULT ''",
                "group_nicknames_json": (
                    "ALTER TABLE user_profiles ADD COLUMN group_nicknames_json TEXT NOT NULL DEFAULT '[]'"
                ),
                "person_type": "ALTER TABLE user_profiles ADD COLUMN person_type TEXT NOT NULL DEFAULT 'unknown'",
                "identity_source": (
                    "ALTER TABLE user_profiles ADD COLUMN identity_source TEXT NOT NULL DEFAULT 'legacy_entity'"
                ),
                "verification_status": (
                    "ALTER TABLE user_profiles ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'unverified'"
                ),
            }
            for column, ddl in column_defs.items():
                if column not in columns:
                    self._memory_db.execute_sql(ddl)
                    logger.info("用户画像表已补充身份字段", column=column)
                    account_id_added = account_id_added or column == "account_id"
            if account_id_added:
                self._memory_db.execute_sql("UPDATE user_profiles SET account_id = user_id")
            self._memory_db.execute_sql(
                "CREATE INDEX IF NOT EXISTS idx_user_profiles_identity "
                "ON user_profiles(platform, account_id, person_type, verification_status)"
            )
        except Exception as e:
            logger.error(f"创建用户画像表失败: {e}")

    def get_profile(self, profile_id: str, platform: Optional[str] = None) -> Optional[UserProfile]:
        """从数据库加载用户画像

        Args:
            profile_id: 画像 ID；兼容仅传裸用户 ID 的单平台场景
            platform: 平台；提供时与裸用户 ID 共同定位画像

        Returns:
            UserProfile 或 None（不存在时）
        """
        try:
            row = self._resolve_profile_row(profile_id, platform=platform)
            if row is None:
                return None

            return self._profile_from_row(row)
        except Exception as e:
            logger.error(f"加载用户画像失败 ({profile_id}): {e}")
            return None

    @staticmethod
    def _profile_from_row(row: UserProfileModel) -> UserProfile:
        """统一单条加载与列表流式加载的序列化规则。"""
        stats = _safe_json_loads(row.stats_json, dict)
        expression_style = stats.pop("_expression_style", "") if isinstance(stats, dict) else ""
        expression_patterns = stats.pop("_expression_patterns", {}) if isinstance(stats, dict) else {}
        if not isinstance(expression_patterns, dict):
            expression_patterns = {}
        if _is_legacy_expression_analysis(expression_patterns):
            expression_style = ""
            expression_patterns = {}

        return UserProfile(
            user_id=row.user_id,
            platform=row.platform or LEGACY_PLATFORM,
            nickname=row.nickname or "",
            cardname=row.cardname or "",
            group_nicknames=_safe_json_loads(row.group_nicknames_json, list),
            person_type=row.person_type or "unknown",
            identity_source=row.identity_source or "legacy_entity",
            verification_status=row.verification_status or "unverified",
            version=row.version,
            traits=_safe_json_loads(row.traits_json, dict),
            interests=_safe_json_loads(row.interests_json, list),
            preferences=_safe_json_loads(row.preferences_json, dict),
            facts=_safe_json_loads(row.facts_json, dict),
            stats=stats,
            expression_style=str(expression_style or ""),
            expression_patterns=expression_patterns,
            mood_history=_safe_json_loads(row.mood_history_json, list),
            impression=row.impression or "",
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_extracted_at=row.last_extracted_at,
        )

    @staticmethod
    def _resolve_profile_row(profile_id: str, platform: Optional[str] = None) -> Optional[UserProfileModel]:
        lookup_id = make_profile_id(platform, profile_id) if platform else str(profile_id or "").strip()
        row = UserProfileModel.get_or_none(UserProfileModel.profile_id == lookup_id)
        if row is not None or platform or not lookup_id:
            return row
        matches = list(
            UserProfileModel.select()
            .where(UserProfileModel.user_id == lookup_id, UserProfileModel.person_type == "person")
            .limit(2)
        )
        return matches[0] if len(matches) == 1 else None

    def save_profile(self, profile: UserProfile) -> None:
        """保存用户画像（upsert）

        Args:
            profile: UserProfile 实例
        """
        profile.updated_at = datetime.now()

        # 将 expression 字段嵌入 stats 字典持久化
        stats_for_save = dict(profile.stats) if profile.stats else {}
        if profile.expression_style:
            stats_for_save["_expression_style"] = profile.expression_style
        if profile.expression_patterns:
            stats_for_save["_expression_patterns"] = profile.expression_patterns

        try:
            with self._memory_db:
                UserProfileModel.replace(
                    profile_id=profile.profile_id,
                    user_id=profile.user_id,
                    platform=profile.platform,
                    nickname=profile.nickname,
                    cardname=profile.cardname,
                    group_nicknames_json=json.dumps(profile.group_nicknames, ensure_ascii=False),
                    person_type=profile.person_type,
                    identity_source=profile.identity_source,
                    verification_status=profile.verification_status,
                    version=profile.version,
                    traits_json=json.dumps(profile.traits, ensure_ascii=False),
                    interests_json=json.dumps(profile.interests, ensure_ascii=False),
                    preferences_json=json.dumps(profile.preferences, ensure_ascii=False),
                    facts_json=json.dumps(profile.facts, ensure_ascii=False),
                    stats_json=json.dumps(stats_for_save, default=str, ensure_ascii=False),
                    mood_history_json=json.dumps(profile.mood_history, ensure_ascii=False, default=str),
                    impression=profile.impression,
                    created_at=profile.created_at,
                    updated_at=profile.updated_at,
                    last_extracted_at=profile.last_extracted_at,
                ).execute()
            logger.debug("用户画像已保存", profile_id=profile.profile_id)
        except Exception as e:
            logger.error(f"保存用户画像失败 ({profile.profile_id}): {e}")

    def get_or_create_profile(self, identity: PersonIdentity) -> UserProfile:
        """按可信身份获取画像，并刷新昵称、群名片及验证元数据。"""
        profile = self.get_profile(identity.profile_id)
        legacy_id: Optional[str] = None
        if profile is None:
            legacy = self.get_profile(identity.user_id)
            if legacy is not None and legacy.platform == LEGACY_PLATFORM and legacy.identity_source == "legacy_entity":
                profile = legacy
                legacy_id = legacy.profile_id
            else:
                profile = UserProfile(user_id=identity.user_id, platform=identity.platform)

        profile.user_id = identity.user_id
        profile.platform = identity.platform
        profile.person_type = identity.person_type
        profile.identity_source = identity.identity_source
        profile.verification_status = identity.verification_status
        if identity.nickname:
            profile.nickname = identity.nickname
        if identity.cardname:
            profile.cardname = identity.cardname
        self._merge_group_nickname(profile, identity)
        self.save_profile(profile)
        persisted_profile = self.get_profile(identity.profile_id)
        if persisted_profile is None:
            # ``save_profile`` historically logs and swallows storage errors;
            # never remove the legacy row when the replacement was not saved.
            return profile

        # A modern profile may have been created before the one-time migration
        # ran (for example by expression learning). Claim still-unscoped
        # evidence once per profile, but avoid scanning the atom table on the
        # hot path after the migration has completed.
        if not profile.stats.get("_legacy_semantic_claimed") and self._has_unscoped_semantic_details():
            if self._claim_legacy_semantic_details(identity):
                profile.stats["_legacy_semantic_claimed"] = True
                persisted_profile.stats["_legacy_semantic_claimed"] = True
                self.save_profile(profile)
        if legacy_id and legacy_id != profile.profile_id:
            self.delete_profile(legacy_id)
        return persisted_profile

    def _has_unscoped_semantic_details(self) -> bool:
        from src.memory.schema import SemanticDetail as SemanticDetailModel

        try:
            return (
                SemanticDetailModel.select(SemanticDetailModel.id)
                .where((SemanticDetailModel.subject_key.is_null(True)) | (SemanticDetailModel.subject_key == ""))
                .limit(1)
                .exists()
            )
        except Exception as e:
            logger.warning("检查未绑定画像语义证据失败", error=str(e))
            return False

    def _claim_legacy_semantic_details(self, identity: PersonIdentity) -> bool:
        """把被可信发送者认领的历史语义证据绑定到平台画像。"""
        from src.memory.schema import MemoryAtom as MemoryAtomModel
        from src.memory.schema import SemanticDetail as SemanticDetailModel

        entity_token = json.dumps(identity.user_id, ensure_ascii=False)
        try:
            atom_rows = list(
                MemoryAtomModel.select(MemoryAtomModel.atom_id, MemoryAtomModel.entities).where(
                    MemoryAtomModel.entities.contains(entity_token)
                )
            )
            atom_ids = [atom.atom_id for atom in atom_rows if _entities_contain_user(atom.entities, identity.user_id)]
            if not atom_ids:
                return True
            with self._memory_db.atomic():
                updated = (
                    SemanticDetailModel.update(subject_key=identity.profile_id)
                    .where(
                        SemanticDetailModel.atom.in_(atom_ids),
                        (SemanticDetailModel.subject_key.is_null(True)) | (SemanticDetailModel.subject_key == ""),
                    )
                    .execute()
                )
            if updated:
                logger.info(
                    "历史画像语义证据已绑定可信身份",
                    profile_id=identity.profile_id,
                    detail_count=updated,
                )
            return True
        except Exception as e:
            logger.warning("历史画像语义证据绑定失败", profile_id=identity.profile_id, error=str(e))
            return False

    @staticmethod
    def _merge_group_nickname(profile: UserProfile, identity: PersonIdentity) -> None:
        if not identity.group_id:
            return
        entry = {
            "platform": identity.platform,
            "group_id": identity.group_id,
            "group_name": identity.group_name,
            "group_nick_name": identity.cardname or identity.nickname,
        }
        for index, existing in enumerate(profile.group_nicknames):
            if existing.get("platform") == identity.platform and existing.get("group_id") == identity.group_id:
                profile.group_nicknames[index] = {
                    **existing,
                    **entry,
                    "group_nick_name": identity.cardname or existing.get("group_nick_name", "") or identity.nickname,
                }
                return
        profile.group_nicknames.append(entry)

    def profile_exists(self, profile_id: str) -> bool:
        """检查用户画像是否存在

        Args:
            profile_id: 画像 ID；裸用户 ID 仅在唯一匹配时兼容

        Returns:
            是否存在
        """
        return self._resolve_profile_row(profile_id) is not None

    def list_profiles(self, include_non_people: bool = False) -> list[str]:
        """列出所有存在画像的用户 ID

        Returns:
            用户 ID 列表，按 updated_at 降序
        """
        try:
            query = UserProfileModel.select(UserProfileModel.profile_id)
            if not include_non_people:
                query = query.where(UserProfileModel.person_type == "person")
            query = query.order_by(UserProfileModel.updated_at.desc())
            return [row.profile_id for row in query]
        except Exception as e:
            logger.error(f"列出用户画像失败: {e}")
            return []

    def iter_profiles(self, include_non_people: bool = False) -> Iterator[UserProfile]:
        """一次查询流式加载画像；不缓存整张表或逐个查询画像。"""
        query = UserProfileModel.select()
        if not include_non_people:
            query = query.where(UserProfileModel.person_type == "person")
        for row in query.order_by(UserProfileModel.updated_at.desc()).iterator():
            yield self._profile_from_row(row)

    def delete_profile(self, profile_id: str) -> None:
        """删除用户画像

        Args:
            profile_id: 画像 ID；裸用户 ID 仅在唯一匹配时兼容
        """
        try:
            row = self._resolve_profile_row(profile_id)
            if row is None:
                return
            with self._memory_db:
                UserProfileModel.delete().where(UserProfileModel.profile_id == row.profile_id).execute()
            logger.info(f"用户画像已删除: {row.profile_id}")
        except Exception as e:
            logger.error(f"删除用户画像失败 ({profile_id}): {e}")


# ---------------------------------------------------------------------------
# ProfileBuilder — 从记忆原子聚合
# ---------------------------------------------------------------------------


class ProfileBuilder:
    """用户画像构建器 — 从 PREFERENCE / FACTUAL 类型记忆原子聚合用户画像

    扫描用户的偏好和事实记忆原子，提取 SemanticDetail 中的结构化数据，
    合并到 UserProfile 中。纯算法聚合，不涉及 LLM。

    Args:
        profile_store: ProfileStore 实例，用于持久化画像
    """

    def __init__(self, profile_store: ProfileStore):
        self.profile_store = profile_store

    def build_profile(self, subject: str | PersonIdentity) -> UserProfile:
        """全量构建用户画像

        查询该用户的所有 PREFERENCE 和 FACTUAL 类型原子，
        提取 SemanticDetail 结构化数据，聚合为 UserProfile。

        Args:
            subject: 平台化人物身份或兼容历史数据的画像 ID

        Returns:
            构建完成的 UserProfile（已自动保存到 ProfileStore）
        """
        profile = (
            self.profile_store.get_or_create_profile(subject)
            if isinstance(subject, PersonIdentity)
            else self.profile_store.get_profile(subject)
            or UserProfile(
                user_id=subject,
                person_type="unknown",
                identity_source="legacy_api",
                verification_status="unverified",
            )
        )

        try:
            semantic_data = self._load_semantic_data(profile)
            if not semantic_data:
                logger.debug("无可用语义证据用于构建画像", profile_id=profile.profile_id)
                if not profile.impression:
                    profile.impression = f"用户 {profile.user_id} 的画像正在收集中"
                self.profile_store.save_profile(profile)
                return profile

            # 只重建由语义证据派生的字段；身份、表达画像和行为统计保留。
            profile.traits = {}
            profile.interests = []
            profile.preferences = {}
            profile.facts = {}
            self._aggregate_into_profile(profile, semantic_data)

            profile.impression = self._build_impression(profile)
            profile.last_extracted_at = datetime.now()
            self.profile_store.save_profile(profile)

            logger.info(
                "用户画像构建完成",
                profile_id=profile.profile_id,
                traits=len(profile.traits),
                interests=len(profile.interests),
                preferences=len(profile.preferences),
                facts=len(profile.facts),
            )

        except Exception as e:
            logger.exception(f"构建用户画像失败 ({profile.profile_id}): {e}")
            if not profile.impression:
                profile.impression = f"用户 {profile.user_id} 的画像构建失败"

        return profile

    def _load_semantic_data(self, profile: UserProfile) -> list[dict[str, Any]]:
        """在数据库中先按人物主体缩小范围，避免全库截断和跨平台污染。"""
        from src.memory.schema import MemoryAtom as MemoryAtomModel
        from src.memory.schema import SemanticDetail as SemanticDetailModel

        semantic_data: list[dict[str, Any]] = []
        with self.profile_store._memory_db:
            if profile.platform == LEGACY_PLATFORM:
                entity_token = json.dumps(profile.user_id, ensure_ascii=False)
                atom_rows = list(
                    MemoryAtomModel.select()
                    .where(
                        MemoryAtomModel.status == "active",
                        MemoryAtomModel.atom_type.in_([AtomType.PREFERENCE.value, AtomType.FACTUAL.value]),
                        MemoryAtomModel.entities.contains(entity_token),
                    )
                    .order_by(MemoryAtomModel.weight.desc())
                )
                atom_rows = [atom for atom in atom_rows if _entities_contain_user(atom.entities, profile.user_id)][:500]
                atom_ids = [atom.atom_id for atom in atom_rows]
                if not atom_ids:
                    return []
                detail_rows = list(
                    SemanticDetailModel.select().where(
                        SemanticDetailModel.atom.in_(atom_ids),
                        (SemanticDetailModel.subject_key.is_null(True))
                        | (SemanticDetailModel.subject_key == "")
                        | (SemanticDetailModel.subject_key == profile.profile_id),
                    )
                )
            else:
                subject_atom_ids = SemanticDetailModel.select(SemanticDetailModel.atom).where(
                    SemanticDetailModel.subject_key == profile.profile_id
                )
                atom_rows = list(
                    MemoryAtomModel.select()
                    .where(
                        MemoryAtomModel.atom_id.in_(subject_atom_ids),
                        MemoryAtomModel.status == "active",
                        MemoryAtomModel.atom_type.in_([AtomType.PREFERENCE.value, AtomType.FACTUAL.value]),
                    )
                    .order_by(MemoryAtomModel.weight.desc())
                    .limit(500)
                )
                atom_ids = [atom.atom_id for atom in atom_rows]
                if not atom_ids:
                    return []
                detail_rows = list(
                    SemanticDetailModel.select().where(
                        SemanticDetailModel.subject_key == profile.profile_id,
                        SemanticDetailModel.atom.in_(atom_ids),
                    )
                )

            details_by_atom = {detail.atom: detail for detail in detail_rows}

        for atom in atom_rows:
            detail = details_by_atom.get(atom.atom_id)
            if detail is None:
                continue
            semantic_data.append(
                {
                    "atom_id": atom.atom_id,
                    "atom_type": atom.atom_type,
                    "content": atom.content,
                    "weight": atom.weight,
                    "confidence": atom.confidence,
                    "created_at": atom.created_at,
                    "attr_category": detail.attr_category or "",
                    "attr_name": detail.attr_name or "",
                    "attr_value": detail.attr_value or "",
                    "evidence_counter": _effective_evidence_count(
                        detail.evidence_counter,
                        detail.evidence_list,
                    ),
                }
            )
        return semantic_data

    def update_profile_from_atom(self, subject: str | PersonIdentity, atom: MemoryAtom) -> Optional[UserProfile]:
        """增量更新用户画像 — 从单条记忆原子提取数据

        当新写入 PREFERENCE 或 FACTUAL 原子时更新偏好/事实/特征，
        当原子附带情景扩展详情时同步更新 mood_history。

        Args:
            subject: 平台化人物身份或历史画像 ID
            atom: 新的记忆原子（可附带 episodic_detail 或 semantic_detail）

        Returns:
            更新后的 UserProfile，无有效数据时返回 None
        """
        profile = (
            self.profile_store.get_or_create_profile(subject)
            if isinstance(subject, PersonIdentity)
            else self.profile_store.get_profile(subject)
            or UserProfile(
                user_id=subject,
                person_type="unknown",
                identity_source="legacy_api",
                verification_status="unverified",
            )
        )

        updated = False

        # 1. 处理 PREFERENCE / FACTUAL 语义数据
        if atom.atom_type in (AtomType.PREFERENCE, AtomType.FACTUAL) and atom.semantic_detail is not None:
            sd = atom.semantic_detail
            category = _semantic_category(sd.attr_category, atom.atom_type)
            evidence = _effective_evidence_count(sd.evidence_counter, sd.evidence_list)
            subject_matches = not sd.subject_key or sd.subject_key == profile.profile_id
            name = sd.attr_name.strip()
            value = sd.attr_value.strip()
            if category and name and value and evidence >= 1 and subject_matches:
                section = _profile_section(category, name)
                field_sources = profile.stats.setdefault(
                    "_profile_field_sources",
                    {"preferences": {}, "interests": {}, "facts": {}, "traits": {}},
                )
                section_sources = field_sources.setdefault(section, {})
                previous_source = section_sources.get(name)
                if not isinstance(previous_source, dict):
                    previous_source = self._find_existing_field_source(profile, section, name)
                    if previous_source is not None:
                        section_sources[name] = previous_source
                candidate = {
                    "atom_id": atom.atom_id,
                    "weight": atom.weight,
                    "confidence": atom.confidence,
                    "evidence_counter": evidence,
                    "created_at": atom.created_at,
                    "category": category,
                    "value": value,
                }
                if not isinstance(previous_source, dict) or _semantic_rank(candidate) >= _semantic_rank(
                    previous_source
                ):
                    section_sources[name] = candidate
                    if section == "preferences":
                        previous_value = str(previous_source.get("value", "")) if previous_source else ""
                        previous_interest = _preference_interest(name, previous_value) if previous_value else ""
                        profile.preferences[name] = value
                        interest = _preference_interest(name, value)
                        supported_interests = _supported_profile_interests(profile.preferences, field_sources)
                        if previous_interest not in supported_interests and previous_interest in profile.interests:
                            profile.interests.remove(previous_interest)
                        if interest and interest not in profile.interests:
                            profile.interests.append(interest)
                    elif section == "interests":
                        previous_value = str(previous_source.get("value", "")) if previous_source else ""
                        previous_interest = _preference_interest(name, previous_value) if previous_value else ""
                        interest = _preference_interest(name, value)
                        supported_interests = _supported_profile_interests(profile.preferences, field_sources)
                        if previous_interest not in supported_interests and previous_interest in profile.interests:
                            profile.interests.remove(previous_interest)
                        if interest and interest not in profile.interests:
                            profile.interests.append(interest)
                    elif section == "traits":
                        previous_value = str(previous_source.get("value", "")) if previous_source else ""
                        if previous_value and previous_value != value:
                            previous_confidence = _trait_confidence_from_sources(field_sources, previous_value)
                            if previous_confidence is None:
                                profile.traits.pop(previous_value, None)
                            else:
                                profile.traits[previous_value] = previous_confidence
                        if profile.facts.get(name) in {previous_value, value}:
                            profile.facts.pop(name, None)
                        trait_confidence = _trait_confidence_from_sources(field_sources, value)
                        if trait_confidence is not None:
                            profile.traits[value] = trait_confidence
                    else:
                        profile.facts[name] = value
                    updated = True

        # 2. 处理感官/情绪数据 → mood_history（任意原子类型）
        if atom.episodic_detail is not None and (
            atom.episodic_detail.sensory_tags or atom.episodic_detail.emotion_tags
        ):
            detail = atom.episodic_detail
            profile.mood_history.append(
                {
                    "timestamp": datetime.now().isoformat(),
                    "sensory_tags": list(detail.sensory_tags),
                    "emotion_tags": list(detail.emotion_tags),
                    "temporal_context": detail.temporal_context,
                    "content": atom.content[:120],
                }
            )
            if len(profile.mood_history) > 200:
                profile.mood_history = profile.mood_history[-200:]
            updated = True

        if not updated:
            return None

        # 更新印象和时间戳
        profile.impression = self._build_impression(profile)
        profile.updated_at = datetime.now()
        profile.last_extracted_at = datetime.now()

        self.profile_store.save_profile(profile)

        logger.debug(
            "增量更新用户画像",
            profile_id=profile.profile_id,
        )

        return profile

    def _find_existing_field_source(
        self,
        profile: UserProfile,
        section: str,
        name: str,
    ) -> Optional[dict[str, Any]]:
        """从历史语义证据恢复旧画像字段的排序来源。"""
        try:
            candidates = []
            for item in self._load_semantic_data(profile):
                category = _semantic_category(item.get("attr_category", ""), item.get("atom_type"))
                item_section = _profile_section(category, str(item.get("attr_name", "") or "").strip())
                if item_section != section or str(item.get("attr_name", "")).strip() != name:
                    continue
                if int(item.get("evidence_counter", 0) or 0) < 1:
                    continue
                candidates.append(
                    {
                        "atom_id": item.get("atom_id", ""),
                        "weight": float(item.get("weight", 0.0) or 0.0),
                        "confidence": float(item.get("confidence", 0.0) or 0.0),
                        "evidence_counter": int(item.get("evidence_counter", 0) or 0),
                        "created_at": item.get("created_at", 0.0),
                        "category": category,
                        "value": str(item.get("attr_value", "") or "").strip(),
                    }
                )
            return max(candidates, key=_semantic_rank) if candidates else None
        except (TypeError, ValueError):
            return None

    # ── 内部: 数据聚合 ───────────────────────────────────────

    def _aggregate_into_profile(
        self,
        profile: UserProfile,
        semantic_data: list[dict[str, Any]],
    ) -> None:
        """将语义数据聚合到 UserProfile 各字段中

        Args:
            profile: 目标 UserProfile
            semantic_data: 语义数据列表（含 attr_category/name/value/evidence_counter）
        """
        winners: dict[tuple[str, str], dict[str, Any]] = {}
        for item in semantic_data:
            category = _semantic_category(item.get("attr_category", ""), item.get("atom_type"))
            name = str(item.get("attr_name", "") or "").strip()
            value = str(item.get("attr_value", "") or "").strip()
            evidence = int(item.get("evidence_counter", 0) or 0)
            if not category or not name or not value or evidence < 1:
                continue

            section = _profile_section(category, name)
            key = (section, name)
            rank = _semantic_rank(item)
            previous = winners.get(key)
            if previous is None or rank > previous["_rank"]:
                winners[key] = {**item, "_category": category, "_rank": rank}

        field_sources: dict[str, dict[str, Any]] = {
            "preferences": {},
            "interests": {},
            "facts": {},
            "traits": {},
        }
        for (section, name), item in sorted(winners.items(), key=lambda entry: entry[1]["_rank"], reverse=True):
            value = str(item["attr_value"]).strip()
            category = item["_category"]
            confidence = float(item.get("confidence", 0.5) or 0.5)
            source = {
                "atom_id": item.get("atom_id", ""),
                "weight": float(item.get("weight", 0.0) or 0.0),
                "confidence": confidence,
                "evidence_counter": int(item.get("evidence_counter", 0) or 0),
                "created_at": item.get("created_at", 0.0),
                "category": category,
                "value": value,
            }

            if section == "preferences":
                profile.preferences[name] = value
                field_sources["preferences"][name] = source
                interest = _preference_interest(name, value)
                if interest and interest not in profile.interests:
                    profile.interests.append(interest)
                continue

            if section == "interests":
                field_sources["interests"][name] = source
                interest = _preference_interest(name, value)
                if interest and interest not in profile.interests:
                    profile.interests.append(interest)
                continue

            if section == "traits":
                profile.traits[value] = max(profile.traits.get(value, 0.0), confidence)
                field_sources["traits"][name] = source
                continue

            profile.facts[name] = value
            field_sources["facts"][name] = source

        profile.stats["_profile_field_sources"] = field_sources

    def _build_impression(self, profile: UserProfile) -> str:
        """从画像数据构建自然语言印象

        Args:
            profile: UserProfile

        Returns:
            印象字符串
        """
        parts = []

        if profile.traits:
            top_traits = sorted(profile.traits.items(), key=lambda x: -x[1])[:3]
            trait_str = "、".join(t[0] for t in top_traits)
            parts.append(f"性格特征：{trait_str}")

        if profile.interests:
            interest_str = "、".join(profile.interests[:5])
            parts.append(f"兴趣：{interest_str}")

        if profile.preferences:
            pref_items = [f"{k}={v}" for k, v in list(profile.preferences.items())[:5]]
            parts.append(f"偏好：{'；'.join(pref_items)}")

        if profile.facts:
            fact_items = [f"{k}={v}" for k, v in list(profile.facts.items())[:5]]
            parts.append(f"属性：{'；'.join(fact_items)}")

        msg_count = profile.stats.get("message_count", 0) if profile.stats else 0
        if msg_count:
            parts.append(f"活跃度：{msg_count}条消息")

        return "；".join(parts) if parts else f"用户 {profile.user_id} 的画像正在收集中"


# ---------------------------------------------------------------------------
# ProfileRetriever — LLM 上下文格式化
# ---------------------------------------------------------------------------


class ProfileRetriever:
    """用户画像检索器 — 将 UserProfile 格式化为 LLM 可用的上下文文本

    用于在回复生成时注入用户画像信息，帮助 LLM 做出个性化响应。
    """

    def __init__(self, profile_store: ProfileStore):
        self.profile_store = profile_store

    def get_profile_context(self, user_id: str, max_chars: int = 800, platform: Optional[str] = None) -> str:
        """获取格式化的用户画像上下文（用于 LLM prompt）

        格式:
            【用户画像 - {user_id}】
            印象: {impression}
            特征: {traits}
            偏好: {preferences}
            ...

        Args:
            user_id: 用户 ID
            max_chars: 最大字符数

        Returns:
            格式化文本，画像不存在时返回空字符串
        """
        profile = (
            self.profile_store.get_profile(user_id, platform=platform)
            if platform
            else self.profile_store.get_profile(user_id)
        )
        if profile is None or profile.person_type != "person" or profile.verification_status != "verified":
            return ""

        display_id = profile.profile_id
        lines = [f"【用户画像 - {display_id}】"]

        if profile.expression_style:
            lines.append(f"表达风格: {profile.expression_style}")
        favorite_expressions = profile.expression_patterns.get("favorite_expressions", [])
        if favorite_expressions:
            lines.append(f"常用表达: {'、'.join(str(item) for item in favorite_expressions[:4])}")

        if profile.impression:
            lines.append(f"印象: {profile.impression}")

        if profile.traits:
            traits_str = "、".join(f"{k}({v:.1%})" for k, v in sorted(profile.traits.items(), key=lambda x: -x[1]))
            lines.append(f"特征: {traits_str}")

        if profile.preferences:
            pref_str = "；".join(f"{k}: {v}" for k, v in profile.preferences.items())
            lines.append(f"偏好: {pref_str}")

        if profile.facts:
            fact_str = "；".join(f"{k}: {v}" for k, v in profile.facts.items())
            lines.append(f"事实: {fact_str}")

        if profile.interests:
            lines.append(f"兴趣: {'、'.join(profile.interests)}")

        public_stats = {k: v for k, v in profile.stats.items() if not str(k).startswith("_")} if profile.stats else {}
        if public_stats:
            stats_str = "；".join(f"{k}: {v}" for k, v in public_stats.items())
            lines.append(f"统计: {stats_str}")

        result = "\n".join(lines)

        if len(result) > max_chars:
            result = result[:max_chars].rsplit("\n", 1)[0]

        return result

    def get_profile_summary(self, user_id: str, max_chars: int = 200, platform: Optional[str] = None) -> str:
        """获取精炼的用户画像摘要（用于密集 prompt）

        Args:
            user_id: 用户 ID
            max_chars: 最大字符数

        Returns:
            摘要文本，画像不存在时返回空字符串
        """
        profile = (
            self.profile_store.get_profile(user_id, platform=platform)
            if platform
            else self.profile_store.get_profile(user_id)
        )
        if profile is None or profile.person_type != "person" or profile.verification_status != "verified":
            return ""

        summary = f"【{profile.profile_id}】"
        parts: list[str] = []
        favorite_expressions = profile.expression_patterns.get("favorite_expressions", [])
        expression_suffix = ""
        if profile.expression_style:
            expression_suffix += " 表达:" + profile.expression_style
        if favorite_expressions:
            expression_suffix += " 常用:" + "、".join(str(item) for item in favorite_expressions[:2])

        if profile.impression:
            remaining = max_chars - len(summary) - len(expression_suffix) - 3
            if remaining > 0:
                summary += profile.impression[:remaining]
        else:
            if profile.traits:
                top = sorted(profile.traits.items(), key=lambda x: -x[1])[:2]
                parts.append("特征:" + "、".join(t[0] for t in top))
            if profile.interests:
                parts.append("兴趣:" + "、".join(profile.interests[:3]))
            if profile.preferences:
                pref_sample = list(profile.preferences.keys())[:3]
                parts.append("偏好:" + "、".join(pref_sample))
            summary += " ".join(parts) if parts else "暂无画像数据"

        summary += expression_suffix

        if len(summary) > max_chars:
            summary = summary[: max_chars - 3] + "..."

        return summary


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _safe_json_loads(text: Optional[str], default_type: type) -> Any:
    """安全解析 JSON 字符串，失败返回空容器

    Args:
        text: JSON 字符串或 None
        default_type: 失败时返回的类型（dict / list）

    Returns:
        解析后的数据或空容器
    """
    if not text:
        return default_type()
    try:
        value = json.loads(text)
        return value if isinstance(value, default_type) else default_type()
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"JSON 解析失败，返回空 {default_type.__name__}")
        return default_type()


def _entities_contain_user(entities_raw: Any, user_id: str) -> bool:
    """检查记忆原子的 entities 字段是否包含指定用户

    Args:
        entities_raw: entities 原始值（JSON 字符串或 None）
        user_id: 目标用户 ID

    Returns:
        是否包含
    """
    if not entities_raw:
        return False
    if isinstance(entities_raw, str):
        try:
            entities = json.loads(entities_raw)
        except (json.JSONDecodeError, TypeError):
            return user_id in entities_raw
    elif isinstance(entities_raw, list):
        entities = entities_raw
    else:
        return False
    return user_id in entities


def _effective_evidence_count(counter: Any, evidence_raw: Any) -> int:
    """兼容历史计数器为 0 但已持久化了真实证据列表的记录。"""
    try:
        count = max(0, int(counter or 0))
    except (TypeError, ValueError):
        count = 0

    evidence = evidence_raw
    if isinstance(evidence_raw, str):
        try:
            evidence = json.loads(evidence_raw)
        except (json.JSONDecodeError, TypeError):
            evidence = []
    if isinstance(evidence, list):
        distinct_evidence = {str(item).strip() for item in evidence if str(item).strip()}
        count = max(count, len(distinct_evidence))
    return count


def _semantic_category(category: Any, atom_type: Any = None) -> str:
    """将提示词协议和历史别名收敛为画像聚合类别。"""
    normalized = str(category or "").strip().casefold()
    if normalized and (normalized in _PERSONALITY_CATEGORIES or _matches_keywords(normalized, _PERSONALITY_CATEGORIES)):
        return "personality"
    if normalized and (normalized in _HABIT_CATEGORIES or _matches_keywords(normalized, _HABIT_CATEGORIES)):
        return "habit"
    if normalized and (normalized in _SKILL_CATEGORIES or _matches_keywords(normalized, _SKILL_CATEGORIES)):
        return "skill"
    if normalized and (normalized in _INTEREST_CATEGORIES or _matches_keywords(normalized, _INTEREST_CATEGORIES)):
        return "interest"
    if normalized and _matches_keywords(normalized, _PREFERENCE_KEYWORDS):
        return "preference"
    if normalized and _matches_keywords(normalized, _FACT_KEYWORDS):
        return "fact"

    normalized_atom_type = atom_type.value if isinstance(atom_type, AtomType) else str(atom_type or "").casefold()
    if normalized_atom_type == AtomType.PREFERENCE.value:
        return "preference"
    if normalized_atom_type == AtomType.FACTUAL.value:
        return "fact"
    return ""


def _profile_section(category: str, name: str) -> str:
    """将语义类别映射到互斥的公开画像维度。"""
    if category == "preference":
        return "preferences"
    if category == "interest":
        return "interests"
    if category == "personality" or name.casefold() in _PERSONALITY_CATEGORIES:
        return "traits"
    return "facts"


def _preference_interest(name: str, value: str) -> str:
    """偏好协议中 value 若是极性，兴趣对象应取 name，而不是“喜欢”本身。"""
    normalized_value = value.strip().casefold()
    if normalized_value in _POSITIVE_PREFERENCE_VALUES:
        return name.strip()
    if normalized_value in _NON_INTEREST_PREFERENCE_VALUES:
        return ""
    for prefix in ("喜欢", "偏好", "爱好", "喜欢的"):
        if value.strip().startswith(prefix) and value.strip()[len(prefix) :].strip():
            return value.strip()[len(prefix) :].strip()
    return value.strip()


def _supported_profile_interests(
    preferences: dict[str, str],
    field_sources: dict[str, dict[str, Any]],
) -> set[str]:
    """返回仍由偏好或显式兴趣证据支持的兴趣值。"""
    supported = {
        interest for name, value in preferences.items() if (interest := _preference_interest(str(name), str(value)))
    }
    interest_sources = field_sources.get("interests", {})
    if isinstance(interest_sources, dict):
        for name, source in interest_sources.items():
            if not isinstance(source, dict):
                continue
            interest = _preference_interest(str(name), str(source.get("value", "")))
            if interest:
                supported.add(interest)
    return supported


def _trait_confidence_from_sources(field_sources: dict[str, dict[str, Any]], value: str) -> Optional[float]:
    """汇总仍指向同一特征值的来源置信度。"""
    trait_sources = field_sources.get("traits", {})
    if not isinstance(trait_sources, dict):
        return None

    confidences = []
    for source in trait_sources.values():
        if not isinstance(source, dict) or str(source.get("value", "")) != value:
            continue
        try:
            confidences.append(float(source.get("confidence", 0.5) or 0.5))
        except (TypeError, ValueError):
            confidences.append(0.5)
    return max(confidences) if confidences else None


def _semantic_rank(item: dict[str, Any]) -> tuple[float, float, int, float, str]:
    """冲突值决胜顺序：权重、置信度、证据数、时间、原子 ID。"""
    created_at = item.get("created_at")
    if isinstance(created_at, datetime):
        created_rank = created_at.timestamp()
    elif isinstance(created_at, (int, float)):
        created_rank = float(created_at)
    elif isinstance(created_at, str):
        try:
            created_rank = datetime.fromisoformat(created_at).timestamp()
        except ValueError:
            created_rank = 0.0
    else:
        created_rank = 0.0
    return (
        float(item.get("weight", 0.0) or 0.0),
        float(item.get("confidence", 0.0) or 0.0),
        int(item.get("evidence_counter", 0) or 0),
        created_rank,
        str(item.get("atom_id", "") or ""),
    )


def _matches_keywords(text: str, keywords: set[str]) -> bool:
    """检查文本是否包含任一关键词

    Args:
        text: 待检文本（已小写化）
        keywords: 关键词集合

    Returns:
        是否匹配
    """
    return any(kw in text for kw in keywords)
