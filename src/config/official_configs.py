import re

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal, Optional
import time

from src.config.config_base import ConfigBase

"""
须知：
1. 本文件中记录了所有的配置项
2. 所有新增的class都需要继承自ConfigBase
3. 所有新增的class都应在config.py中的Config类中添加字段
4. 对于新增的字段，若为可选项，则应在其后添加field()并设置default_factory或default
"""


@lru_cache(maxsize=2048)
def _parse_stream_config_to_chat_id(stream_config_str: str) -> Optional[str]:
    """复用聊天流的纯 ID 生成入口，避免配置解析时创建聊天管理器和数据库。"""
    if not isinstance(stream_config_str, str):
        return None
    parts = stream_config_str.split(":")
    if len(parts) != 3:
        return None
    platform, account, stream_type = parts
    if not platform or not account or stream_type not in {"group", "private"}:
        return None
    from src.chat.message_receive.chat_stream import ChatManager

    try:
        return ChatManager.get_stream_id(platform, account, is_group=stream_type == "group")
    except ValueError:
        return None


def _valid_learning_rule(config_item: object, width: int) -> bool:
    """旧配置允许未知条目；畸形学习规则沿用忽略并回退到后续规则的语义。"""
    return (
        isinstance(config_item, (list, tuple))
        and len(config_item) >= width
        and all(isinstance(value, str) for value in config_item[:width])
        and all(value.lower() in {"enable", "disable"} for value in config_item[1:width])
    )


@dataclass
class BotConfig(ConfigBase):
    """QQ机器人配置类"""

    platform: str
    """平台"""

    qq_account: str
    """QQ账号"""

    nickname: str
    """昵称"""

    platforms: list[str] = field(default_factory=list)
    """其他平台列表"""

    alias_names: list[str] = field(default_factory=lambda: ["Riya", "小璃"])
    """别名列表"""


@dataclass
class PersonalityConfig(ConfigBase):
    """人格配置类"""

    personality: str
    """人格"""

    reply_style: str = "请不要刻意突出自身学科背景。可以参考贴吧，知乎和微博的回复风格。"
    """默认表达风格"""


@dataclass
class RelationshipConfig(ConfigBase):
    """关系配置类"""

    enable_relationship: bool = True
    """是否启用关系系统"""


@dataclass
class ChatConfig(ConfigBase):
    """聊天配置类"""

    group_message_buffer_seconds: float = 3.0
    """群聊首条消息进入 TurnGate 后的固定等待时长，0 为关闭"""

    private_message_buffer_seconds: float = 1.5
    """私聊首条消息进入 TurnGate 后的固定等待时长，0 为关闭"""

    max_context_size: int = 30
    """上下文长度"""

    mentioned_bot_reply: bool = True
    """是否启用提及必回复"""

    at_bot_inevitable_reply: float = 1
    """@bot 必然回复，1为100%回复，0为不额外增幅"""

    planner_smooth: float = 3
    """规划器平滑，增大数值会减小planner负荷，略微降低反应速度，推荐2-5，0为关闭，必须大于等于0"""

    talk_value: float = 1
    """思考频率"""

    enable_talk_value_rules: bool = True
    """是否启用动态发言频率规则"""

    talk_value_rules: list[dict] = field(
        default_factory=lambda: [
            {"target": "", "time": "00:00-08:59", "value": 0.8},
            {"target": "", "time": "09:00-22:59", "value": 1.0},
            {"target": "qq:1919810:group", "time": "20:00-23:59", "value": 0.6},
            {"target": "qq:114514:private", "time": "00:00-23:59", "value": 0.3},
        ]
    )
    """
    思考频率规则列表，支持按聊天流/按日内时段配置。
    规则格式：{ target="platform:id:type" 或 "", time="HH:MM-HH:MM", value=0.5 }

    示例:
    [
        ["", "00:00-08:59", 0.2],                 # 全局规则：凌晨到早上更安静
        ["", "09:00-22:59", 1.0],                 # 全局规则：白天正常
        ["qq:1919810:group", "20:00-23:59", 0.6], # 指定群在晚高峰降低发言
        ["qq:114514:private", "00:00-23:59", 0.3],# 指定私聊全时段较安静
    ]

    匹配优先级: 先匹配指定 chat 流规则，再匹配全局规则(\"\"). 
    时间区间支持跨夜，例如 "23:00-02:00"。
    """

    plan_reply_log_max_per_chat: int = 1024
    """每个聊天流最大保存的Plan/Reply日志数量，超过此数量时会自动删除最老的日志"""

    llm_quote: bool = False
    """是否在 reply action 中启用 quote 参数，启用后 LLM 可以控制是否引用消息"""

    _parse_stream_config_to_chat_id = staticmethod(_parse_stream_config_to_chat_id)

    def _now_minutes(self) -> int:
        """返回本地时间的分钟数(0-1439)。"""
        lt = time.localtime()
        return lt.tm_hour * 60 + lt.tm_min

    def _parse_range(self, range_str: str) -> Optional[tuple[int, int]]:
        """解析 "HH:MM-HH:MM" 到 (start_min, end_min)。"""
        try:
            start_str, end_str = [s.strip() for s in range_str.split("-")]
            sh, sm = [int(x) for x in start_str.split(":")]
            eh, em = [int(x) for x in end_str.split(":")]
            return sh * 60 + sm, eh * 60 + em
        except Exception:
            return None

    def _in_range(self, now_min: int, start_min: int, end_min: int) -> bool:
        """
        判断 now_min 是否在 [start_min, end_min] 区间内。
        支持跨夜：如果 start > end，则表示跨越午夜。
        """
        if start_min <= end_min:
            return start_min <= now_min <= end_min
        # 跨夜：例如 23:00-02:00
        return now_min >= start_min or now_min <= end_min

    def get_talk_value(self, chat_id: Optional[str]) -> float:
        """根据规则返回当前 chat 的动态 talk_value，未匹配则回退到基础值。"""
        if not self.enable_talk_value_rules or not self.talk_value_rules:
            result = self.talk_value
            # 防止返回0值，自动转换为0.0000001
            if result == 0:
                return 0.0000001
            return result

        now_min = self._now_minutes()

        # 1) 先尝试匹配指定 chat 的规则
        if chat_id:
            for rule in self.talk_value_rules:
                if not isinstance(rule, dict):
                    continue
                target = rule.get("target", "")
                time_range = rule.get("time", "")
                value = rule.get("value", None)
                if not isinstance(time_range, str):
                    continue
                # 跳过全局
                if target == "":
                    continue
                config_chat_id = self._parse_stream_config_to_chat_id(str(target))
                if config_chat_id is None or config_chat_id != chat_id:
                    continue
                parsed = self._parse_range(time_range)
                if not parsed:
                    continue
                start_min, end_min = parsed
                if self._in_range(now_min, start_min, end_min):
                    try:
                        result = float(value)
                        # 防止返回0值，自动转换为0.0000001
                        if result == 0:
                            return 0.0000001
                        return result
                    except Exception:
                        continue

        # 2) 再匹配全局规则("")
        for rule in self.talk_value_rules:
            if not isinstance(rule, dict):
                continue
            target = rule.get("target", None)
            time_range = rule.get("time", "")
            value = rule.get("value", None)
            if target != "" or not isinstance(time_range, str):
                continue
            parsed = self._parse_range(time_range)
            if not parsed:
                continue
            start_min, end_min = parsed
            if self._in_range(now_min, start_min, end_min):
                try:
                    result = float(value)
                    # 防止返回0值，自动转换为0.0000001
                    if result == 0:
                        return 0.0000001
                    return result
                except Exception:
                    continue

        # 3) 未命中规则返回基础值
        result = self.talk_value
        # 防止返回0值，自动转换为0.0000001
        if result == 0:
            return 0.0000001
        return result


@dataclass
class MessageReceiveConfig(ConfigBase):
    """消息接收配置类"""

    ban_words: set[str] = field(default_factory=lambda: set())
    """过滤词列表"""

    ban_msgs_regex: set[str] = field(default_factory=lambda: set())
    """过滤正则表达式列表"""


@dataclass
class MemoryConfig(ConfigBase):
    """记忆配置类"""

    max_agent_iterations: int = 5
    """Agent最多迭代轮数（最低为1）"""

    agent_timeout_seconds: float = 180.0
    """Agent超时时间（秒）"""

    global_memory: bool = False
    """是否允许记忆检索在聊天记录中进行全局查询（忽略当前chat_id，仅对 search_chat_history 等工具生效）"""

    global_memory_blacklist: list[str] = field(default_factory=lambda: [])
    """
    全局记忆黑名单，当启用全局记忆时，不将特定聊天流纳入检索
    格式: ["platform:id:type", ...]
    
    示例:
    [
        "qq:1919810:private",  # 排除特定私聊
        "qq:114514:group",     # 排除特定群聊
    ]
    
    说明:
    - 当启用全局记忆时，黑名单中的聊天流不会被检索
    - 当在黑名单中的聊天流进行查询时，仅使用该聊天流的本地记忆
    """

    planner_question: bool = True
    """
    是否使用 Planner 提供的 question 作为记忆检索问题
    - True: 当 Planner 在 reply 动作中提供了 question 时，直接使用该问题进行记忆检索，跳过 LLM 生成问题的步骤
    - False: 沿用旧模式，使用 LLM 生成问题
    """

    # ------------------------------------------------------------------
    # 存储配置（可通过 bot_config.toml [memory] 段覆盖 MemoryStoreConfig 默认值）
    # ------------------------------------------------------------------

    sqlite_path: str = "data/memory.db"
    """SQLite 数据库文件路径"""

    qdrant_url: str = ""
    """Qdrant 服务器 URL，为空字符串时使用本地嵌入模式"""

    qdrant_api_key: Optional[str] = field(default="", repr=False)
    """Qdrant API 密钥（可选，空字符串等价于 None）"""

    qdrant_local_path: str = "data/qdrant"
    """Qdrant 本地模式数据目录"""

    embedding_dimension: int = 1024
    """嵌入向量维度，应与 LLM 的 embedding 模型输出维度一致"""

    collection_name_atoms: str = "memory_atoms"
    """记忆原子 Qdrant 集合名称"""

    collection_name_graph: str = "graph_entries"
    """图条目 Qdrant 集合名称"""

    vector_batch_size: int = 100
    """向量批量写入大小"""

    def __post_init__(self):
        """验证配置值"""
        if self.max_agent_iterations < 1:
            raise ValueError(f"max_agent_iterations 必须至少为1，当前值: {self.max_agent_iterations}")
        if self.agent_timeout_seconds <= 0:
            raise ValueError(f"agent_timeout_seconds 必须大于0，当前值: {self.agent_timeout_seconds}")


@dataclass
class ExpressionConfig(ConfigBase):
    """表达配置类"""

    learning_list: list[list] = field(
        default_factory=lambda: [
            ["", "enable", "enable", "enable"],
            ["qq:1919810:group", "enable", "enable", "enable"],
            ["qq:114514:private", "enable", "disable", "disable"],
        ]
    )
    """
    表达学习配置列表，支持按聊天流配置
    格式: [["chat_stream_id", "use_expression", "enable_learning", "enable_jargon_learning"], ...]
    
    示例:
    [
        ["", "enable", "enable", "enable"],  # 全局配置：使用表达，启用学习，启用jargon学习
        ["qq:1919810:private", "enable", "enable", "enable"],  # 特定私聊配置：使用表达，启用学习，启用jargon学习
        ["qq:114514:private", "enable", "disable", "disable"],  # 特定私聊配置：使用表达，禁用学习，禁用jargon学习
    ]
    
    说明:
    - 第一位: chat_stream_id，空字符串表示全局配置
    - 第二位: 是否使用学到的表达 ("enable"/"disable")
    - 第三位: 是否学习表达 ("enable"/"disable") 
    - 第四位: 是否启用jargon学习 ("enable"/"disable")
    """

    expression_groups: list[list[str]] = field(
        default_factory=lambda: [["qq:1919810:private", "qq:114514:private", "qq:1111111:group"]]
    )
    """
    表达学习互通组
    格式: [["qq:12345:group", "qq:67890:private"]]
    """

    vector_selection_enabled: bool = True
    """是否使用向量召回筛选表达候选；关闭后使用传统选择并跳过相关 embedding 调用"""

    expression_self_reflect: bool = True
    """是否启用自动表达优化"""

    expression_manual_reflect: bool = False
    """是否启用手动表达优化"""

    manual_reflect_operator_id: str = ""
    """表达反思操作员ID"""

    allow_reflect: list[str] = field(default_factory=list)
    """
    允许进行表达反思的聊天流ID列表
    格式: ["qq:123456:private", "qq:654321:group", ...]
    只有在此列表中的聊天流才会提出问题并跟踪
    如果列表为空，则所有聊天流都可以进行表达反思（前提是 reflect = true）
    """

    all_global_jargon: bool = True
    """是否将所有新增的jargon项目默认为全局（is_global=True），chat_id记录第一次存储时的id。注意，此功能关闭后，已经记录的全局黑话不会改变，需要手动删除"""

    enable_jargon_explanation: bool = True
    """是否在回复前尝试对上下文中的黑话进行解释（关闭可减少一次LLM调用，仅影响回复前的黑话匹配与解释，不影响黑话学习）"""

    jargon_mode: Literal["context", "planner"] = "planner"
    """
    黑话解释来源模式：
    - "context": 使用上下文自动匹配黑话并解释（原有模式）
    - "planner": 仅使用 Planner 在 reply 动作中给出的 unknown_words 列表进行黑话检索
    """

    expression_checked_only: bool = True
    """
    是否仅选择已检查且未拒绝的表达方式
    当设置为 true 时，只有 checked=True 且 rejected=False 的表达方式才会被选择
    当设置为 false 时，保留旧的筛选原则（仅排除 rejected=True 的表达方式）
    """

    expression_auto_check_interval: int = 600
    """
    表达方式自动检查的间隔时间（单位：秒）
    默认值：600秒（10分钟）
    """

    expression_auto_check_count: int = 20
    """
    每次自动检查时随机选取的表达方式数量
    默认值：20条
    """

    expression_auto_check_custom_criteria: list[str] = field(default_factory=list)
    """
    表达方式自动检查的额外自定义评估标准
    格式: ["标准1", "标准2", "标准3", ...]
    这些标准会被添加到评估提示词中，作为额外的评估要求
    默认值：空列表
    """

    _parse_stream_config_to_chat_id = staticmethod(_parse_stream_config_to_chat_id)

    def get_expression_config_for_chat(self, chat_stream_id: Optional[str] = None) -> tuple[bool, bool, bool]:
        """
        根据聊天流ID获取表达配置

        Args:
            chat_stream_id: 聊天流ID，格式为哈希值

        Returns:
            tuple: (是否使用表达, 是否学习表达, 是否启用jargon学习)
        """
        if not self.learning_list:
            # 如果没有配置，使用默认值：启用表达，启用学习，启用jargon学习
            return True, True, True

        # 优先检查聊天流特定的配置
        if chat_stream_id:
            specific_expression_config = self._get_stream_specific_config(chat_stream_id)
            if specific_expression_config is not None:
                return specific_expression_config

        # 检查全局配置（第一个元素为空字符串的配置）
        global_expression_config = self._get_global_config()
        if global_expression_config is not None:
            return global_expression_config

        # 如果都没有匹配，返回默认值：启用表达，启用学习，启用jargon学习
        return True, True, True

    def _get_stream_specific_config(self, chat_stream_id: str) -> Optional[tuple[bool, bool, bool]]:
        """
        获取特定聊天流的表达配置

        Args:
            chat_stream_id: 聊天流ID（哈希值）

        Returns:
            tuple: (是否使用表达, 是否学习表达, 是否启用jargon学习)，如果没有配置则返回 None
        """
        for config_item in self.learning_list:
            if not _valid_learning_rule(config_item, 4):
                continue

            stream_config_str = config_item[0]  # 例如 "qq:1026294844:group"

            # 如果是空字符串，跳过（这是全局配置）
            if stream_config_str == "":
                continue

            # 解析配置字符串并生成对应的 chat_id
            config_chat_id = self._parse_stream_config_to_chat_id(stream_config_str)
            if config_chat_id is None:
                continue

            # 比较生成的 chat_id
            if config_chat_id != chat_stream_id:
                continue

            # 解析配置
            try:
                use_expression: bool = config_item[1].lower() == "enable"
                enable_learning: bool = config_item[2].lower() == "enable"
                enable_jargon_learning: bool = config_item[3].lower() == "enable"
                return use_expression, enable_learning, enable_jargon_learning  # type: ignore
            except (ValueError, IndexError):
                continue

        return None

    def _get_global_config(self) -> Optional[tuple[bool, bool, bool]]:
        """
        获取全局表达配置

        Returns:
            tuple: (是否使用表达, 是否学习表达, 是否启用jargon学习)，如果没有配置则返回 None
        """
        for config_item in self.learning_list:
            if not _valid_learning_rule(config_item, 4):
                continue

            # 检查是否为全局配置（第一个元素为空字符串）
            if config_item[0] == "":
                try:
                    use_expression: bool = config_item[1].lower() == "enable"
                    enable_learning: bool = config_item[2].lower() == "enable"
                    enable_jargon_learning: bool = config_item[3].lower() == "enable"
                    return use_expression, enable_learning, enable_jargon_learning  # type: ignore
                except (ValueError, IndexError):
                    continue

        return None


@dataclass
class BehaviorConfig(ConfigBase):
    """行为学习配置类"""

    learning_list: list[list] = field(
        default_factory=lambda: [
            ["", "enable", "enable"],
            ["qq:1919810:group", "enable", "enable"],
            ["qq:114514:private", "enable", "disable"],
        ]
    )
    """
    行为学习配置列表，支持按聊天流配置
    格式: [["chat_stream_id", "use_behavior", "enable_learning"], ...]
    """

    behavior_groups: list[list[str]] = field(
        default_factory=lambda: [["qq:1919810:private", "qq:114514:private", "qq:1111111:group"]]
    )
    """
    行为学习互通组
    格式: [["qq:12345:group", "qq:67890:private"]]
    """

    _parse_stream_config_to_chat_id = staticmethod(_parse_stream_config_to_chat_id)

    def get_behavior_config_for_chat(self, chat_stream_id: Optional[str] = None) -> tuple[bool, bool]:
        """
        根据聊天流ID获取行为学习配置。

        Returns:
            tuple: (是否使用行为参考, 是否学习行为)
        """
        if not self.learning_list:
            return True, True

        if chat_stream_id:
            specific_config = self._get_stream_specific_config(chat_stream_id)
            if specific_config is not None:
                return specific_config

        global_config = self._get_global_config()
        if global_config is not None:
            return global_config

        return True, True

    def _get_stream_specific_config(self, chat_stream_id: str) -> Optional[tuple[bool, bool]]:
        for config_item in self.learning_list:
            if not _valid_learning_rule(config_item, 3):
                continue
            stream_config_str = config_item[0]
            if stream_config_str == "":
                continue
            config_chat_id = self._parse_stream_config_to_chat_id(stream_config_str)
            if config_chat_id != chat_stream_id:
                continue
            try:
                return config_item[1].lower() == "enable", config_item[2].lower() == "enable"  # type: ignore
            except (ValueError, IndexError):
                continue
        return None

    def _get_global_config(self) -> Optional[tuple[bool, bool]]:
        for config_item in self.learning_list:
            if not _valid_learning_rule(config_item, 3):
                continue
            if config_item[0] == "":
                try:
                    return config_item[1].lower() == "enable", config_item[2].lower() == "enable"  # type: ignore
                except (ValueError, IndexError):
                    continue
        return None


@dataclass
class ToolConfig(ConfigBase):
    """工具配置类"""

    enable_tool: bool = True
    """是否在聊天中启用工具"""


@dataclass
class VoiceConfig(ConfigBase):
    """语音识别配置类"""

    enable_asr: bool = False
    """是否启用语音识别"""


@dataclass
class EmojiConfig(ConfigBase):
    """表情包配置类"""

    emoji_chance: float = 0.4
    """发送表情包的基础概率"""

    max_reg_num: int = 100
    """表情包最大注册数量"""

    do_replace: bool = True
    """达到最大注册数量时替换旧表情包"""

    check_interval: int = 10
    """表情包检查间隔（分钟）"""

    steal_emoji: bool = True
    """是否偷取表情包，让璃夜可以发送她保存的这些表情包"""

    content_filtration: bool = False
    """是否开启表情包过滤"""

    filtration_prompt: str = "符合公序良俗"
    """表情包过滤要求"""

    vector_selection_enabled: bool = True
    """是否使用向量召回筛选表情候选；关闭后使用随机候选并跳过相关 embedding 调用"""

    usage_scene_enabled: bool = True
    """是否学习并使用真人发送表情包时的场景"""

    usage_scene_context_messages: int = 8
    """学习真人使用场景时读取的历史消息条数"""

    usage_scene_max_scenes: int = 8
    """单个表情包触发场景合并前的场景数量软上限"""

    usage_scene_weight: float = 0.6
    """候选初筛中真人场景相似度所占权重"""

    selection_candidate_count: int = 8
    """交给模型执行最终选择的候选表情包数量"""

    def __post_init__(self) -> None:
        if not 1 <= self.usage_scene_context_messages <= 32:
            raise ValueError("usage_scene_context_messages 必须在 1 到 32 之间")
        if not 1 <= self.usage_scene_max_scenes <= 32:
            raise ValueError("usage_scene_max_scenes 必须在 1 到 32 之间")
        if not 0.0 <= self.usage_scene_weight <= 1.0:
            raise ValueError("usage_scene_weight 必须在 0 到 1 之间")
        if not 1 <= self.selection_candidate_count <= 30:
            raise ValueError("selection_candidate_count 必须在 1 到 30 之间")


@dataclass
class KeywordRuleConfig(ConfigBase):
    """关键词规则配置类"""

    keywords: list[str] = field(default_factory=lambda: [])
    """关键词列表"""

    regex: list[str] = field(default_factory=lambda: [])
    """正则表达式列表"""

    reaction: str = ""
    """关键词触发的反应"""

    def __post_init__(self):
        """验证配置"""
        if not self.keywords and not self.regex:
            raise ValueError("关键词规则必须至少包含keywords或regex中的一个")

        if not self.reaction:
            raise ValueError("关键词规则必须包含reaction")

        # 验证正则表达式
        for pattern in self.regex:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"无效的正则表达式 '{pattern}': {str(e)}") from e


@dataclass
class KeywordReactionConfig(ConfigBase):
    """关键词配置类"""

    keyword_rules: list[KeywordRuleConfig] = field(
        default_factory=lambda: [
            KeywordRuleConfig(
                keywords=["人机", "bot", "机器", "入机", "robot", "机器人", "ai", "AI"],
                reaction="有人说你是人机或者机器人，否定这一事实，攻击或反问他。注意，只有认为你是机器人才回复，否则不要否认",
            ),
            KeywordRuleConfig(keywords=["测试关键词回复", "test"], reaction="回答测试成功"),
        ]
    )
    """关键词规则列表"""

    regex_rules: list[KeywordRuleConfig] = field(
        default_factory=lambda: [
            KeywordRuleConfig(
                regex=[r"^(?P<n>\S{1,20})是这样的$"],
                reaction=(
                    "请按照以下模板造句：[n]是这样的，xx只要xx就可以，可是[n]要考虑的事情就很多了，"
                    "比如什么时候xx，什么时候xx，什么时候xx。（请自由发挥替换xx部分，只需保持句式结构，"
                    "同时表达一种将[n]过度重视的反讽意味）"
                ),
            ),
            KeywordRuleConfig(
                regex=[r"(?:[^，。.\s]+，\s*)?我(?:也)?[没沒]要求你\s*(?P<action>.+?)[.。,，]?$"],
                reaction=(
                    "进行一次“八岐大蛇定型文”补完。暂时忽略回复风格，进入“粉絲吐槽”模式。请严格按照以下"
                    "“填空模板”，围绕“[action]”进行创作，使其充满滑稽的夸张感和天马行空的联想：我没要求你"
                    "[action]，我不是(与action关联的、宽容的角色)。可是，(与action相关的具体负面行为)是什么"
                    "意思？你的(与action相关的某种观念)怎么了？你才(与action相关的某个状态或时间点)吧？再这样"
                    "下去，你(一个中期的、等比级数式的滑稽推演)，(一个后期的、等比级数式的滑稽推演)，最后就"
                    "变成(一个与主题相关的、夸张的最终形态)了。作为(与最终形态相关的、克星或权威身份)，我可能"
                    "得(对你执行一个天罚般的行动)。真的。"
                ),
            ),
        ]
    )
    """正则表达式规则列表"""

    def __post_init__(self):
        """验证配置"""
        # 验证所有规则
        for rule in self.keyword_rules + self.regex_rules:
            if not isinstance(rule, KeywordRuleConfig):
                raise ValueError(f"规则必须是KeywordRuleConfig类型，而不是{type(rule).__name__}")


@dataclass
class ResponsePostProcessConfig(ConfigBase):
    """回复后处理配置类"""

    enable_response_post_process: bool = True
    """是否启用回复后处理，包括错别字生成器，回复分割器"""


@dataclass
class ChineseTypoConfig(ConfigBase):
    """中文错别字配置类"""

    enable: bool = True
    """是否启用中文错别字生成器"""

    error_rate: float = 0.01
    """单字替换概率"""

    min_freq: int = 9
    """最小字频阈值"""

    tone_error_rate: float = 0.1
    """声调错误概率"""

    word_replace_rate: float = 0.006
    """整词替换概率"""


@dataclass
class ResponseSplitterConfig(ConfigBase):
    """回复分割器配置类"""

    enable: bool = True
    """是否启用回复分割器"""

    max_length: int = 512
    """回复允许的最大长度"""

    max_sentence_num: int = 8
    """回复允许的最大句子数"""

    enable_kaomoji_protection: bool = False
    """是否启用颜文字保护"""

    enable_overflow_return_all: bool = False
    """是否在超出句子数量限制时合并后一次性返回"""


@dataclass
class TelemetryConfig(ConfigBase):
    """遥测配置类"""

    enable: bool = False
    """是否启用遥测"""


@dataclass
class LogConfig(ConfigBase):
    """日志配置类"""

    date_style: str = "m-d H:i:s"
    """日期格式"""

    log_level_style: Literal["FULL", "full", "compact", "lite"] = "compact"
    """日志级别样式"""

    color_text: Literal["none", "title", "full"] = "title"
    """日志文本颜色"""

    log_level: str = "INFO"
    """全局日志级别"""

    console_log_level: str = "INFO"
    """控制台日志级别"""

    file_log_level: str = "DEBUG"
    """文件日志级别"""

    debug_plaintext_logging: bool = False
    """DEBUG级别是否允许记录截断后的明文文本"""

    max_debug_text_length: int = 500
    """DEBUG明文字段最大长度"""

    max_log_field_length: int = 2000
    """普通日志字段最大长度"""

    suppress_libraries: list[str] = field(
        default_factory=lambda: [
            "faiss",
            "httpx",
            "urllib3",
            "asyncio",
            "websockets",
            "httpcore",
            "requests",
            "peewee",
            "openai",
            "uvicorn",
            "jieba",
        ]
    )
    """完全屏蔽的第三方库日志列表"""

    library_log_levels: dict[str, str] = field(default_factory=lambda: {"aiohttp": "WARNING"})
    """特定第三方库日志级别"""


@dataclass
class WebUIConfig(ConfigBase):
    """WebUI配置类

    注意: host 和 port 配置已移至环境变量 WEBUI_HOST 和 WEBUI_PORT
    """

    enabled: bool = True
    """是否启用WebUI"""

    mode: Literal["development", "production"] = "production"
    """运行模式：development(开发) 或 production(生产)"""

    anti_crawler_mode: Literal["false", "strict", "loose", "basic"] = "loose"
    """防爬虫模式：false(禁用) / strict(严格) / loose(宽松) / basic(基础-只记录不阻止)"""

    allowed_ips: str = "127.0.0.1"
    """IP白名单（逗号分隔，支持精确IP、CIDR格式和通配符）"""

    trusted_proxies: str = ""
    """信任的代理IP列表（逗号分隔），只有来自这些IP的X-Forwarded-For才被信任"""

    trust_xff: bool = False
    """是否启用X-Forwarded-For代理解析（默认false）"""

    secure_cookie: bool = False
    """是否启用安全Cookie（仅通过HTTPS传输，默认false）"""

    plugin_registry_url: str = "https://raw.githubusercontent.com/hsd221/RiyaBot-Plugins-Registry/main/registry.json"
    """RiyaBot 官方插件 Registry 地址；市场请求不会接受客户端覆盖"""


@dataclass
class DebugConfig(ConfigBase):
    """调试配置类"""

    show_prompt: bool = False
    """是否显示prompt"""

    show_replyer_prompt: bool = False
    """是否显示回复器prompt"""

    show_replyer_reasoning: bool = False
    """是否显示回复器推理"""

    show_jargon_prompt: bool = False
    """是否显示jargon相关提示词"""

    show_memory_prompt: bool = False
    """是否显示记忆检索相关prompt"""

    show_planner_prompt: bool = False
    """是否显示planner相关提示词"""

    show_lpmm_paragraph: bool = False
    """是否显示lpmm找到的相关文段日志"""


@dataclass
class ExperimentalConfig(ConfigBase):
    """实验功能配置类"""

    chat_prompts: list[str] = field(default_factory=lambda: [])
    """
    为指定聊天添加额外的prompt配置列表
    格式: ["platform:id:type:prompt内容", ...]
    
    示例:
    [
        "qq:114514:group:这是一个摄影群，你精通摄影知识",
        "qq:19198:group:这是一个二次元交流群",
        "qq:114514:private:这是你与好朋友的私聊"
    ]
    
    说明:
    - platform: 平台名称，如 "qq"
    - id: 群ID或用户ID
    - type: "group" 或 "private"
    - prompt内容: 要添加的额外prompt文本
    """


@dataclass
class UpdateConfig(ConfigBase):
    """程序更新配置。"""

    channel: Literal["stable", "dev"] = "stable"
    """跟踪频道：stable 检查正式版本标签，dev 检查开发分支提交。"""


@dataclass
class MaimMessageConfig(ConfigBase):
    """maim_message配置类"""

    auth_token: list[str] = field(default_factory=lambda: [])
    """认证令牌，用于旧版API验证，为空则不启用验证"""

    enable_api_server: bool = False
    """是否启用额外的新版API Server"""

    api_server_host: str = "127.0.0.1"
    """新版API Server主机地址，默认仅监听本机"""

    api_server_port: int = 8090
    """新版API Server端口号"""

    api_server_use_wss: bool = False
    """新版API Server是否启用WSS"""

    api_server_cert_file: str = ""
    """新版API Server SSL证书文件路径"""

    api_server_key_file: str = ""
    """新版API Server SSL密钥文件路径"""

    api_server_allowed_api_keys: list[str] = field(default_factory=lambda: [])
    """新版API Server允许的API Key列表；为空时仅本机监听允许匿名连接"""


@dataclass
class LPMMKnowledgeConfig(ConfigBase):
    """旧 LPMM 知识配置兼容项。

    LPMM 知识库已移除；enable/classic 目前只控制回复器是否额外发起一次保守的按需记忆查询。
    新记忆系统已有主检索链路，因此兼容桥默认关闭，避免重复查询与重复注入。
    其余 RAG/QA 参数保留用于兼容旧配置文件。
    """

    enable: bool = False
    """是否启用旧知识入口的按需记忆查询兼容桥（默认关闭）"""

    lpmm_mode: Literal["classic", "agent"] = "agent"
    """旧模式兼容项：classic 启用按需记忆查询桥，agent 跳过该入口"""

    rag_synonym_search_top_k: int = 10
    """旧配置兼容项，当前未使用。RAG同义词搜索的Top K数量"""

    rag_synonym_threshold: float = 0.8
    """旧配置兼容项，当前未使用。RAG同义词搜索的相似度阈值"""

    info_extraction_workers: int = 3
    """旧配置兼容项，当前未使用。信息提取工作线程数"""

    qa_relation_search_top_k: int = 10
    """旧配置兼容项，当前未使用。QA关系搜索的Top K数量"""

    qa_relation_threshold: float = 0.5
    """旧配置兼容项，当前未使用。QA关系搜索的相似度阈值"""

    qa_paragraph_search_top_k: int = 1000
    """旧配置兼容项，当前未使用。QA段落搜索的Top K数量"""

    qa_paragraph_node_weight: float = 0.05
    """旧配置兼容项，当前未使用。QA段落节点权重"""

    qa_ent_filter_top_k: int = 10
    """旧配置兼容项，当前未使用。QA实体过滤的Top K数量"""

    qa_ppr_damping: float = 0.8
    """旧配置兼容项，当前未使用。QA PageRank阻尼系数"""

    qa_res_top_k: int = 3
    """旧配置兼容项，当前未使用。QA最终结果的Top K数量"""

    embedding_dimension: int = 1024
    """旧配置兼容项，当前未使用。嵌入向量维度，应该与模型的输出维度一致"""

    max_embedding_workers: int = 3
    """旧配置兼容项，当前未使用。嵌入/抽取并发线程数"""

    embedding_chunk_size: int = 4
    """旧配置兼容项，当前未使用。每批嵌入的条数"""

    max_synonym_entities: int = 2000
    """旧配置兼容项，当前未使用。同义边参与的实体数上限，超限则跳过"""

    enable_ppr: bool = True
    """旧配置兼容项，当前未使用。是否启用PPR，低配机器可关闭"""
