import asyncio
import time
import random
from typing import List, Optional, Dict, Any, Tuple, TYPE_CHECKING
from rich.traceback import install

from src.config.config import global_config
from src.common.background_tasks import spawn_background_task
from src.common.logger import get_logger
from src.common.data_models.info_data_model import ActionPlannerInfo
from src.common.data_models.message_data_model import ReplyContentType
from src.chat.message_receive.chat_stream import ChatStream, get_chat_manager
from src.chat.message_receive.recall_registry import recall_registry
from src.common.prompt_manager import prompt_manager
from src.chat.utils.timer_calculator import Timer
from src.chat.planner_actions.planner import ActionPlanner
from src.chat.planner_actions.action_modifier import ActionModifier
from src.chat.planner_actions.action_manager import ActionManager
from src.chat.heart_flow.hfc_utils import CycleDetail
from src.chat.heart_flow.turn_scheduler import ReplyTurnScheduler
from src.bw_learner.expression_learner import expression_learner_manager
from src.bw_learner.reflect_tracker import reflect_tracker_manager
from src.bw_learner.expression_reflector import expression_reflector_manager
from src.bw_learner.message_recorder import extract_and_distribute_messages
from src.common.person_stub import Person
from src.plugin_system.base.component_types import ActionInfo
from src.plugin_system.apis import generator_api, send_api, message_api, database_api
from src.chat.utils.utils import record_replyer_action_temp

# 新记忆系统 — 原始消息归档 + 话题摘要
try:
    from src.memory.layer0_archive import MessageArchiver as _MessageArchiver
    from src.memory.layer1_summarizer import GroupTopicSummarizer as _GroupTopicSummarizer

    _HAS_MEMORY_ARCHIVE = True
except ImportError:
    _MessageArchiver = None  # type: ignore
    _GroupTopicSummarizer = None  # type: ignore
    _HAS_MEMORY_ARCHIVE = False

if TYPE_CHECKING:
    from src.common.data_models.database_data_model import DatabaseMessages
    from src.common.data_models.message_data_model import ReplySetModel


install(extra_lines=3)

# 注释：原来的动作修改超时常量已移除，因为改为顺序执行

logger = get_logger("hfc")  # Logger Name Changed


class HeartFChatting:
    """
    管理一个连续的Focus Chat循环
    用于在特定聊天流中生成回复。
    其生命周期现在由其关联的 SubHeartflow 的 FOCUSED 状态控制。
    """

    def __init__(self, chat_id: str):
        """
        HeartFChatting 初始化函数

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
        self.action_planner = ActionPlanner(chat_id=self.stream_id, action_manager=self.action_manager)
        self.tool_registry = self.action_planner.tool_registry
        self.action_modifier = ActionModifier(action_manager=self.action_manager, chat_id=self.stream_id)

        # 循环控制内部状态
        self.running: bool = False
        self._loop_task: Optional[asyncio.Task] = None  # 主循环任务
        self._new_message_event = asyncio.Event()

        # 添加循环信息管理相关的属性
        self.history_loop: List[CycleDetail] = []
        self._cycle_counter = 0
        self._current_cycle_detail: CycleDetail = None  # type: ignore

        self.last_read_time = time.time() - 2

        self.is_mute = False

        self.last_active_time = time.time()  # 记录上一次非noreply时间

        self.question_probability_multiplier = 1
        self.questioned = False

        # 跟踪连续 no_reply 次数，用于动态调整阈值
        self.consecutive_no_reply_count = 0
        self.turn_scheduler = ReplyTurnScheduler()

        # 新记忆系统 — 原始消息归档器 + 话题摘要器
        self.message_archiver = _MessageArchiver() if _HAS_MEMORY_ARCHIVE else None
        self.topic_summarizer = _GroupTopicSummarizer() if _HAS_MEMORY_ARCHIVE else None

    def notify_new_message(self) -> None:
        """标记群聊有新消息，下一轮 TurnGate 只聚合一次。"""
        self._new_message_event.set()

    async def _wait_for_group_message_or_timeout(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._new_message_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def start(self):
        """检查是否需要启动主循环，如果未激活则启动。"""

        # 如果循环已经激活，直接返回
        if self.running:
            logger.debug(f"{self.log_prefix} HeartFChatting 已激活，无需重复启动")
            return

        try:
            # 标记为活动状态，防止重复启动
            self.running = True

            self._loop_task = asyncio.create_task(self._main_chat_loop())
            self._loop_task.add_done_callback(self._handle_loop_completion)

            # 记忆系统无需后台启动循环（消息归档在 _observe 中触发）

            logger.info(f"{self.log_prefix} HeartFChatting 启动完成")

        except Exception as e:
            # 启动失败时重置状态
            self.running = False
            self._loop_task = None
            logger.error(f"{self.log_prefix} HeartFChatting 启动失败: {e}")
            raise

    def _handle_loop_completion(self, task: asyncio.Task):
        """当 _hfc_loop 任务完成时执行的回调。"""
        try:
            if exception := task.exception():
                logger.error(f"{self.log_prefix} HeartFChatting: 脱离了聊天(异常): {exception}", exc_info=True)
            else:
                logger.info(f"{self.log_prefix} HeartFChatting: 脱离了聊天 (外部停止)")
        except asyncio.CancelledError:
            logger.info(f"{self.log_prefix} HeartFChatting: 结束了聊天")

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
            if elapsed < 0.1:
                # 不显示小于0.1秒的计时器
                continue
            formatted_time = f"{elapsed:.2f}秒"
            timer_strings.append(f"{name}: {formatted_time}")

        logger.debug(
            f"{self.log_prefix} 第{self._current_cycle_detail.cycle_id}次思考,"
            f"耗时: {self._current_cycle_detail.end_time - self._current_cycle_detail.start_time:.1f}秒;"  # type: ignore
            + (f"详情: {'; '.join(timer_strings)}" if timer_strings else "")
        )

    def _get_pending_group_messages(self, end_time: float) -> List["DatabaseMessages"]:
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

    async def _loopbody(self):
        batch_end_time = time.time()
        recent_messages_list = self._get_pending_group_messages(batch_end_time)

        if recent_messages_list and self._new_message_event.is_set():
            self._new_message_event.clear()
            buffer_wait_seconds = self.turn_scheduler.get_group_buffer_wait_seconds()
            if buffer_wait_seconds > 0:
                await asyncio.sleep(buffer_wait_seconds)
                batch_end_time = time.time()
                recent_messages_list = self._get_pending_group_messages(batch_end_time)
                self._new_message_event.clear()

        decision = self.turn_scheduler.decide_group_turn(
            stream_id=self.stream_id,
            recent_messages=recent_messages_list,
            consecutive_no_reply_count=self.consecutive_no_reply_count,
        )
        if decision.should_update_last_read_time:
            self.last_read_time = batch_end_time

        if not decision.should_observe:
            if decision.should_update_last_read_time and self.message_archiver is not None and recent_messages_list:
                await self._archive_recent_messages(recent_messages_list)
            await self._wait_for_group_message_or_timeout(decision.sleep_seconds)
            return True

        await self._observe(
            recent_messages_list=recent_messages_list,
            force_reply_message=decision.force_reply_message,
        )
        return True

    async def _send_and_store_reply(
        self,
        response_set: "ReplySetModel",
        action_message: "DatabaseMessages",
        cycle_timers: Dict[str, float],
        thinking_id,
        actions,
        selected_expressions: Optional[List[int]] = None,
        quote_message: Optional[bool] = None,
    ) -> Tuple[Dict[str, Any], str, Dict[str, float]]:
        with Timer("回复发送", cycle_timers):
            reply_text = await self._send_response(
                reply_set=response_set,
                message_data=action_message,
                selected_expressions=selected_expressions,
                quote_message=quote_message,
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

    async def _observe(
        self,  # interest_value: float = 0.0,
        recent_messages_list: Optional[List["DatabaseMessages"]] = None,
        force_reply_message: Optional["DatabaseMessages"] = None,
    ) -> bool:  # sourcery skip: merge-else-if-into-elif, remove-redundant-if
        if recent_messages_list is None:
            recent_messages_list = []
        _reply_text = ""  # 初始化reply_text变量，避免UnboundLocalError

        # 提前检查：若强制回复的消息已被撤回，跳过本轮观察。
        # 这是最早的中断点，避免为已撤回消息启动归档、规划、LLM 调用等整个流程。
        if force_reply_message and recall_registry.is_recalled(getattr(force_reply_message, "message_id", None)):
            logger.info(
                f"{self.log_prefix} 强制回复的消息已被撤回，跳过本轮观察",
                event_code="chat.recall.observe_skipped",
                message_id=getattr(force_reply_message, "message_id", None),
            )
            return False

        # ── 未闭合话题恢复（跨轮衔接）──
        _restored_topics: list[dict] = []
        try:
            from src.memory.layer1_summarizer import UnclosedTopicBridge

            _bridge = UnclosedTopicBridge()
            _rt = _bridge.restore_topics(self.stream_id)
            if _rt:
                _restored_topics = _rt
                if self.topic_summarizer is not None and hasattr(self.topic_summarizer, "restore_unclosed_topics"):
                    self.topic_summarizer.restore_unclosed_topics(self.stream_id, _rt)
                logger.info(f"{self.log_prefix} 恢复 {len(_rt)} 个未闭合话题")
        except Exception:
            pass

        # -------------------------------------------------------------------------
        # ReflectTracker Check
        # 在每次回复前检查一次上下文，看是否有反思问题得到了解答
        # -------------------------------------------------------------------------

        reflector = expression_reflector_manager.get_or_create_reflector(self.stream_id)
        await reflector.check_and_ask()
        tracker = reflect_tracker_manager.get_tracker(self.stream_id)
        if tracker:
            resolved = await tracker.trigger_tracker()
            if resolved:
                reflect_tracker_manager.remove_tracker(self.stream_id)
                logger.debug(f"{self.log_prefix} ReflectTracker resolved and removed.")

        start_time = time.time()
        self.chat_stream = get_chat_manager().get_stream(self.stream_id) or self.chat_stream
        self.tool_registry.chat_stream = self.chat_stream
        context = self.chat_stream.context
        async with prompt_manager.async_message_scope(context.get_template_name() if context else None):
            # 通过 MessageRecorder 统一提取消息并分发给 expression_learner 和 jargon_miner
            # 在 replyer 执行时触发，统一管理时间窗口，避免重复获取消息
            spawn_background_task(extract_and_distribute_messages(self.stream_id), name="message-distribute")

            # 异步归档原始消息到第0层
            if self.message_archiver is not None and recent_messages_list:
                spawn_background_task(self._archive_recent_messages(recent_messages_list), name="archive-messages")

            cycle_timers, thinking_id = self.start_cycle()
            logger.debug(
                f"{self.log_prefix} 开始第{self._cycle_counter}次思考(频率: {global_config.chat.get_talk_value(self.stream_id)})"
            )

            # 第一步：动作检查
            available_actions: Dict[str, ActionInfo] = {}
            try:
                await self.action_modifier.modify_actions()
                available_actions = self.action_manager.get_using_actions()
            except Exception as e:
                logger.error(f"{self.log_prefix} 动作修改失败: {e}")

            with Timer("规划器", cycle_timers):
                action_to_use_info = await self.action_planner.plan(
                    loop_start_time=self.last_read_time,
                    available_actions=available_actions,
                    force_reply_message=force_reply_message,
                )

            logger.debug(
                f"{self.log_prefix} 决定执行{len(action_to_use_info)}个动作: {' '.join([a.action_type for a in action_to_use_info])}"
            )
            if not action_to_use_info:
                self.consecutive_no_reply_count += 1

            # 3. 并行执行所有动作
            action_tasks = [
                asyncio.create_task(
                    self._execute_action(action, action_to_use_info, thinking_id, available_actions, cycle_timers)
                )
                for action in action_to_use_info
            ]

            # 并行执行所有任务
            results = await asyncio.gather(*action_tasks, return_exceptions=True)

            # 处理执行结果
            reply_loop_info = None
            reply_text_from_reply = ""
            action_success = False
            action_reply_text = ""

            excute_result_str = ""
            for result in results:
                if isinstance(result, BaseException):
                    logger.error(f"{self.log_prefix} 动作执行异常: {result}")
                    continue

                excute_result_str += f"{result['action_type']} 执行结果:{result['result']}\n"

                if result["action_type"] != "reply":
                    action_success = result["success"]
                    action_reply_text = result["result"]
                elif result["action_type"] == "reply":
                    if result["success"]:
                        reply_loop_info = result["loop_info"]
                        reply_text_from_reply = result["result"]
                    else:
                        logger.warning(f"{self.log_prefix} 回复动作执行失败")

            self.action_planner.add_plan_excute_log(result=excute_result_str)

            # 构建最终的循环信息
            if reply_loop_info:
                # 如果有回复信息，使用回复的loop_info作为基础
                loop_info = reply_loop_info
                # 更新动作执行信息
                loop_info["loop_action_info"].update(
                    {
                        "action_taken": action_success,
                        "taken_time": time.time(),
                    }
                )
                _reply_text = reply_text_from_reply
            else:
                # 没有回复信息，构建纯动作的loop_info
                loop_info = {
                    "loop_plan_info": {
                        "action_result": action_to_use_info,
                    },
                    "loop_action_info": {
                        "action_taken": action_success,
                        "reply_text": action_reply_text,
                        "taken_time": time.time(),
                    },
                }
                _reply_text = action_reply_text

            self.end_cycle(loop_info, cycle_timers)
            self.print_cycle_info(cycle_timers)

            end_time = time.time()
            if end_time - start_time < global_config.chat.planner_smooth:
                wait_time = global_config.chat.planner_smooth - (end_time - start_time)
                await asyncio.sleep(wait_time)
            else:
                await asyncio.sleep(0.1)
            return True

    async def _archive_recent_messages(self, messages: list["DatabaseMessages"]) -> None:
        """将 recent_messages_list 归档到第0层原始消息表

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
                adapted.user_id = msg.user_id  # archiver 需要
                adapted.message_id = msg.message_id  # archiver 需要
                await self.message_archiver.archive_group_message(adapted)
            except Exception as e:
                logger.warning(f"消息归档失败: {e}")
                continue

        # 同时更新话题摘要。摘要是多条消息融合的自然语言，一旦吸收就无法按
        # message_id 反查删除，因此必须在进入摘要前就把撤回消息滤掉。
        if self.topic_summarizer is not None:
            summarizable = [
                msg for msg in messages if not recall_registry.is_recalled(getattr(msg, "message_id", None))
            ]
            try:
                if hasattr(self.topic_summarizer, "add_messages"):
                    await self.topic_summarizer.add_messages(
                        stream_id=self.stream_id,
                        messages=[
                            {
                                "message_id": msg.message_id,
                                "text": msg.processed_plain_text or "",
                                "user_id": msg.user_id,
                                "speaker": msg.user_nickname or msg.user_id,
                                "timestamp": msg.time,
                            }
                            for msg in summarizable
                        ],
                    )
                else:
                    for msg in summarizable:
                        self.topic_summarizer.add_message(
                            stream_id=self.stream_id,
                            message_text=msg.processed_plain_text or "",
                            user_id=msg.user_id,
                            timestamp=msg.time,
                        )
            except Exception:
                pass

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
                            stream_type="group_chat",
                            message_id=msg.message_id,
                            platform=msg.user_platform,
                            nickname=msg.user_nickname,
                            cardname=msg.user_cardname or "",
                            group_id=msg.group_info.group_id if msg.group_info else "",
                            group_name=msg.group_info.group_name if msg.group_info else "",
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
                _topics = self.topic_summarizer.get_topic_summaries(self.stream_id)
                if _topics:
                    _bridge.save_unclosed_topics(self.stream_id, _topics)
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
                        group_id=_msg.group_info.group_id if _msg.group_info else "",
                        group_name=_msg.group_info.group_name if _msg.group_info else "",
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
                    await asyncio.sleep(0.1)
                    if not success:
                        break
                except Exception:
                    logger.exception(f"{self.log_prefix} 聊天循环异常，将于 {retry_delay}s 后重试")
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)
        except asyncio.CancelledError:
            logger.info(f"{self.log_prefix} 聊天循环已取消")
        finally:
            self.running = False

    async def _handle_action(
        self,
        action: str,
        action_reasoning: str,
        action_data: dict,
        cycle_timers: Dict[str, float],
        thinking_id: str,
        action_message: Optional["DatabaseMessages"] = None,
    ) -> tuple[bool, str, str]:
        """
        处理规划动作，使用动作工厂创建相应的动作处理器

        参数:
            action: 动作类型
            action_reasoning: 决策理由
            action_data: 动作数据，包含不同动作需要的参数
            cycle_timers: 计时器字典
            thinking_id: 思考ID
            action_message: 消息数据
        返回:
            tuple[bool, str, str]: (是否执行了动作, 思考消息ID, 命令)
        """
        try:
            # 使用工厂创建动作处理器实例
            try:
                action_handler = self.action_manager.create_action(
                    action_name=action,
                    action_data=action_data,
                    cycle_timers=cycle_timers,
                    thinking_id=thinking_id,
                    chat_stream=self.chat_stream,
                    log_prefix=self.log_prefix,
                    action_reasoning=action_reasoning,
                    action_message=action_message,
                )
            except Exception as e:
                logger.error(f"{self.log_prefix} 创建动作处理器时出错: {e}", exc_info=True)
                return False, ""

            if action_handler is None:
                logger.warning("动作处理器不可用", action_name=action)
                return False, "动作处理器不可用"

            # 处理动作并获取结果（固定记录一次动作信息）
            result = await action_handler.execute()
            success, action_text = result

            return success, action_text

        except Exception as e:
            logger.error(f"{self.log_prefix} 处理{action}时出错: {e}", exc_info=True)
            return False, ""

    async def _send_response(
        self,
        reply_set: "ReplySetModel",
        message_data: "DatabaseMessages",
        selected_expressions: Optional[List[int]] = None,
        quote_message: Optional[bool] = None,
    ) -> str:
        # 发送前最后一道检查：若被回复的消息在生成期间已被撤回，则中止发送。
        # 这里挡住的是最尴尬的情况——回复一条已经不存在的消息。
        if recall_registry.is_recalled(getattr(message_data, "message_id", None)):
            logger.info(
                f"{self.log_prefix} 目标消息已被撤回，中止回复发送",
                event_code="chat.recall.reply_aborted",
                message_id=getattr(message_data, "message_id", None),
            )
            return ""

        # 根据 llm_quote 配置决定是否使用 quote_message 参数
        if global_config.chat.llm_quote:
            # 如果配置为 true，使用 llm_quote 参数决定是否引用回复
            if quote_message is None:
                logger.warning(f"{self.log_prefix} quote_message 参数为空，不引用")
                need_reply = False
            else:
                need_reply = quote_message
                if need_reply:
                    logger.info(f"{self.log_prefix} LLM 决定使用引用回复")
        else:
            # 如果配置为 false，使用原来的模式
            new_message_count = message_api.count_new_messages(
                chat_id=self.chat_stream.stream_id, start_time=self.last_read_time, end_time=time.time()
            )
            need_reply = new_message_count >= random.randint(2, 3) or time.time() - self.last_read_time > 90
            if need_reply:
                logger.info(
                    f"{self.log_prefix} 从思考到回复，共有{new_message_count}条新消息，使用引用回复，或者上次回复时间超过90秒"
                )

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

    async def _execute_action(
        self,
        action_planner_info: ActionPlannerInfo,
        chosen_action_plan_infos: List[ActionPlannerInfo],
        thinking_id: str,
        available_actions: Dict[str, ActionInfo],
        cycle_timers: Dict[str, float],
    ):
        """执行单个动作的通用函数"""
        try:
            with Timer(f"动作{action_planner_info.action_type}", cycle_timers):
                if action_planner_info.action_type == "reply":
                    # 直接当场执行reply逻辑
                    self.questioned = False
                    # 刷新主动发言状态
                    # 重置连续 no_reply 计数
                    self.consecutive_no_reply_count = 0

                    reason = action_planner_info.reasoning or ""
                    think_level = 1
                    # 使用 action_reasoning（planner 的整体思考理由）作为 reply_reason
                    planner_reasoning = action_planner_info.action_reasoning or reason

                    record_replyer_action_temp(
                        chat_id=self.stream_id,
                        reason=reason,
                        think_level=think_level,
                    )

                    await database_api.store_action_info(
                        chat_stream=self.chat_stream,
                        action_build_into_prompt=False,
                        action_prompt_display=reason,
                        action_done=True,
                        thinking_id=thinking_id,
                        action_data={},
                        action_name="reply",
                        action_reasoning=reason,
                    )

                    # 从 Planner 的 action_data 中提取未知词语列表（仅在 reply 时使用）
                    unknown_words = None
                    quote_message = None
                    extra_info = ""
                    if isinstance(action_planner_info.action_data, dict):
                        uw = action_planner_info.action_data.get("unknown_words")
                        if isinstance(uw, list):
                            cleaned_uw: List[str] = []
                            for item in uw:
                                if isinstance(item, str):
                                    s = item.strip()
                                    if s:
                                        cleaned_uw.append(s)
                            if cleaned_uw:
                                unknown_words = cleaned_uw

                        # 从 Planner 的 action_data 中提取 quote_message 参数
                        qm = action_planner_info.action_data.get("quote")
                        if qm is not None:
                            # 支持多种格式：true/false, "true"/"false", 1/0
                            if isinstance(qm, bool):
                                quote_message = qm
                            elif isinstance(qm, str):
                                quote_message = qm.lower() in ("true", "1", "yes")
                            elif isinstance(qm, (int, float)):
                                quote_message = bool(qm)

                        raw_extra_info = action_planner_info.action_data.get("extra_info")
                        if isinstance(raw_extra_info, str):
                            extra_info = raw_extra_info

                        logger.info(f"{self.log_prefix} {qm}引用回复设置: {quote_message}")

                    success, llm_response = await generator_api.generate_reply(
                        chat_stream=self.chat_stream,
                        reply_message=action_planner_info.action_message,
                        available_actions=available_actions,
                        chosen_actions=chosen_action_plan_infos,
                        reply_reason=planner_reasoning,
                        extra_info=extra_info,
                        unknown_words=unknown_words,
                        enable_tool=False,
                        request_type="replyer",
                        from_plugin=False,
                        reply_time_point=(action_planner_info.action_data or {}).get("loop_start_time", time.time()),
                        think_level=think_level,
                    )

                    if not success or not llm_response or not llm_response.reply_set:
                        if action_planner_info.action_message:
                            logger.info(f"对 {action_planner_info.action_message.processed_plain_text} 的回复生成失败")
                        else:
                            logger.info("回复生成失败")
                        return {"action_type": "reply", "success": False, "result": "回复生成失败", "loop_info": None}

                    response_set = llm_response.reply_set
                    selected_expressions = llm_response.selected_expressions
                    loop_info, reply_text, _ = await self._send_and_store_reply(
                        response_set=response_set,
                        action_message=action_planner_info.action_message,  # type: ignore
                        cycle_timers=cycle_timers,
                        thinking_id=thinking_id,
                        actions=chosen_action_plan_infos,
                        selected_expressions=selected_expressions,
                        quote_message=quote_message,
                    )
                    self.last_active_time = time.time()

                    try:
                        atom_ids = getattr(llm_response, "retrieved_atom_ids", None) or []
                        if atom_ids and reply_text:
                            from src.memory.feedback import ReinforcementTracker
                            from src.memory.store import MemoryStore

                            store = MemoryStore.get_instance()
                            tracker = ReinforcementTracker(store)
                            atom_data_list = await asyncio.gather(*[store.get_atom(aid) for aid in atom_ids])
                            atoms = [ReinforcementTracker._dict_to_atom(d) for d in atom_data_list if d is not None]
                            if atoms:
                                usage = tracker.analyze_reply_for_memory_usage(reply_text, atoms)
                                await tracker.apply_usage_feedback(usage)
                                logger.debug(f"{self.log_prefix} 记忆强化反馈: {len(usage)} 个原子已处理")
                    except Exception as e:
                        logger.debug(f"{self.log_prefix} 记忆反馈跳过: {e}")

                    return {
                        "action_type": "reply",
                        "success": True,
                        "result": f"你使用reply动作，对' {action_planner_info.action_message.processed_plain_text} '这句话进行了回复，回复内容为: '{reply_text}'",
                        "loop_info": loop_info,
                    }

                else:
                    # 执行普通动作
                    if not self.tool_registry.is_available(action_planner_info.action_type):
                        logger.info(
                            f"{self.log_prefix} Action {action_planner_info.action_type} 在执行前已被禁用，跳过"
                        )
                        return {
                            "action_type": action_planner_info.action_type,
                            "success": False,
                            "result": f"Action {action_planner_info.action_type} 当前已被禁用。",
                        }
                    with Timer("动作执行", cycle_timers):
                        success, result = await self._handle_action(
                            action=action_planner_info.action_type,
                            action_reasoning=action_planner_info.action_reasoning or "",
                            action_data=action_planner_info.action_data or {},
                            cycle_timers=cycle_timers,
                            thinking_id=thinking_id,
                            action_message=action_planner_info.action_message,
                        )

                    self.last_active_time = time.time()
                    return {
                        "action_type": action_planner_info.action_type,
                        "success": success,
                        "result": result,
                    }

        except Exception as e:
            logger.exception(f"{self.log_prefix} 执行动作时出错: {e}")
            return {
                "action_type": action_planner_info.action_type,
                "success": False,
                "result": "",
                "loop_info": None,
                "error": str(e),
            }
