import asyncio
import time
import random
from typing import List, Optional, Dict, Any, Tuple, TYPE_CHECKING
from rich.traceback import install

from src.common.background_tasks import spawn_background_task
from src.common.logger import get_logger
from src.common.data_models.message_data_model import ReplyContentType
from src.chat.chat_tool_registry import ChatToolRegistry, ToolExecutionResult, format_tool_results_for_reply
from src.chat.message_receive.chat_stream import ChatStream, get_chat_manager
from src.chat.message_receive.recall_registry import recall_registry
from src.common.prompt_manager import prompt_manager
from src.chat.utils.timer_calculator import Timer
from src.chat.brain_chat.private_tool_pipeline import (
    PlannerDecision,
    PrivateToolPipeline,
    PrivateToolPlanner,
    PrivateTurnResult,
)
from src.chat.planner_actions.action_manager import ActionManager
from src.chat.heart_flow.hfc_utils import CycleDetail
from src.chat.heart_flow.turn_scheduler import ReplyTurnScheduler
from src.bw_learner.expression_learner import expression_learner_manager
from src.bw_learner.message_recorder import extract_and_distribute_messages
from src.common.person_stub import Person
from src.llm_models.payload_content import ToolCall
from src.plugin_system.apis import generator_api, send_api, message_api, database_api

# 新记忆系统 — 原始消息归档 + 话题摘要（私聊用）
try:
    from src.memory.layer0_archive import MessageArchiver as _MessageArchiver
    from src.memory.layer1_summarizer import PrivateChatSummarizer as _PrivateChatSummarizer

    _HAS_MEMORY_ARCHIVE = True
except ImportError:
    _MessageArchiver = None  # type: ignore
    _PrivateChatSummarizer = None  # type: ignore
    _HAS_MEMORY_ARCHIVE = False

if TYPE_CHECKING:
    from src.common.data_models.database_data_model import DatabaseMessages
    from src.common.data_models.message_data_model import ReplySetModel


install(extra_lines=3)

# 注释：原来的动作修改超时常量已移除，因为改为顺序执行

logger = get_logger("bc")  # Logger Name Changed


class BrainChatting:
    """
    管理一个连续的私聊Brain Chat循环
    用于在特定聊天流中生成回复。
    """

    def __init__(self, chat_id: str):
        """
        BrainChatting 初始化函数

        参数:
            chat_id: 聊天流唯一标识符(如stream_id)
            on_stop_focus_chat: 当收到stop_focus_chat命令时调用的回调函数
            performance_version: 性能记录版本号，用于区分不同启动版本
        """
        # 基础属性
        self.stream_id: str = chat_id  # 聊天流ID
        self.chat_stream: ChatStream = get_chat_manager().get_stream(self.stream_id)  # type: ignore
        if not self.chat_stream:
            raise ValueError(f"无法找到聊天流: {self.stream_id}")
        self.log_prefix = f"[{get_chat_manager().get_stream_name(self.stream_id) or self.stream_id}]"

        self.expression_learner = expression_learner_manager.get_expression_learner(self.stream_id)

        self.action_manager = ActionManager()
        self.tool_registry = ChatToolRegistry(
            chat_id=self.stream_id,
            chat_scope="private",
            action_manager=self.action_manager,
            chat_stream=self.chat_stream,
        )
        self.tool_planner = PrivateToolPlanner(
            chat_id=self.stream_id,
            tool_registry=self.tool_registry,
            action_manager=self.action_manager,
        )
        self.tool_pipeline = PrivateToolPipeline(planner=self.tool_planner, tool_registry=self.tool_registry)
        logger.info(f"{self.log_prefix} 私聊使用统一原生工具调用管线")

        # 循环控制内部状态
        self.running: bool = False
        self._loop_task: Optional[asyncio.Task] = None  # 主循环任务
        self._new_message_event = asyncio.Event()  # 新消息事件，用于打断 wait

        # 添加循环信息管理相关的属性
        self.history_loop: List[CycleDetail] = []
        self._cycle_counter = 0
        self._current_cycle_detail: CycleDetail = None  # type: ignore

        self.last_read_time = time.time() - 2

        self.turn_scheduler = ReplyTurnScheduler()

        # 新记忆系统 — 原始消息归档器 + 话题摘要器（私聊用）
        self.message_archiver = _MessageArchiver() if _HAS_MEMORY_ARCHIVE else None
        self.topic_summarizer = _PrivateChatSummarizer() if _HAS_MEMORY_ARCHIVE else None

    def notify_new_message(self) -> None:
        """唤醒私聊循环，由 TurnGate 负责后续消息聚合。"""
        self._new_message_event.set()

    async def start(self):
        """检查是否需要启动主循环，如果未激活则启动。"""

        # 如果循环已经激活，直接返回
        if self.running:
            logger.debug(f"{self.log_prefix} BrainChatting 已激活，无需重复启动")
            return

        try:
            # 标记为活动状态，防止重复启动
            self.running = True

            self._loop_task = asyncio.create_task(self._main_chat_loop())
            self._loop_task.add_done_callback(self._handle_loop_completion)
            logger.info(f"{self.log_prefix} BrainChatting 启动完成")

        except Exception as e:
            # 启动失败时重置状态
            self.running = False
            self._loop_task = None
            logger.error(f"{self.log_prefix} BrainChatting 启动失败: {e}")
            raise

    def _handle_loop_completion(self, task: asyncio.Task):
        """当 _hfc_loop 任务完成时执行的回调。"""
        try:
            if exception := task.exception():
                logger.error(f"{self.log_prefix} BrainChatting: 脱离了聊天(异常): {exception}", exc_info=True)
            else:
                logger.info(f"{self.log_prefix} BrainChatting: 脱离了聊天 (外部停止)")
        except asyncio.CancelledError:
            logger.info(f"{self.log_prefix} BrainChatting: 结束了聊天")

    def start_cycle(self) -> Tuple[Dict[str, float], str]:
        self._cycle_counter += 1
        self._current_cycle_detail = CycleDetail(self._cycle_counter)
        self._current_cycle_detail.thinking_id = f"tid{str(round(time.time(), 2))}"
        cycle_timers = {}
        return cycle_timers, self._current_cycle_detail.thinking_id

    def end_cycle(self, loop_info, cycle_timers):
        self._current_cycle_detail.set_loop_info(loop_info)
        self.history_loop.append(self._current_cycle_detail)
        del self.history_loop[:-100]
        self._current_cycle_detail.timers = cycle_timers
        self._current_cycle_detail.end_time = time.time()

    def print_cycle_info(self, cycle_timers):
        # 记录循环信息和计时器结果
        timer_strings = []
        for name, elapsed in cycle_timers.items():
            formatted_time = f"{elapsed * 1000:.2f}毫秒" if elapsed < 1 else f"{elapsed:.2f}秒"
            timer_strings.append(f"{name}: {formatted_time}")

        logger.debug(
            f"{self.log_prefix} 第{self._current_cycle_detail.cycle_id}次思考,"
            f"耗时: {self._current_cycle_detail.end_time - self._current_cycle_detail.start_time:.1f}秒"  # type: ignore
            + (f"\n详情: {'; '.join(timer_strings)}" if timer_strings else "")
        )

    def _get_pending_private_messages(self, end_time: float) -> List["DatabaseMessages"]:
        # 窗口内消息必须完整交给 TurnGate，不能截断后再推进读取游标。
        return message_api.get_messages_by_time_in_chat(
            chat_id=self.stream_id,
            start_time=self.last_read_time,
            end_time=end_time,
            limit=0,
            filter_mai=True,
            filter_command=False,
            filter_intercept_message_level=0,
        )

    async def _loopbody(self):  # sourcery skip: hoist-if-from-if
        # 获取最新消息（用于上下文，但不影响是否调用 observe）
        batch_end_time = time.time()
        recent_messages_list = self._get_pending_private_messages(batch_end_time)

        if recent_messages_list:
            buffer_wait_seconds = self.turn_scheduler.get_private_buffer_wait_seconds()
            if buffer_wait_seconds > 0:
                await asyncio.sleep(buffer_wait_seconds)
                batch_end_time = time.time()
                recent_messages_list = self._get_pending_private_messages(batch_end_time)

        decision = self.turn_scheduler.decide_private_turn(recent_messages=recent_messages_list)
        if decision.should_update_last_read_time:
            self.last_read_time = batch_end_time
        if decision.should_set_new_message_event:
            self._new_message_event.set()  # 触发新消息事件，唤醒等待中的私聊循环

        if not decision.should_observe:
            return False

        # TurnGate 放行后执行一次原生 Tool 规划循环。
        should_continue = await self._observe(recent_messages_list=recent_messages_list)

        if not should_continue:
            # 无 Tool Call 或本轮已终止，返回 False 并等待新消息。
            return False

        # 需要重规划时短暂等待，避免过于频繁的循环。
        await asyncio.sleep(decision.sleep_seconds)

        return True

    async def _send_and_store_reply(
        self,
        response_set: "ReplySetModel",
        action_message: "DatabaseMessages",
        cycle_timers: Dict[str, float],
        thinking_id,
        actions,
        selected_expressions: Optional[List[int]] = None,
    ) -> Tuple[Dict[str, Any], str, Dict[str, float]]:
        with Timer("回复发送", cycle_timers):
            reply_text = await self._send_response(
                reply_set=response_set,
                message_data=action_message,
                selected_expressions=selected_expressions,
            )

        # 获取 platform，如果不存在则从 chat_stream 获取，如果还是 None 则使用默认值
        platform = action_message.chat_info.platform
        if platform is None:
            platform = getattr(self.chat_stream, "platform", "unknown")

        person = Person(platform=platform, user_id=action_message.user_info.user_id)
        person_name = person.person_name
        action_prompt_display = f"你对{person_name}进行了回复：{reply_text}"

        await database_api.store_action_info(
            chat_stream=self.chat_stream,
            action_build_into_prompt=False,
            action_prompt_display=action_prompt_display,
            action_done=True,
            thinking_id=thinking_id,
            action_data={"reply_text": reply_text},
            action_name="reply",
        )

        # 构建循环信息
        loop_info: Dict[str, Any] = {
            "loop_plan_info": {
                "action_result": actions,
            },
            "loop_action_info": {
                "action_taken": True,
                "reply_text": reply_text,
                "command": "",
                "taken_time": time.time(),
            },
        }

        return loop_info, reply_text, cycle_timers

    async def _run_native_tool_turn(
        self,
        cycle_timers: Dict[str, float],
        thinking_id: str,
    ) -> PrivateTurnResult:
        if self.tool_pipeline is None:
            return PrivateTurnResult()

        async def reply_handler(tool_call: ToolCall, decision: PlannerDecision) -> ToolExecutionResult:
            return await self._execute_reply_tool(tool_call, decision, cycle_timers, thinking_id)

        return await self.tool_pipeline.run(
            reply_handler=reply_handler,
            loop_start_time=self.last_read_time,
            context_end_time=self.last_read_time,
            cycle_timers=cycle_timers,
            thinking_id=thinking_id,
        )

    async def _check_reflect_tracker(self) -> None:
        try:
            from src.bw_learner.reflect_tracker import reflect_tracker_manager

            tracker = reflect_tracker_manager.get_tracker(self.stream_id)
            if not tracker:
                return

            resolved = await tracker.trigger_tracker()
            if resolved:
                reflect_tracker_manager.remove_tracker(self.stream_id)
                logger.info(f"{self.log_prefix} ReflectTracker resolved and removed.")
        except Exception:
            logger.exception(f"{self.log_prefix} ReflectTracker 检查失败")

    def _schedule_native_post_turn_tasks(self, recent_messages_list: List["DatabaseMessages"]) -> None:
        spawn_background_task(self._check_reflect_tracker(), name="reflect-tracker-check")

        from src.bw_learner.expression_reflector import expression_reflector_manager

        reflector = expression_reflector_manager.get_or_create_reflector(self.stream_id)
        spawn_background_task(reflector.check_and_ask(), name="expression-reflector")
        spawn_background_task(extract_and_distribute_messages(self.stream_id), name="message-distribute")

        if self.message_archiver is not None and recent_messages_list:
            spawn_background_task(self._archive_recent_messages(recent_messages_list), name="archive-messages")

    async def _observe_native(self, recent_messages_list: List["DatabaseMessages"]) -> bool:
        self.chat_stream = get_chat_manager().get_stream(self.stream_id) or self.chat_stream
        self.tool_registry.chat_stream = self.chat_stream
        context = self.chat_stream.context
        async with prompt_manager.async_message_scope(context.get_template_name() if context else None):
            cycle_timers, thinking_id = self.start_cycle()
            logger.debug(f"{self.log_prefix} 开始第{self._cycle_counter}次思考")

            with Timer("原生工具管线", cycle_timers):
                turn_result = await self._run_native_tool_turn(cycle_timers, thinking_id)

            plan_info = {
                "planner_decisions": [
                    {
                        "content": decision.content,
                        "reasoning": decision.reasoning,
                        "model_name": decision.model_name,
                        "tool_calls": [
                            {
                                "call_id": call.call_id,
                                "name": call.func_name,
                                "arguments": call.args or {},
                            }
                            for call in decision.tool_calls
                        ],
                    }
                    for decision in turn_result.decisions
                ],
                "tool_results": [result.to_prompt_data() for result in turn_result.tool_results],
            }
            action_taken = turn_result.reply_sent or any(result.success for result in turn_result.tool_results)
            loop_info = turn_result.loop_info or {
                "loop_plan_info": plan_info,
                "loop_action_info": {
                    "action_taken": action_taken,
                    "reply_text": turn_result.reply_text,
                    "taken_time": time.time(),
                },
            }
            loop_info["loop_plan_info"] = plan_info
            loop_info["loop_action_info"].update(
                {
                    "action_taken": action_taken,
                    "reply_text": turn_result.reply_text,
                    "taken_time": time.time(),
                }
            )
            self.end_cycle(loop_info, cycle_timers)
            self.print_cycle_info(cycle_timers)
            self._schedule_native_post_turn_tasks(recent_messages_list)
            return turn_result.should_continue

    async def _observe(
        self,
        recent_messages_list: Optional[List["DatabaseMessages"]] = None,
    ) -> bool:
        messages = recent_messages_list or []

        # 提前检查：若本批待处理消息已全部被撤回，则跳过本轮观察。
        # 这是最早的中断点，避免为已撤回消息启动规划与 LLM 调用。
        # 仅在批次非空时判定——空批次属于正常的续轮场景，不应被拦截。
        if messages and all(recall_registry.is_recalled(getattr(msg, "message_id", None)) for msg in messages):
            logger.info(
                f"{self.log_prefix} 待处理消息已全部被撤回，跳过本轮观察",
                event_code="chat.recall.observe_skipped",
                message_count=len(messages),
            )
            return False

        return await self._observe_native(messages)

    async def _archive_recent_messages(self, messages: list["DatabaseMessages"]) -> None:
        """将最近消息归档到第0层原始消息表

        异步 fire-and-forget 任务，不影响主聊天循环。
        DatabaseMessages 与 MessageArchiver 的 duck-typing 不完全兼容，
        此处适配后调用 archiver 归档。
        """
        if self.message_archiver is None:
            return
        import copy

        for msg in messages:
            try:
                # 归档是 fire-and-forget 任务，撤回可能在循环执行期间抵达，
                # 因此逐条检查而非只在入口检查一次。
                if recall_registry.is_recalled(getattr(msg, "message_id", None)):
                    logger.debug(
                        f"{self.log_prefix} 消息已被撤回，跳过归档",
                        event_code="chat.recall.archive_skipped",
                        message_id=getattr(msg, "message_id", None),
                    )
                    continue

                # 适配 DatabaseMessages → archiver 期望的 duck-typing 接口
                adapted = copy.copy(msg)
                adapted.stream_id = msg.chat_id  # chat_id → stream_id
                adapted.content = msg.processed_plain_text  # processed_plain_text → content
                adapted.timestamp = msg.time  # time → timestamp
                await self.message_archiver.archive_private_message(adapted)
            except Exception as e:
                logger.warning(f"消息归档失败: {e}")
                continue

        # 同时更新话题摘要。摘要一旦吸收就无法按 message_id 反查删除，
        # 因此必须在进入摘要前把撤回消息滤掉。
        if self.topic_summarizer is not None:
            for msg in messages:
                if recall_registry.is_recalled(getattr(msg, "message_id", None)):
                    continue
                try:
                    self.topic_summarizer.add_message(
                        stream_id=self.stream_id,
                        message_text=msg.processed_plain_text or "",
                        user_id=msg.user_id,
                        timestamp=msg.time,
                    )
                except Exception:
                    continue

        # 送入编码管线（Layer 2 缓冲区）
        try:
            from src.memory.encoding_pipeline import get_encoding_pipeline

            pipeline = get_encoding_pipeline()
            if pipeline is not None:
                for msg in messages:
                    # 编码管线是通往 Layer 2 的入口，进入后同样无法按 ID 反查
                    if recall_registry.is_recalled(getattr(msg, "message_id", None)):
                        continue
                    try:
                        await pipeline.ingest(
                            stream_id=self.stream_id,
                            user_id=msg.user_id,
                            speaker=msg.user_nickname or msg.user_id,
                            content=msg.processed_plain_text or "",
                            timestamp=msg.time,
                            stream_type="private_chat",
                            message_id=msg.message_id,
                            platform=msg.user_platform,
                            nickname=msg.user_nickname,
                            cardname=msg.user_cardname or "",
                        )
                    except Exception:
                        continue
        except ImportError:
            pass

        # ── 保存未闭合话题（跨轮衔接）──
        try:
            from src.memory.layer1_summarizer import UnclosedTopicBridge

            _bridge = UnclosedTopicBridge()
            if self.topic_summarizer is not None:
                # 群聊话题摘要器 → get_topic_summaries
                if hasattr(self.topic_summarizer, "get_topic_summaries"):
                    _topics = self.topic_summarizer.get_topic_summaries(self.stream_id)
                    if _topics:
                        _bridge.save_unclosed_topics(self.stream_id, _topics)
                # 私聊渐进式摘要器 → get_summary_data
                elif hasattr(self.topic_summarizer, "get_summary_data"):
                    _sd = self.topic_summarizer.get_summary_data(self.stream_id)
                    if _sd:
                        _bridge.save_unclosed_topics(
                            self.stream_id,
                            [
                                {
                                    "topic_id": "private_summary",
                                    "keywords": _sd.get("key_topics", []),
                                    "key_points": [_sd.get("content", "")],
                                    "participant_count": 2,
                                    "message_count": _sd.get("exchange_count", 0),
                                    "last_updated": time.time(),
                                    "is_closed": False,
                                },
                            ],
                        )
        except Exception:
            pass

        # ── 表达学习桥接（异步更新用户表达画像）──
        try:
            from src.memory.expression_bridge import ExpressionBridge
            from src.memory.user_profile import PersonIdentity, ProfileStore

            _expr_bridge = ExpressionBridge(ProfileStore())
            _user_msgs: dict[str, tuple[PersonIdentity, list[str]]] = {}
            for _msg in messages:
                _uid = _msg.user_id
                _txt = _msg.processed_plain_text or ""
                if _uid and _txt.strip():
                    _identity = PersonIdentity(
                        platform=_msg.user_platform or getattr(self.chat_stream, "platform", "") or "legacy",
                        user_id=_uid,
                        nickname=_msg.user_nickname,
                        cardname=_msg.user_cardname or "",
                    )
                    _stored_identity, _txts = _user_msgs.get(_identity.profile_id, (_identity, []))
                    _txts.append(_txt)
                    _user_msgs[_identity.profile_id] = (_stored_identity.merged_with(_identity), _txts)
            for _identity, _txts in _user_msgs.values():
                _expr_bridge.update_expression_profile(_identity, _txts)
        except Exception:
            pass

    async def _main_chat_loop(self):
        """Run in one supervised task; back off repeated failures without spawning replacements."""
        retry_delay = 3
        try:
            while self.running:
                try:
                    success = await self._loopbody()
                    retry_delay = 3
                    if not success:
                        await self._wait_for_new_message()
                        continue
                    await asyncio.sleep(0.1)
                except Exception:
                    logger.exception(f"{self.log_prefix} 聊天循环异常，将于 {retry_delay}s 后重试")
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)
        except asyncio.CancelledError:
            logger.info(f"{self.log_prefix} 聊天循环已取消")
        finally:
            self.running = False

    async def _wait_for_new_message(self):
        """等待新消息到达"""
        last_check_time = self.last_read_time
        check_interval = 30.0  # 入站事件立即唤醒；数据库轮询仅兜底遗漏通知

        # 清除事件状态，准备等待新消息
        self._new_message_event.clear()

        while self.running:
            # 检查是否有新消息
            recent_messages_list = message_api.get_messages_by_time_in_chat(
                chat_id=self.stream_id,
                start_time=last_check_time,
                end_time=time.time(),
                limit=20,
                limit_mode="latest",
                filter_mai=True,
                filter_command=False,
                filter_intercept_message_level=0,
            )

            # 这里只负责唤醒，读取游标由下一轮 TurnGate 在聚合完成后推进
            if len(recent_messages_list) >= 1:
                logger.info(f"{self.log_prefix} 检测到新消息，恢复循环")
                return

            # 等待新消息事件或超时后再次检查
            try:
                await asyncio.wait_for(self._new_message_event.wait(), timeout=check_interval)
                # 事件被触发，说明有新消息
                logger.info(f"{self.log_prefix} 检测到新消息事件，恢复循环")
                return
            except asyncio.TimeoutError:
                # 超时后继续检查
                continue

    async def _send_response(
        self,
        reply_set: "ReplySetModel",
        message_data: "DatabaseMessages",
        selected_expressions: Optional[List[int]] = None,
    ) -> str:
        # 发送前最后一道检查：若被回复的消息在生成期间已被撤回，则中止发送。
        if recall_registry.is_recalled(getattr(message_data, "message_id", None)):
            logger.info(
                f"{self.log_prefix} 目标消息已被撤回，中止回复发送",
                event_code="chat.recall.reply_aborted",
                message_id=getattr(message_data, "message_id", None),
            )
            return ""

        new_message_count = message_api.count_new_messages(
            chat_id=self.chat_stream.stream_id, start_time=self.last_read_time, end_time=time.time()
        )

        need_reply = new_message_count >= random.randint(2, 4)

        if need_reply:
            logger.info(f"{self.log_prefix} 从思考到回复，共有{new_message_count}条新消息，使用引用回复")

        reply_text = ""
        first_replied = False
        for reply_content in reply_set.reply_data:
            if reply_content.content_type != ReplyContentType.TEXT:
                continue
            data: str = reply_content.content  # type: ignore
            if not first_replied:
                await send_api.text_to_stream(
                    text=data,
                    stream_id=self.chat_stream.stream_id,
                    reply_message=message_data,
                    set_reply=need_reply,
                    typing=False,
                    selected_expressions=selected_expressions,
                )
                first_replied = True
            else:
                await send_api.text_to_stream(
                    text=data,
                    stream_id=self.chat_stream.stream_id,
                    reply_message=message_data,
                    set_reply=False,
                    typing=True,
                    selected_expressions=selected_expressions,
                )
            reply_text += data

        return reply_text

    def _has_new_inbound_message_since(self, started_at: float) -> bool:
        return bool(
            message_api.get_messages_by_time_in_chat(
                chat_id=self.stream_id,
                start_time=started_at,
                end_time=time.time(),
                limit=1,
                limit_mode="latest",
                filter_mai=True,
                filter_command=False,
                filter_intercept_message_level=0,
            )
        )

    async def _execute_reply_tool(
        self,
        tool_call: ToolCall,
        decision: PlannerDecision,
        cycle_timers: Dict[str, float],
        thinking_id: str,
    ) -> ToolExecutionResult:
        args = tool_call.args if isinstance(tool_call.args, dict) else {}
        target_message_id = str(args.get("target_message_id", "")).strip()
        action_message = decision.messages_by_id.get(target_message_id)
        if not action_message:
            return ToolExecutionResult(
                call_id=tool_call.call_id,
                tool_name="reply",
                success=False,
                content=f"目标消息 {target_message_id or '<empty>'} 不存在，请重新选择真实消息 ID。",
            )
        if action_message.user_id != self.chat_stream.user_info.user_id:
            return ToolExecutionResult(
                call_id=tool_call.call_id,
                tool_name="reply",
                success=False,
                content=f"目标消息 {target_message_id} 不是当前聊天对象发送的消息，请重新选择。",
            )

        if self._has_new_inbound_message_since(decision.started_at):
            return ToolExecutionResult(
                call_id=tool_call.call_id,
                tool_name="reply",
                success=False,
                content="Planner 决策后收到新消息，旧 reply 已丢弃，请基于最新上下文重新规划。",
                should_continue=True,
            )

        reply_reason = args.get("reply_reason")
        if not isinstance(reply_reason, str) or not reply_reason.strip():
            reply_reason = decision.reasoning or "当前消息需要回应"

        extra_info = format_tool_results_for_reply(decision.tool_results)

        success, llm_response = await generator_api.generate_reply(
            chat_stream=self.chat_stream,
            reply_message=action_message,
            extra_info=extra_info,
            available_actions={},
            chosen_actions=None,
            reply_reason=reply_reason,
            enable_tool=False,
            request_type="replyer",
            from_plugin=False,
            reply_time_point=decision.started_at,
        )
        if not success or not llm_response or not llm_response.reply_set:
            return ToolExecutionResult(
                call_id=tool_call.call_id,
                tool_name="reply",
                success=False,
                content="Replyer 生成回复失败，本轮结束。",
                terminal=True,
            )

        if self._has_new_inbound_message_since(decision.started_at):
            logger.info(f"{self.log_prefix} Replyer 生成期间收到新消息，丢弃过期回复并重新规划")
            return ToolExecutionResult(
                call_id=tool_call.call_id,
                tool_name="reply",
                success=False,
                content="Replyer 生成期间收到新消息，过期回复已丢弃，请重新规划。",
                should_continue=True,
            )

        loop_info, reply_text, _ = await self._send_and_store_reply(
            response_set=llm_response.reply_set,
            action_message=action_message,
            cycle_timers=cycle_timers,
            thinking_id=thinking_id,
            actions=[tool_call],
            selected_expressions=llm_response.selected_expressions,
        )
        await self._apply_memory_feedback(llm_response, reply_text)
        return ToolExecutionResult(
            call_id=tool_call.call_id,
            tool_name="reply",
            success=True,
            content="reply 已发送。",
            terminal=True,
            reply_text=reply_text,
            loop_info=loop_info,
        )

    async def _apply_memory_feedback(self, llm_response: Any, reply_text: str) -> None:
        try:
            atom_ids = getattr(llm_response, "retrieved_atom_ids", None) or []
            if not atom_ids or not reply_text:
                return

            from src.memory.feedback import ReinforcementTracker
            from src.memory.store import MemoryStore

            store = MemoryStore.get_instance()
            tracker = ReinforcementTracker(store)
            atom_data_list = await asyncio.gather(*[store.get_atom(atom_id) for atom_id in atom_ids])
            atoms = [ReinforcementTracker._dict_to_atom(data) for data in atom_data_list if data is not None]
            if atoms:
                usage = tracker.analyze_reply_for_memory_usage(reply_text, atoms)
                await tracker.apply_usage_feedback(usage)
                logger.debug(f"{self.log_prefix} 记忆强化反馈: {len(usage)} 个原子已处理")
        except Exception as exc:
            logger.debug(f"{self.log_prefix} 记忆反馈跳过: {exc}")
