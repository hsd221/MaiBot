"""
第1层记忆摘要器 — 群聊多话题摘要 + 私聊渐进式摘要

本模块纯算法实现，不依赖任何 LLM 调用或外部 NLP 库。
CJK 文本处理基于 2-gram 词频，关键词提取使用简单统计方法。

设计目标:
  - GroupTopicSummarizer: 按话题聚合群聊消息，维护活跃话题列表，
    支持话题合并、关闭和自动裁剪。
  - PrivateChatSummarizer: 为私聊维护持续增长的渐进式摘要，
    超过容量上限时自动合并旧句子。
"""

from dataclasses import dataclass, field
from typing import Any, Optional

from src.memory.types import TopicSummary

import json
import os
import re
import threading
import time

from src.common.logger import get_logger
from src.common.prompt_manager import prompt_manager

logger = get_logger("memory.layer1")

# ---------------------------------------------------------------------------
# CJK 文本工具
# ---------------------------------------------------------------------------

# CJK 统一表意文字范围（含扩展A区常用字）
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")

# 中文常用停用词（基础版，仅含高频率无意义词）
_BASIC_STOPWORDS: set[str] = {
    "的",
    "了",
    "在",
    "是",
    "我",
    "有",
    "和",
    "就",
    "不",
    "人",
    "都",
    "一",
    "一个",
    "上",
    "也",
    "很",
    "到",
    "说",
    "要",
    "去",
    "你",
    "会",
    "着",
    "没有",
    "看",
    "好",
    "自己",
    "这",
    "他",
    "她",
    "它",
    "们",
    "那",
    "里",
    "为",
    "什么",
    "怎么",
    "如何",
    "因为",
    "所以",
    "但是",
    "如果",
    "虽然",
    "这个",
    "那个",
    "时候",
    "可以",
    "知道",
    "觉得",
    "应该",
    "还是",
    "就是",
    "不是",
    "而且",
    "或者",
    "然后",
    "以后",
    "之前",
    "现在",
    "已经",
    "刚刚",
    "正在",
    "一直",
    "一样",
    "一些",
    "有点",
    "可能",
    "大概",
    "真的",
    "非常",
    "比较",
    "特别",
    "其实",
    "当然",
    "不过",
}


def extract_cjk_bigrams(text: str) -> list[str]:
    """提取 CJK 文本中连续的 2-gram 作为关键词候选

    过滤非 CJK 字符后，对连续的中文字符串构建滑动 2-gram。
    例如 "今天天气真好" → ["今天", "天天", "天气", "气真", "真好"]

    Args:
        text: 输入文本

    Returns:
        2-gram 字符串列表
    """
    # 仅保留 CJK 字符
    cjk_chars = "".join(_CJK_RE.findall(text))
    if len(cjk_chars) < 2:
        return []
    return [cjk_chars[i : i + 2] for i in range(len(cjk_chars) - 1)]


def extract_keywords(
    text: str,
    stopwords: Optional[set[str]] = None,
    max_keywords: int = 5,
) -> list[str]:
    """从文本中提取关键词 — 简单词频法

    策略:
      1. 对 CJK 文本提取 2-gram 并统计频率
      2. 对非 CJK 文本按空白/标点分割后统计词频
      3. 过滤停用词后返回 Top N

    Args:
        text: 输入文本
        stopwords: 停用词集合，默认使用内置基础停用词
        max_keywords: 最大返回关键词数

    Returns:
        关键词列表，按频率降序排列
    """
    if not text or not text.strip():
        return []

    sw = _BASIC_STOPWORDS if stopwords is None else stopwords

    # ── CJK 2-gram 频率统计 ──
    bigrams = extract_cjk_bigrams(text)
    freq: dict[str, int] = {}
    for bg in bigrams:
        if bg not in sw:
            freq[bg] = freq.get(bg, 0) + 1

    # ── 非 CJK 词分割 ──
    # 按空白和常见标点分割非 CJK 部分
    non_cjk_tokens = re.findall(r"[a-zA-Z0-9_\u00c0-\u024f\u0400-\u04ff]+", text)
    for token in non_cjk_tokens:
        lower = token.lower()
        if len(lower) >= 2 and lower not in sw:
            freq[lower] = freq.get(lower, 0) + 1

    # 按频率降序排列，取前 N 个
    sorted_keywords = sorted(freq.items(), key=lambda x: (-x[1], x[0]))
    return [kw for kw, _ in sorted_keywords[:max_keywords]]


def extract_key_points(text: str, max_points: int = 3) -> list[str]:
    """从文本中提取要点 — 基于句子长度和位置

    评分公式:
        score = len(sentence) * position_weight
    其中 position_weight:
        - 首句: 1.5
        - 前 25%: 1.3
        - 前半: 1.1
        - 后半: 0.8
        - 末句: 1.2

    Args:
        text: 输入文本
        max_points: 最大提取要点数

    Returns:
        要点句子列表，按评分降序排列
    """
    if not text or not text.strip():
        return []

    # 分句：支持 CJK 和西方标点
    sentences = re.split(r"(?<=[。！？.!?\n])\s*", text)
    sentences = [s.strip() for s in sentences if s and len(s.strip()) > 4]

    if not sentences:
        return []

    total = len(sentences)
    scored: list[tuple[float, str]] = []

    for i, sent in enumerate(sentences):
        # 位置权重
        ratio = i / max(total - 1, 1)
        if i == 0:
            pos_weight = 1.5
        elif i == total - 1:
            pos_weight = 1.2
        elif ratio < 0.25:
            pos_weight = 1.3
        elif ratio < 0.5:
            pos_weight = 1.1
        else:
            pos_weight = 0.8

        # 长度因子（过短的句子权重低，过长的句子适度衰减）
        length = len(sent)
        if length < 8:
            length_factor = 0.5
        elif length > 100:
            length_factor = 1.5  # 长句可能是重要论述
        else:
            length_factor = 1.0

        score = length * pos_weight * length_factor
        scored.append((score, sent))

    # 按评分降序取 top N
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:max_points]]


# ---------------------------------------------------------------------------
# 相似度计算
# ---------------------------------------------------------------------------


def compute_topic_similarity(
    keywords: list[str],
    topic_keywords: list[str],
) -> float:
    """计算关键词集合与话题关键词的相似度 — Jaccard 系数

    Args:
        keywords: 待匹配的关键词列表
        topic_keywords: 话题所含关键词列表

    Returns:
        Jaccard 相似度，范围 [0.0, 1.0]
    """
    set_a = set(keywords)
    set_b = set(topic_keywords)
    intersection = set_a & set_b
    union = set_a | set_b
    if not union:
        return 0.0
    return len(intersection) / len(union)


def _compact_topic_text(text: Any, max_chars: int = 800) -> str:
    """压缩并中和进入话题判断 prompt 的聊天文本。"""
    compacted = str(text or "").replace("\r", "\n").strip()
    replacements = {
        "```": "'''",
        "---BEGIN": "--- BEGIN",
        "---END": "--- END",
        "<<<": "< < <",
        ">>>": "> > >",
    }
    for marker, replacement in replacements.items():
        compacted = compacted.replace(marker, replacement)
    if len(compacted) > max_chars:
        return compacted[:max_chars] + "..."
    return compacted


def _extract_json_candidate(text: str) -> str:
    """从 LLM 响应中取 JSON 片段，支持 markdown code block。"""
    matches = re.findall(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if matches:
        return matches[0].strip()
    return text.strip()


@dataclass
class TopicMessage:
    """供 L1 话题分段判断使用的消息快照。"""

    message_id: str
    text: str
    user_id: str
    speaker: str = ""
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "text": self.text,
            "user_id": self.user_id,
            "speaker": self.speaker,
            "timestamp": self.timestamp,
        }


@dataclass
class JudgedTopicSegment:
    """TopicJudgeAgent 输出的连续话题片段。"""

    topic_title: str
    start_message_id: str
    end_message_id: str
    is_closed: bool
    summary: str = ""


class TopicJudgeAgent:
    """基于 LLM 的 L1 话题分段判断器。

    它只负责判断连续消息的分段和最后一段是否未闭合；LLM 输出会在
    GroupTopicSummarizer 中再次做结构验证，不能直接改变系统状态。
    """

    def __init__(self, task_name: str = "memory_encoder") -> None:
        self.task_name = task_name
        self._llm_request = None

    async def judge(self, messages: list[TopicMessage]) -> list[JudgedTopicSegment]:
        if not messages:
            return []
        prompt = self._build_prompt(messages)
        response = await self._call_llm(prompt)
        return self._parse_response(response)

    @staticmethod
    def _build_prompt(messages: list[TopicMessage]) -> str:
        lines: list[str] = []
        for msg in messages:
            speaker = _compact_topic_text(msg.speaker or msg.user_id, max_chars=80)
            content = _compact_topic_text(msg.text)
            lines.append(
                f"[message_id={msg.message_id} speaker={speaker} timestamp={msg.timestamp}]\n"
                f"<<<MESSAGE_CONTENT\n{content}\nMESSAGE_CONTENT>>>"
            )
        message_block = "\n\n".join(lines)

        return prompt_manager.format_prompt("memory.topic_judge", message_block=message_block)

    async def _call_llm(self, prompt: str) -> str:
        if self._llm_request is None:
            from src.config.config import model_config
            from src.llm_models.utils_model import LLMRequest

            task_config = getattr(model_config.model_task_config, self.task_name, None)
            if task_config is None:
                task_config = model_config.model_task_config.utils
                logger.warning(
                    "话题判断任务未配置，回退到 utils",
                    task_name=self.task_name,
                )
            self._llm_request = LLMRequest(
                model_set=task_config,
                request_type="memory_topic_judge",
            )

        content, _ = await self._llm_request.generate_response_async(
            prompt=prompt,
            temperature=0.0,
            max_tokens=2048,
            raise_when_empty=False,
        )
        return content.strip()

    @staticmethod
    def _parse_response(response: str) -> list[JudgedTopicSegment]:
        if not response:
            return []
        try:
            from json_repair import repair_json

            data = json.loads(repair_json(_extract_json_candidate(response)))
        except Exception as exc:
            logger.warning("话题判断 JSON 解析失败", error=str(exc))
            return []

        raw_segments = data.get("segments", []) if isinstance(data, dict) else []
        segments: list[JudgedTopicSegment] = []
        for item in raw_segments:
            if not isinstance(item, dict):
                continue
            start_message_id = str(item.get("start_message_id", "")).strip()
            end_message_id = str(item.get("end_message_id", "")).strip()
            if not start_message_id or not end_message_id:
                continue
            segments.append(
                JudgedTopicSegment(
                    topic_title=str(item.get("topic_title", "")).strip(),
                    start_message_id=start_message_id,
                    end_message_id=end_message_id,
                    is_closed=bool(item.get("is_closed", False)),
                    summary=str(item.get("summary", "")).strip(),
                )
            )
        return segments


# ---------------------------------------------------------------------------
# 话题状态
# ---------------------------------------------------------------------------


@dataclass
class TopicState:
    """话题状态 — 维护单个话题的实时摘要信息

    Attributes:
        topic_id: 话题唯一标识
        keywords: 话题关键词列表
        key_points: 提炼的要点列表
        participants: 参与用户 ID 集合
        message_count: 归属消息数
        first_seen: 首条消息时间戳
        last_updated: 最后更新时间戳
        is_closed: 是否已关闭（关闭后不再接受新消息）
        topic_title: LLM 判断出的简短话题名
        start_message_id: 话题起始消息 ID
        end_message_id: 话题结束消息 ID
        messages: 话题包含的消息快照
    """

    topic_id: str
    keywords: list[str] = field(default_factory=list)
    key_points: list[str] = field(default_factory=list)
    participants: set[str] = field(default_factory=set)
    message_count: int = 0
    first_seen: float = 0.0
    last_updated: float = 0.0
    is_closed: bool = False
    topic_title: str = ""
    start_message_id: str = ""
    end_message_id: str = ""
    messages: list[TopicMessage] = field(default_factory=list)

    @property
    def participant_count(self) -> int:
        """参与人数"""
        return len(self.participants)

    def to_summary_dict(self) -> TopicSummary:
        """导出为摘要字典，供外部读取"""
        return {
            "topic_id": self.topic_id,
            "keywords": list(self.keywords),
            "key_points": list(self.key_points),
            "participant_count": self.participant_count,
            "message_count": self.message_count,
            "first_seen": self.first_seen,
            "last_updated": self.last_updated,
            "is_closed": self.is_closed,
            "topic_title": self.topic_title,
            "start_message_id": self.start_message_id,
            "end_message_id": self.end_message_id,
            "messages": [msg.to_dict() for msg in self.messages],
        }


@dataclass
class ProgressiveSummary:
    """渐进式摘要 — 为私聊维护持续增长的对话摘要

    Attributes:
        sentences: 摘要句子列表
        key_topics: 对话中已识别的关键话题
        last_speaker: 上一轮发言者
        exchange_count: 对话轮次计数
    """

    sentences: list[str] = field(default_factory=list)
    key_topics: list[str] = field(default_factory=list)
    last_speaker: str = ""
    exchange_count: int = 0


# ---------------------------------------------------------------------------
# 群聊多话题摘要器
# ---------------------------------------------------------------------------


class GroupTopicSummarizer:
    """群聊多话题摘要器

    维护每个聊天流（stream_id）的活跃话题列表。
    旧同步接口会把新消息分配到最匹配的话题，或创建新话题。
    新异步批量接口会每隔 judge_trigger_count 条消息调用 TopicJudgeAgent
    判断连续消息从哪里到哪里属于一个话题，并把最后未闭合片段带入下一轮。
    超过最大话题数时自动裁剪（合并最不活跃的话题）。

    Args:
        max_topics_per_stream: 每个流最大话题数，超限后自动裁剪
        match_threshold: 话题匹配阈值（Jaccard 相似度），低于此值则新建话题
        judge_trigger_count: 新批量接口累计多少条新消息后触发一次话题判断
        topic_judge: 可注入的话题判断器，测试时可传入 fake；默认使用 LLM TopicJudgeAgent
        topic_judge_task_name: LLM 话题判断使用的 model_config 任务名
    """

    def __init__(
        self,
        max_topics_per_stream: int = 10,
        match_threshold: float = 0.3,
        judge_trigger_count: int = 10,
        topic_judge: Optional[Any] = None,
        topic_judge_task_name: str = "memory_encoder",
    ):
        self.max_topics = max(1, max_topics_per_stream)
        self.match_threshold = match_threshold
        self.judge_trigger_count = max(1, judge_trigger_count)
        self.topic_judge = topic_judge
        self.topic_judge_task_name = topic_judge_task_name
        # stream_id -> {topic_id -> TopicState}
        self.topics: dict[str, dict[str, TopicState]] = {}
        # stream_id -> 尚未触发判断的新消息
        self.pending_messages: dict[str, list[TopicMessage]] = {}
        # stream_id -> 上一轮未闭合的最后一段消息，下一轮会重新带入判断
        self.open_topic_messages: dict[str, list[TopicMessage]] = {}
        # stream_id -> 当前临时 open topic 的 topic_id，下一轮重判前会移除
        self.open_topic_ids: dict[str, list[str]] = {}
        self._topic_counter: int = 0
        self._message_counter: int = 0

    def add_message(
        self,
        stream_id: str,
        message_text: str,
        user_id: str,
        timestamp: float,
    ) -> str:
        """将一条消息分配到话题

        流程:
          1. 从消息文本中提取关键词
          2. 与当前活跃话题逐一计算 Jaccard 相似度
          3. 若匹配到现有话题（相似度 > match_threshold）则加入
          4. 否则创建新话题
          5. 若话题数超出上限，自动合并最不活跃的话题

        Args:
            stream_id: 聊天流 ID（如群号）
            message_text: 消息文本
            user_id: 发送者 ID
            timestamp: 时间戳

        Returns:
            归属的话题 ID
        """
        if stream_id not in self.topics:
            self.topics[stream_id] = {}

        keywords = extract_keywords(message_text)
        message = TopicMessage(
            message_id=self._new_message_id(),
            text=message_text or "",
            user_id=user_id,
            speaker=user_id,
            timestamp=timestamp,
        )

        # 尝试匹配现有话题（仅匹配非关闭的话题）
        best_match = None
        best_score = 0.0
        for tid, topic in self.topics[stream_id].items():
            if topic.is_closed:
                continue
            score = compute_topic_similarity(keywords, topic.keywords)
            if score > best_score:
                best_score = score
                best_match = tid

        if best_match is not None and best_score >= self.match_threshold:
            target_id = best_match
            topic = self.topics[stream_id][target_id]
        else:
            target_id = self._new_topic_id()
            topic = TopicState(
                topic_id=target_id,
                keywords=keywords,
                first_seen=timestamp,
                topic_title="、".join(keywords[:3]) or "未命名话题",
                start_message_id=message.message_id,
            )
            self.topics[stream_id][target_id] = topic

        # 更新话题状态
        topic.keywords = self._merge_keywords(topic.keywords, keywords)
        if not topic.topic_title:
            topic.topic_title = "、".join(topic.keywords[:3]) or "未命名话题"
        if not topic.start_message_id:
            topic.start_message_id = message.message_id
        topic.end_message_id = message.message_id
        topic.messages.append(message)
        if len(topic.messages) > 50:
            topic.messages = topic.messages[-50:]
        topic.participants.add(user_id)
        topic.message_count += 1
        topic.last_updated = timestamp

        # 提取要点并追加（限制最大要点数，防无限膨胀）
        new_points = extract_key_points(message_text, max_points=2)
        for pt in new_points:
            if pt not in topic.key_points:
                topic.key_points.append(pt)
        # 控制要点数不超过 20 条
        if len(topic.key_points) > 20:
            topic.key_points = topic.key_points[-20:]

        # 自动裁剪超限话题
        self._trim_if_needed(stream_id)

        logger.debug(
            "话题消息摄入",
            stream_id=stream_id,
            topic_count=len(self.topics.get(stream_id, {})),
            best_topic=best_match if best_match else "none",
        )

        return target_id

    async def add_message_async(
        self,
        stream_id: str,
        message_text: str,
        user_id: str,
        timestamp: float,
        message_id: str = "",
        speaker: str = "",
        force: bool = False,
    ) -> list[TopicSummary]:
        """异步加入一条消息，按批量阈值触发 LLM 话题分段判断。

        Returns:
            本次判断生成或更新的话题摘要；未达到阈值时返回空列表。
        """
        message = TopicMessage(
            message_id=message_id or self._new_message_id(),
            text=message_text or "",
            user_id=user_id,
            speaker=speaker or user_id,
            timestamp=timestamp,
        )
        return await self.add_messages(stream_id, [message], force=force)

    async def add_messages(
        self,
        stream_id: str,
        messages: list[Any],
        force: bool = False,
    ) -> list[TopicSummary]:
        """批量加入消息，并在达到阈值时判断话题边界。

        新消息会先进入 pending buffer。每满 judge_trigger_count 条新消息，
        就将上一轮未闭合尾段 + 这批新消息交给 TopicJudgeAgent 判断。
        判断完成后，闭合片段会固化为 closed topic，最后未闭合片段会进入
        open_topic_messages，下一轮继续参与判断。
        """
        if stream_id not in self.topics:
            self.topics[stream_id] = {}

        pending = self.pending_messages.setdefault(stream_id, [])
        pending.extend(self._coerce_topic_message(item) for item in messages)

        judged_summaries: list[TopicSummary] = []
        while len(pending) >= self.judge_trigger_count:
            batch = pending[: self.judge_trigger_count]
            del pending[: self.judge_trigger_count]
            judged_summaries.extend(await self._judge_message_batch(stream_id, batch))

        if force and pending:
            batch = list(pending)
            pending.clear()
            judged_summaries.extend(await self._judge_message_batch(stream_id, batch))

        return judged_summaries

    def get_open_topic_messages(self, stream_id: str) -> list[dict[str, Any]]:
        """获取当前未闭合尾段消息，主要用于调试和跨轮续传。"""
        return [msg.to_dict() for msg in self.open_topic_messages.get(stream_id, [])]

    def restore_unclosed_topics(self, stream_id: str, topics: list[TopicSummary]) -> None:
        """从 UnclosedTopicBridge 恢复未闭合尾段。

        恢复出的 open topic 仍是临时状态；下一次批量判断前会被移除并重新分段。
        """
        if not topics:
            return

        if stream_id not in self.topics:
            self.topics[stream_id] = {}
        self._discard_open_topics(stream_id)

        restored_open_ids: list[str] = []
        restored_messages: list[TopicMessage] = []
        for topic_data in topics:
            if topic_data.get("is_closed", False):
                continue

            messages = [self._coerce_topic_message(item) for item in topic_data.get("messages", [])]
            if not messages:
                continue

            topic_id = self._new_topic_id()
            state = self._build_topic_state_from_segment(
                topic_id=topic_id,
                segment=JudgedTopicSegment(
                    topic_title=topic_data.get("topic_title", "") or topic_data.get("topic_name", ""),
                    start_message_id=topic_data.get("start_message_id", messages[0].message_id),
                    end_message_id=topic_data.get("end_message_id", messages[-1].message_id),
                    is_closed=False,
                    summary=topic_data.get("summary", ""),
                ),
                messages=messages,
            )
            self.topics[stream_id][topic_id] = state
            restored_open_ids.append(topic_id)
            restored_messages.extend(messages)

        if restored_open_ids and restored_messages:
            self.open_topic_ids[stream_id] = restored_open_ids
            self.open_topic_messages[stream_id] = restored_messages

    def get_topic_summaries(self, stream_id: str) -> list[TopicSummary]:
        """获取指定流的当前所有话题摘要

        Args:
            stream_id: 聊天流 ID

        Returns:
            话题摘要字典列表
        """
        if stream_id not in self.topics:
            return []
        topics = self.topics[stream_id].values()
        return [t.to_summary_dict() for t in topics]

    def merge_topics(
        self,
        stream_id: str,
        topic_a: str,
        topic_b: str,
    ) -> bool:
        """手动合并两个话题

        将 topic_b 合并到 topic_a，合并后 topic_b 标记为关闭。
        合并策略：关键词取并集，要点按时间顺序保留关键信息。

        Args:
            stream_id: 聊天流 ID
            topic_a: 保留的话题 ID
            topic_b: 被合并的话题 ID

        Returns:
            是否合并成功
        """
        if stream_id not in self.topics:
            return False
        topics = self.topics[stream_id]
        if topic_a not in topics or topic_b not in topics:
            return False
        if topic_a == topic_b:
            return True

        ta = topics[topic_a]
        tb = topics[topic_b]

        # 合并关键词
        ta.keywords = self._merge_keywords(ta.keywords, tb.keywords)

        # 合并要点（去重）
        for pt in tb.key_points:
            if pt not in ta.key_points:
                ta.key_points.append(pt)
        if len(ta.key_points) > 20:
            ta.key_points = ta.key_points[-20:]

        # 合并参与者
        ta.participants.update(tb.participants)

        # 合并消息数和时间信息
        ta.message_count += tb.message_count
        ta.first_seen = min(ta.first_seen, tb.first_seen)
        ta.last_updated = max(ta.last_updated, tb.last_updated)

        # 关闭 topic_b
        tb.is_closed = True

        active_count = len(
            [t for t in self.topics[stream_id].values() if not t.is_closed],
        )
        logger.info(
            "话题合并",
            stream_id=stream_id,
            topic_a=topic_a,
            topic_b=topic_b,
            active_count=active_count,
        )

        return True

    def close_topic(self, stream_id: str, topic_id: str) -> Optional[dict]:
        """关闭一个话题并返回最终摘要

        话题关闭后不再接受新消息分配。返回的摘要可作为 MemoryAtom 的来源数据。

        Args:
            stream_id: 聊天流 ID
            topic_id: 话题 ID

        Returns:
            最终摘要字典，若话题不存在返回 None
        """
        if stream_id not in self.topics:
            return None
        topic = self.topics[stream_id].get(topic_id)
        if topic is None:
            return None

        topic.is_closed = True
        logger.info("话题关闭", stream_id=stream_id, topic_id=topic_id)
        return topic.to_summary_dict()

    def get_topic_count(self, stream_id: str) -> int:
        """获取指定流的话题数

        Args:
            stream_id: 聊天流 ID

        Returns:
            活跃话题数量
        """
        if stream_id not in self.topics:
            return 0
        return len([t for t in self.topics[stream_id].values() if not t.is_closed])

    def reset_stream(self, stream_id: str) -> None:
        """重置指定流的所有话题

        Args:
            stream_id: 聊天流 ID
        """
        self.topics.pop(stream_id, None)
        self.pending_messages.pop(stream_id, None)
        self.open_topic_messages.pop(stream_id, None)
        self.open_topic_ids.pop(stream_id, None)

    # ── 内部方法 ──────────────────────────────────────────────

    def _coerce_topic_message(self, item: Any) -> TopicMessage:
        if isinstance(item, TopicMessage):
            return item

        if isinstance(item, dict):
            message_id = str(item.get("message_id") or item.get("id") or self._new_message_id())
            text = str(item.get("text") or item.get("content") or item.get("message_text") or "")
            user_id = str(item.get("user_id") or item.get("sender_id") or item.get("speaker") or "")
            speaker = str(item.get("speaker") or item.get("user_nickname") or user_id)
            timestamp = float(item.get("timestamp") or item.get("time") or time.time())
            return TopicMessage(
                message_id=message_id,
                text=text,
                user_id=user_id,
                speaker=speaker,
                timestamp=timestamp,
            )

        message_id = str(getattr(item, "message_id", "") or self._new_message_id())
        text = str(
            getattr(item, "text", "")
            or getattr(item, "content", "")
            or getattr(item, "processed_plain_text", "")
            or "",
        )
        user_id = str(getattr(item, "user_id", "") or getattr(item, "sender_id", "") or "")
        speaker = str(getattr(item, "speaker", "") or getattr(item, "user_nickname", "") or user_id)
        timestamp = float(getattr(item, "timestamp", 0.0) or getattr(item, "time", 0.0) or time.time())
        return TopicMessage(
            message_id=message_id,
            text=text,
            user_id=user_id,
            speaker=speaker,
            timestamp=timestamp,
        )

    async def _judge_message_batch(
        self,
        stream_id: str,
        new_messages: list[TopicMessage],
    ) -> list[TopicSummary]:
        messages_for_judge = [*self.open_topic_messages.get(stream_id, []), *new_messages]
        if not messages_for_judge:
            return []

        # 上一轮 open topic 只是临时尾段；这轮会重新判断它是否已经闭合。
        self._discard_open_topics(stream_id)

        segments = await self._run_topic_judge(messages_for_judge)
        if not self._segments_are_valid(segments, messages_for_judge):
            logger.warning(
                "话题判断结果无效，回退到关键词分段",
                stream_id=stream_id,
                message_count=len(messages_for_judge),
            )
            segments = self._heuristic_segment_messages(messages_for_judge)

        return self._apply_judged_segments(stream_id, messages_for_judge, segments)

    async def _run_topic_judge(self, messages: list[TopicMessage]) -> list[JudgedTopicSegment]:
        try:
            judge = self._get_topic_judge()
            raw_segments = await judge.judge(messages)
            return self._normalize_judged_segments(raw_segments)
        except Exception as exc:
            logger.warning("话题判断调用失败，回退到关键词分段", error=str(exc))
            return []

    def _get_topic_judge(self) -> Any:
        if self.topic_judge is None:
            self.topic_judge = TopicJudgeAgent(task_name=self.topic_judge_task_name)
        return self.topic_judge

    @staticmethod
    def _normalize_judged_segments(raw_segments: Any) -> list[JudgedTopicSegment]:
        if isinstance(raw_segments, dict):
            raw_segments = raw_segments.get("segments", [])
        if not isinstance(raw_segments, list):
            return []

        normalized: list[JudgedTopicSegment] = []
        for item in raw_segments:
            if isinstance(item, JudgedTopicSegment):
                normalized.append(item)
                continue
            if not isinstance(item, dict):
                continue
            start_message_id = str(item.get("start_message_id", "")).strip()
            end_message_id = str(item.get("end_message_id", "")).strip()
            if not start_message_id or not end_message_id:
                continue
            normalized.append(
                JudgedTopicSegment(
                    topic_title=str(item.get("topic_title", "")).strip(),
                    start_message_id=start_message_id,
                    end_message_id=end_message_id,
                    is_closed=bool(item.get("is_closed", False)),
                    summary=str(item.get("summary", "")).strip(),
                )
            )
        return normalized

    @staticmethod
    def _segments_are_valid(
        segments: list[JudgedTopicSegment],
        messages: list[TopicMessage],
    ) -> bool:
        if not segments or not messages:
            return False

        index_by_id = {msg.message_id: idx for idx, msg in enumerate(messages)}
        expected_start = 0
        for segment in segments:
            if segment.start_message_id not in index_by_id or segment.end_message_id not in index_by_id:
                return False
            start = index_by_id[segment.start_message_id]
            end = index_by_id[segment.end_message_id]
            if start != expected_start or end < start:
                return False
            expected_start = end + 1

        return expected_start == len(messages)

    def _apply_judged_segments(
        self,
        stream_id: str,
        messages: list[TopicMessage],
        segments: list[JudgedTopicSegment],
    ) -> list[TopicSummary]:
        if stream_id not in self.topics:
            self.topics[stream_id] = {}

        index_by_id = {msg.message_id: idx for idx, msg in enumerate(messages)}
        open_ids: list[str] = []
        open_messages: list[TopicMessage] = []
        summaries: list[TopicSummary] = []

        for index, segment in enumerate(segments):
            is_last = index == len(segments) - 1
            if not is_last and not segment.is_closed:
                segment = JudgedTopicSegment(
                    topic_title=segment.topic_title,
                    start_message_id=segment.start_message_id,
                    end_message_id=segment.end_message_id,
                    is_closed=True,
                    summary=segment.summary,
                )

            start = index_by_id[segment.start_message_id]
            end = index_by_id[segment.end_message_id]
            segment_messages = messages[start : end + 1]
            topic_id = self._new_topic_id()
            topic = self._build_topic_state_from_segment(topic_id, segment, segment_messages)
            self.topics[stream_id][topic_id] = topic
            summaries.append(topic.to_summary_dict())

            if not topic.is_closed:
                open_ids.append(topic_id)
                open_messages.extend(segment_messages)

        if open_ids:
            self.open_topic_ids[stream_id] = open_ids
            self.open_topic_messages[stream_id] = open_messages
        else:
            self.open_topic_ids.pop(stream_id, None)
            self.open_topic_messages.pop(stream_id, None)

        self._trim_if_needed(stream_id)
        logger.debug(
            "批量话题判断完成",
            stream_id=stream_id,
            segment_count=len(segments),
            open_count=len(open_ids),
        )
        return summaries

    def _build_topic_state_from_segment(
        self,
        topic_id: str,
        segment: JudgedTopicSegment,
        messages: list[TopicMessage],
    ) -> TopicState:
        text_blob = "\n".join(msg.text for msg in messages)
        keywords = extract_keywords(text_blob, max_keywords=10)
        key_points = [segment.summary] if segment.summary else extract_key_points(text_blob, max_points=3)
        if not key_points and text_blob.strip():
            key_points = [_compact_topic_text(text_blob, max_chars=120)]

        first_seen = min((msg.timestamp for msg in messages), default=0.0)
        last_updated = max((msg.timestamp for msg in messages), default=0.0)
        title = segment.topic_title.strip() or "、".join(keywords[:3]) or "未命名话题"

        return TopicState(
            topic_id=topic_id,
            keywords=keywords,
            key_points=key_points[:20],
            participants={msg.user_id for msg in messages if msg.user_id},
            message_count=len(messages),
            first_seen=first_seen,
            last_updated=last_updated,
            is_closed=segment.is_closed,
            topic_title=title,
            start_message_id=segment.start_message_id,
            end_message_id=segment.end_message_id,
            messages=list(messages),
        )

    def _heuristic_segment_messages(self, messages: list[TopicMessage]) -> list[JudgedTopicSegment]:
        if not messages:
            return []

        groups: list[list[TopicMessage]] = []
        current: list[TopicMessage] = []
        current_keywords: list[str] = []
        for msg in messages:
            msg_keywords = extract_keywords(msg.text, max_keywords=10)
            score = compute_topic_similarity(msg_keywords, current_keywords)
            if current and msg_keywords and current_keywords and score < self.match_threshold:
                groups.append(current)
                current = [msg]
                current_keywords = msg_keywords
                continue

            current.append(msg)
            current_keywords = self._merge_keywords(current_keywords, msg_keywords)

        if current:
            groups.append(current)

        segments: list[JudgedTopicSegment] = []
        for index, group in enumerate(groups):
            text_blob = "\n".join(msg.text for msg in group)
            keywords = extract_keywords(text_blob, max_keywords=3)
            title = "、".join(keywords) or "未命名话题"
            points = extract_key_points(text_blob, max_points=1)
            segments.append(
                JudgedTopicSegment(
                    topic_title=title,
                    start_message_id=group[0].message_id,
                    end_message_id=group[-1].message_id,
                    is_closed=index < len(groups) - 1,
                    summary=points[0] if points else _compact_topic_text(text_blob, max_chars=120),
                )
            )
        return segments

    def _discard_open_topics(self, stream_id: str) -> None:
        topics = self.topics.get(stream_id)
        if topics is not None:
            for topic_id in self.open_topic_ids.get(stream_id, []):
                topics.pop(topic_id, None)
        self.open_topic_ids.pop(stream_id, None)
        self.open_topic_messages.pop(stream_id, None)

    def _new_topic_id(self) -> str:
        """生成递增的话题 ID"""
        self._topic_counter += 1
        return f"topic_{self._topic_counter}"

    def _new_message_id(self) -> str:
        """生成内部消息 ID，用于调用方未提供 message_id 的情况。"""
        self._message_counter += 1
        return f"msg_{self._message_counter}"

    def _trim_if_needed(self, stream_id: str) -> None:
        """如果话题数超出上限，关闭最不活跃的话题，保留各自摘要。"""
        if stream_id not in self.topics:
            return
        active = {tid: t for tid, t in self.topics[stream_id].items() if not t.is_closed}
        if len(active) <= self.max_topics:
            return

        # 按 last_updated 升序排列，最久未更新的排最前。同分再按 topic_id 排序，
        # 避免依赖字典构造顺序导致测试和重启后的裁剪对象漂移。
        sorted_active = sorted(active.items(), key=lambda x: (x[1].last_updated, x[0]))
        # 容量限制不代表话题语义相同，不能把旧内容并入最新话题。
        overflow = len(sorted_active) - self.max_topics
        for tid, _ in sorted_active[:overflow]:
            self.close_topic(stream_id, tid)

    @staticmethod
    def _merge_keywords(
        existing: list[str],
        incoming: list[str],
    ) -> list[str]:
        """合并两组关键词，保留频率权重（已存在的保持原位，新的追加）"""
        seen = set(existing)
        result = list(existing)
        for kw in incoming:
            if kw not in seen:
                result.append(kw)
                seen.add(kw)
        return result[:20]  # 关键词上限 20 个


# ---------------------------------------------------------------------------
# 私聊渐进式摘要器
# ---------------------------------------------------------------------------


class PrivateChatSummarizer:
    """私聊渐进式摘要器

    维护每个私聊流的持续增长对话摘要。
    每次追加新内容时更新摘要而非重写全部，超过容量上限时自动合并旧句子。

    Args:
        max_summary_sentences: 摘要最大句子数，超限后合并相邻旧句
    """

    def __init__(self, max_summary_sentences: int = 20):
        self.max_sentences = max_summary_sentences
        # stream_id -> ProgressiveSummary
        self.summaries: dict[str, ProgressiveSummary] = {}

    def append_exchange(
        self,
        stream_id: str,
        speaker: str,
        content: str,
        timestamp: float,
    ) -> None:
        """追加一轮对话到渐进式摘要

        处理逻辑:
          1. 从本轮对话中提取 1-2 句要点
          2. 格式化为 "[speaker]: point" 追加到句子列表
          3. 若句子数超出 max_sentences，合并相邻旧句子
          4. 收集关键话题词

        Args:
            stream_id: 聊天流 ID
            speaker: 发言人标识
            content: 发言内容
            timestamp: 时间戳（当前未使用，保留供未来扩展）
        """
        if stream_id not in self.summaries:
            self.summaries[stream_id] = ProgressiveSummary()

        summary = self.summaries[stream_id]

        # 提取要点（1-2 句）
        points = extract_key_points(content, max_points=2)
        if not points:
            # 无法提取要点时，用内容前 50 字作为替代
            short = content.strip()[:50]
            if short:
                points = [short]

        # 格式化为句子
        for pt in points:
            sentence = f"[{speaker}]: {pt}"
            summary.sentences.append(sentence)

        # 更新关键话题词
        keywords = extract_keywords(content, max_keywords=3)
        for kw in keywords:
            if kw not in summary.key_topics:
                summary.key_topics.append(kw)

        summary.last_speaker = speaker
        summary.exchange_count += 1

        # 超限压缩
        self._compress_if_needed(stream_id)

        logger.debug(
            "私聊摘要追加",
            stream_id=stream_id,
            exchange_count=summary.exchange_count,
        )

    def get_summary(self, stream_id: str) -> str:
        """获取当前摘要文本

        Args:
            stream_id: 聊天流 ID

        Returns:
            合并后的摘要文本，若不存在则返回空字符串
        """
        if stream_id not in self.summaries:
            return ""
        return "\n".join(self.summaries[stream_id].sentences)

    def get_key_topics(self, stream_id: str) -> list[str]:
        """获取对话中提取的关键话题

        Args:
            stream_id: 聊天流 ID

        Returns:
            关键话题词列表
        """
        if stream_id not in self.summaries:
            return []
        return list(self.summaries[stream_id].key_topics)

    def get_exchange_count(self, stream_id: str) -> int:
        """获取对话轮次计数

        Args:
            stream_id: 聊天流 ID

        Returns:
            累计对话轮次数
        """
        if stream_id not in self.summaries:
            return 0
        return self.summaries[stream_id].exchange_count

    def reset(self, stream_id: str) -> None:
        """重置指定流的摘要

        Args:
            stream_id: 聊天流 ID
        """
        self.summaries.pop(stream_id, None)

    def get_summary_data(self, stream_id: str) -> Optional[dict]:
        """获取摘要完整数据（用于持久化存储）

        返回的字典可直接作为 MemoryAtom 的 source_data 供 store.insert_atom() 使用。
        预期: 通过 store.insert_atom(atom_data) 将摘要持久化到 SQLite。

        Returns:
            包含摘要信息的字典，结构:
            {
                "content": str,
                "key_topics": list[str],
                "exchange_count": int,
                "sentence_count": int,
                "atom_type": "episodic",
                "source_scene": "summary",
            }
        """
        if stream_id not in self.summaries:
            return None
        summary = self.summaries[stream_id]
        return {
            "content": self.get_summary(stream_id),
            "key_topics": list(summary.key_topics),
            "exchange_count": summary.exchange_count,
            "sentence_count": len(summary.sentences),
            "atom_type": "episodic",
            "source_scene": "summary",
        }

    # ── 内部方法 ──────────────────────────────────────────────

    def _compress_if_needed(self, stream_id: str) -> None:
        """超过最大句子数时，合并相邻旧句子"""
        if stream_id not in self.summaries:
            return
        summary = self.summaries[stream_id]
        while len(summary.sentences) > self.max_sentences:
            self._merge_oldest_two(summary)

    @staticmethod
    def _merge_oldest_two(summary: ProgressiveSummary) -> None:
        """合并最旧的两条句子为一条

        从句子列表头部取出两条，用 "；" 连接后放回头部。
        """
        if len(summary.sentences) < 2:
            return

        oldest = summary.sentences.pop(0)
        second = summary.sentences.pop(0)

        # 移除 speaker 前缀（保留第二条的前缀即可）
        merged = f"{oldest}；{second}"
        summary.sentences.insert(0, merged)

    @staticmethod
    def _merge_last_two(summary: ProgressiveSummary) -> None:
        """合并最新的两条句子为一条（替代方案，当前未使用但保留）"""
        if len(summary.sentences) < 2:
            return

        last = summary.sentences.pop()
        prev = summary.sentences.pop()
        merged = f"{prev}；{last}"
        summary.sentences.append(merged)


# ---------------------------------------------------------------------------
# 未闭合话题桥接 — 跨轮衔接
# ---------------------------------------------------------------------------


@dataclass
class SavedTopic:
    """已保存的未闭合话题状态

    由 UnclosedTopicBridge 持久化到 JSON 文件，
    用于跨编码周期的断点续传。

    Attributes:
        topic_id: 话题 ID
        topic_name: 话题显示名称（由关键词生成）
        keywords: 关键词列表
        last_active: 最后活跃时间戳
        participant_count: 参与人数
        message_count: 消息数
        summary: 话题摘要文本
    """

    topic_id: str = ""
    topic_name: str = ""
    keywords: list[str] = field(default_factory=list)
    last_active: float = 0.0
    participant_count: int = 0
    message_count: int = 0
    summary: str = ""


class UnclosedTopicBridge:
    """未闭合话题桥接 — 跨轮衔接断点续传

    当编码周期结束时，保存仍活跃的话题状态；
    下一轮编码开始前恢复，提供话题连续性。

    存储格式为 JSON 文件（非 DB 依赖）：
      data/topic_bridge.json → { "stream_id": [ {topic_dict}, ... ] }

    使用方式:
        bridge = UnclosedTopicBridge()
        bridge.save_unclosed_topics("group_123", topics)
        restored = bridge.restore_topics("group_123")
    """

    _DATA_DIR = os.path.join(
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),
        "data",
    )
    _FILE_PATH = os.path.join(_DATA_DIR, "topic_bridge.json")
    _IO_LOCK = threading.RLock()

    def __init__(self):
        self._data: dict[str, list[TopicSummary]] = {}
        self._load()

    # ── 公开 API ──────────────────────────────────────────────

    def save_unclosed_topics(self, stream_id: str, topics: list[TopicSummary]) -> None:
        """保存未闭合话题

        只有符合以下条件的话题会被保留：
          - 话题未关闭 (is_closed != True)
          - 最后更新在 30 分钟内（仍在活跃窗口）

        Args:
            stream_id: 聊天流 ID
            topics: 话题摘要字典列表
        """
        now = time.time()
        active: list[dict[str, Any]] = []
        for t in topics:
            if t.get("is_closed", False):
                continue
            last_active = t.get("last_updated", now)
            if now - last_active > 1800:  # 30 分钟超时
                continue

            keywords = t.get("keywords", [])
            key_points = t.get("key_points", [])
            messages = t.get("messages", [])
            active.append(
                {
                    "topic_id": t.get("topic_id", ""),
                    "topic_name": t.get("topic_title", "") or "、".join(keywords[:3]) or "未命名话题",
                    "topic_title": t.get("topic_title", ""),
                    "keywords": keywords,
                    "last_active": last_active,
                    "participant_count": t.get("participant_count", 0),
                    "message_count": t.get("message_count", 0),
                    "summary": "；".join(key_points) if key_points else "、".join(keywords),
                    "is_closed": False,
                    "start_message_id": t.get("start_message_id", ""),
                    "end_message_id": t.get("end_message_id", ""),
                    "messages": [
                        {
                            "message_id": str(m.get("message_id", "")),
                            "text": _compact_topic_text(m.get("text", ""), max_chars=800),
                            "user_id": str(m.get("user_id", "")),
                            "speaker": str(m.get("speaker", "")),
                            "timestamp": float(m.get("timestamp", last_active) or last_active),
                        }
                        for m in messages[-30:]
                        if isinstance(m, dict)
                    ],
                }
            )

        with self._IO_LOCK:
            # 每次写前重载最新文件：回复链路会为不同 stream 新建 bridge 实例，若沿用构造时的
            # 整体快照，后保存的实例会把先保存的其他 stream 记录整份覆盖掉。
            self._load()
            if active:
                self._data[stream_id] = active
                self._save()
                logger.info(
                    "未闭合话题已保存",
                    stream_id=stream_id,
                    count=len(active),
                )
            elif stream_id in self._data:
                # 没有活跃话题了，清理该流的记录
                del self._data[stream_id]
                self._save()

    def restore_topics(self, stream_id: str) -> list[TopicSummary]:
        """恢复未闭合话题并从存储中清除

        Args:
            stream_id: 聊天流 ID

        Returns:
            保存的话题字典列表（若无则为空列表）
        """
        with self._IO_LOCK:
            self._load()
            topics = self._data.pop(stream_id, [])
            if topics:
                self._save()
                logger.info(
                    "未闭合话题已恢复",
                    stream_id=stream_id,
                    count=len(topics),
                )
            return topics

    def cleanup_stale(self, max_age_hours: int = 24) -> None:
        """清理过期的未闭合话题

        Args:
            max_age_hours: 最大保留时间（小时）
        """
        with self._IO_LOCK:
            self._load()
            now = time.time()
            max_age = max_age_hours * 3600
            changed = False
            stale_streams: list[str] = []
            for sid, topics in self._data.items():
                fresh = [t for t in topics if now - t.get("last_active", 0) < max_age]
                if len(fresh) != len(topics):
                    changed = True
                    if fresh:
                        self._data[sid] = fresh
                    else:
                        stale_streams.append(sid)
            for sid in stale_streams:
                del self._data[sid]
            if changed:
                self._save()

    # ── 内部 IO ───────────────────────────────────────────────

    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB 上限

    def _load(self) -> None:
        self._data = {}
        try:
            if os.path.exists(self._FILE_PATH):
                file_size = os.path.getsize(self._FILE_PATH)
                if file_size > self.MAX_FILE_SIZE:
                    logger.warning(
                        "topic_bridge.json 文件过大，跳过加载",
                        extra={"path": self._FILE_PATH, "size": file_size, "max": self.MAX_FILE_SIZE},
                    )
                    return
                with open(self._FILE_PATH, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._data = loaded
                else:
                    logger.warning("topic_bridge.json 顶层结构无效，已忽略", path=self._FILE_PATH)
        except Exception:
            logger.warning("topic_bridge.json 加载失败，已忽略损坏快照", path=self._FILE_PATH, exc_info=True)

    def _save(self) -> None:
        temp_path = f"{self._FILE_PATH}.tmp"
        try:
            os.makedirs(os.path.dirname(self._FILE_PATH), exist_ok=True)
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self._FILE_PATH)
        except Exception:
            logger.warning("topic_bridge.json 保存失败", path=self._FILE_PATH, exc_info=True)
            try:
                os.remove(temp_path)
            except OSError:
                pass
