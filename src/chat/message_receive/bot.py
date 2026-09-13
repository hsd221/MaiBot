import os
import re

from typing import Dict, Any, Optional
from maim_message import UserInfo, Seg, GroupInfo

from src.common.logger import get_logger, hash_id
from src.common.prompt_manager import prompt_manager
from src.common.data_models.message_component_model import from_seg_to_components
from src.config.config import global_config
from src.chat.message_receive.chat_stream import get_chat_manager
from src.chat.message_receive.message import MessageRecv
from src.chat.message_receive.storage import MessageStorage
from src.chat.message_receive.recall_registry import recall_registry
from src.chat.heart_flow.heartflow_message_processor import HeartFCMessageReceiver
from src.plugin_system.core import component_registry, events_manager, global_announcement_manager
from src.plugin_system.base import BaseCommand, EventType

# 记忆系统 — 原始消息归档（撤回清理需要）
try:
    from src.memory.layer0_archive import MessageArchiver as _MessageArchiver

    _HAS_MEMORY_ARCHIVE = True
except ImportError:
    _MessageArchiver = None  # type: ignore
    _HAS_MEMORY_ARCHIVE = False

# 定义日志配置

# 获取项目根目录（假设本文件在src/chat/message_receive/下，根目录为上上上级目录）
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))

# 配置主程序日志格式
logger = get_logger("chat")


def _apply_modified_message(
    message: MessageRecv,
    modified_message: Any,
    *,
    merge_additional_data: bool = False,
) -> None:
    if not modified_message:
        return
    if modified_message._modify_flags.modify_message_segments:
        message.message_segment = Seg(type="seglist", data=modified_message.message_segments)
        message.message_components = from_seg_to_components(message.message_segment)
    if modified_message._modify_flags.modify_plain_text:
        message.processed_plain_text = modified_message.plain_text
    if merge_additional_data and modified_message.additional_data:
        additional_config = message.message_info.additional_config
        if not isinstance(additional_config, dict):
            additional_config = {}
        additional_config.update(modified_message.additional_data)
        message.message_info.additional_config = additional_config


def _check_ban_words(text: str, userinfo: UserInfo, group_info: Optional[GroupInfo] = None) -> bool:
    """检查消息是否包含过滤词

    Args:
        text: 待检查的文本
        chat: 聊天对象
        userinfo: 用户信息

    Returns:
        bool: 是否包含过滤词
    """
    for word in global_config.message_receive.ban_words:
        if word in text:
            logger.info(
                "消息命中过滤词，已跳过处理",
                event_code="chat.message.filtered_by_word",
                chat_type="group" if group_info else "private",
                group_id=group_info.group_id if group_info else None,
                user_id=userinfo.user_id,
                text_hash=hash_id(text),
                text_length=len(text),
                rule_hash=hash_id(word),
            )
            return True
    return False


def _check_ban_regex(text: str, userinfo: UserInfo, group_info: Optional[GroupInfo] = None) -> bool:
    """检查消息是否匹配过滤正则表达式

    Args:
        text: 待检查的文本
        chat: 聊天对象
        userinfo: 用户信息

    Returns:
        bool: 是否匹配过滤正则
    """
    # 检查text是否为None或空字符串
    if text is None or not text:
        return False

    for pattern in global_config.message_receive.ban_msgs_regex:
        if re.search(pattern, text):
            logger.info(
                "消息命中过滤正则，已跳过处理",
                event_code="chat.message.filtered_by_regex",
                chat_type="group" if group_info else "private",
                group_id=group_info.group_id if group_info else None,
                user_id=userinfo.user_id,
                text_hash=hash_id(text),
                text_length=len(text),
                pattern=pattern,
            )
            return True
    return False


class ChatBot:
    def __init__(self):
        self.bot = None  # bot 实例引用
        self._started = False
        self.heartflow_message_receiver = HeartFCMessageReceiver()  # 新增

    async def _ensure_started(self):
        """确保所有任务已启动"""
        if not self._started:
            logger.debug("ChatBot 启动状态已确认", event_code="chatbot.ensure_started")

            self._started = True

    async def _process_commands(self, message: MessageRecv):
        # sourcery skip: use-named-expression
        """使用新插件系统处理命令"""
        try:
            text = message.processed_plain_text

            # 使用新的组件注册中心查找命令
            command_result = component_registry.find_command_by_text(text)
            if command_result:
                command_class, matched_groups, command_info = command_result
                plugin_name = command_info.plugin_name
                command_name = command_info.name
                if (
                    message.chat_stream
                    and message.chat_stream.stream_id
                    and command_name
                    in global_announcement_manager.get_disabled_chat_commands(message.chat_stream.stream_id)
                ):
                    logger.info(
                        "命令被聊天流配置禁用，已跳过处理",
                        event_code="chat.command.skipped_disabled",
                        plugin_name=plugin_name,
                        command_name=command_name,
                        chat_id=message.chat_stream.stream_id,
                    )
                    return False, None, True

                message.is_command = True

                # 获取插件配置
                plugin_config = component_registry.get_plugin_config(plugin_name)

                # 创建命令实例
                command_instance: BaseCommand = command_class(message, plugin_config)
                command_instance.set_matched_groups(matched_groups)

                hook_context = {
                    "command_name": command_name,
                    "plugin_name": plugin_name,
                    "matched_groups": matched_groups,
                }
                continue_flag, modified_message = await events_manager.handle_mai_events(
                    EventType.ON_COMMAND_BEFORE_EXECUTE,
                    message,
                    extra_data=hook_context,
                )
                if not continue_flag:
                    logger.info(
                        "命令执行前 hook 取消命令",
                        event_code="chat.command.cancelled_by_before_hook",
                        plugin_name=plugin_name,
                        command_name=command_name,
                    )
                    return True, None, False
                if modified_message:
                    _apply_modified_message(message, modified_message)
                    if "matched_groups" in modified_message.additional_data:
                        updated_groups = modified_message.additional_data["matched_groups"]
                        if isinstance(updated_groups, dict):
                            matched_groups = updated_groups
                            command_instance.set_matched_groups(matched_groups)
                            hook_context["matched_groups"] = matched_groups
                    if modified_message.additional_data.get("execute_command") is False:
                        logger.info(
                            "命令执行前 hook 跳过命令",
                            event_code="chat.command.skipped_by_before_hook",
                            plugin_name=plugin_name,
                            command_name=command_name,
                        )
                        continue_process = bool(modified_message.additional_data.get("continue_process", True))
                        return True, modified_message.additional_data.get("response"), continue_process

                try:
                    # 执行命令
                    success, response, intercept_message_level = await command_instance.execute()
                    message.intercept_message_level = intercept_message_level

                    continue_process = not bool(intercept_message_level)
                    continue_flag, modified_message = await events_manager.handle_mai_events(
                        EventType.ON_COMMAND_AFTER_EXECUTE,
                        message,
                        extra_data={
                            **hook_context,
                            "success": success,
                            "response": response,
                            "intercept_message_level": intercept_message_level,
                            "continue_process": continue_process,
                        },
                    )
                    if not continue_flag:
                        continue_process = False
                    if modified_message:
                        _apply_modified_message(message, modified_message)
                        if "success" in modified_message.additional_data:
                            success = bool(modified_message.additional_data["success"])
                        if "response" in modified_message.additional_data:
                            response = modified_message.additional_data["response"]
                        if "intercept_message_level" in modified_message.additional_data:
                            intercept_message_level = int(modified_message.additional_data["intercept_message_level"])
                            message.intercept_message_level = intercept_message_level
                        if "continue_process" in modified_message.additional_data:
                            continue_process = bool(modified_message.additional_data["continue_process"])

                    # 记录命令执行结果
                    if success:
                        logger.info(
                            "命令执行完成",
                            event_code="chat.command.executed",
                            status="success",
                            plugin_name=plugin_name,
                            command_name=command_name,
                            command_class=command_class.__name__,
                            intercept_message_level=intercept_message_level,
                        )
                    else:
                        logger.warning(
                            "命令执行完成",
                            event_code="chat.command.executed",
                            status="failed",
                            plugin_name=plugin_name,
                            command_name=command_name,
                            command_class=command_class.__name__,
                            response=response,
                            intercept_message_level=intercept_message_level,
                        )

                    # 根据命令的拦截设置决定是否继续处理消息
                    return (
                        True,
                        response,
                        continue_process,
                    )  # 找到命令，根据intercept_message决定是否继续

                except Exception:
                    logger.exception(
                        "命令执行异常",
                        event_code="chat.command.execute_failed",
                        plugin_name=plugin_name,
                        command_name=command_name,
                        command_class=command_class.__name__,
                    )

                    success = False
                    # Do not expose exception internals to chat participants.
                    response = "命令执行失败，请稍后重试"
                    intercept_message_level = message.intercept_message_level
                    continue_process = False
                    continue_flag, modified_message = await events_manager.handle_mai_events(
                        EventType.ON_COMMAND_AFTER_EXECUTE,
                        message,
                        extra_data={
                            **hook_context,
                            "success": success,
                            "response": response,
                            "intercept_message_level": intercept_message_level,
                            "continue_process": continue_process,
                        },
                    )
                    if not continue_flag:
                        continue_process = False
                    if modified_message:
                        _apply_modified_message(message, modified_message)
                        if "success" in modified_message.additional_data:
                            success = bool(modified_message.additional_data["success"])
                        if "response" in modified_message.additional_data:
                            response = modified_message.additional_data["response"]
                        if "intercept_message_level" in modified_message.additional_data:
                            intercept_message_level = int(modified_message.additional_data["intercept_message_level"])
                            message.intercept_message_level = intercept_message_level
                        if "continue_process" in modified_message.additional_data:
                            continue_process = bool(modified_message.additional_data["continue_process"])

                    if not success and not continue_process:
                        try:
                            await command_instance.send_text(f"命令执行出错: {response}")
                        except Exception:
                            logger.exception(
                                "命令错误提示发送失败",
                                event_code="chat.command.error_response_send_failed",
                                plugin_name=plugin_name,
                                command_name=command_name,
                            )

                    # 命令出错时，根据命令的拦截设置决定是否继续处理消息
                    return True, response, continue_process  # 出错时继续处理消息

            # 没有找到命令，继续处理消息
            return False, None, True

        except Exception:
            logger.exception("命令处理流程异常", event_code="chat.command.process_failed")
            return False, None, True  # 出错时继续处理消息

    async def handle_notice_message(self, message: MessageRecv):
        if message.message_info.message_id == "notice":
            message.is_notify = True
            logger.debug("通知消息开始处理", event_code="chat.notice.process_started")
            try:
                seg = message.message_segment
                mi = message.message_info
                sub_type = None
                scene = None
                msg_id = None
                recalled_id = None

                if getattr(seg, "type", None) == "notify" and isinstance(getattr(seg, "data", None), dict):
                    sub_type = seg.data.get("sub_type")
                    scene = seg.data.get("scene")
                    msg_id = seg.data.get("message_id")
                    recalled = seg.data.get("recalled_user_info") or {}
                    if isinstance(recalled, dict):
                        recalled_id = recalled.get("user_id")

                op = mi.user_info
                gid = mi.group_info.group_id if mi.group_info else None

                # 撤回事件：适配器下发的是 group_recall / friend_recall
                if sub_type in ("group_recall", "friend_recall", "recall"):
                    recalled_name = None
                    try:
                        if isinstance(recalled, dict):
                            recalled_name = (
                                recalled.get("user_cardname")
                                or recalled.get("user_nickname")
                                or str(recalled.get("user_id"))
                            )
                    except Exception:
                        pass

                    await self._handle_message_recall(msg_id)

                    if recalled_name and str(recalled_id) != str(getattr(op, "user_id", None)):
                        logger.info(
                            "消息撤回通知",
                            event_code="chat.notice.recall",
                            operator_id=getattr(op, "user_id", None),
                            recalled_user_id=recalled_id,
                            group_id=gid,
                            message_id=msg_id,
                        )
                    else:
                        logger.info(
                            "消息撤回通知",
                            event_code="chat.notice.recall",
                            operator_id=getattr(op, "user_id", None),
                            group_id=gid,
                            message_id=msg_id,
                        )
                else:
                    logger.debug(
                        "通知消息详情",
                        event_code="chat.notice.detail",
                        sub_type=sub_type,
                        scene=scene,
                        operator_id=getattr(op, "user_id", None),
                        group_id=gid,
                        message_id=msg_id,
                        recalled_user_id=recalled_id,
                    )
            except Exception:
                logger.exception("通知消息解析失败", event_code="chat.notice.parse_failed")

            return True

        return

    async def _handle_message_recall(self, message_id: Optional[str]) -> None:
        """处理消息撤回：登记 ID、标记数据库、清理归档。

        登记必须先于数据库操作，因为撤回可能早于消息落库；此时数据库无行可标记，
        需要由内存登记表在 store_message 阶段拦截补写。
        """
        if not message_id:
            logger.warning("撤回通知缺少消息 ID，无法定位目标消息", event_code="chat.recall.missing_id")
            return

        msg_id = str(message_id)

        # 1. 先登记到内存，兜住「撤回早于落库」的时序竞争
        matched = MessageStorage.mark_message_recalled(msg_id)
        recall_registry.register(msg_id, matched_in_db=matched)

        # 2. 清理第 0 层归档。归档在 _observe 早期发生，早于回复决策，
        #    查询层的过滤覆盖不到这份独立副本，必须主动删除。
        if _HAS_MEMORY_ARCHIVE and _MessageArchiver is not None:
            try:
                await _MessageArchiver().delete_by_message_id(msg_id)
            except Exception:
                logger.exception(
                    "清理撤回消息归档失败",
                    event_code="chat.recall.archive_cleanup_failed",
                    message_id=msg_id,
                )

    async def echo_message_process(self, raw_data: Dict[str, Any]) -> None:
        """
        用于专门处理回送消息ID的函数
        """
        message_data: Dict[str, Any] = raw_data.get("content", {})
        if not message_data:
            return
        message_type = message_data.get("type")
        if message_type != "echo":
            return
        mmc_message_id = message_data.get("echo")
        actual_message_id = message_data.get("actual_id")
        if MessageStorage.update_message(mmc_message_id, actual_message_id):
            logger.debug(
                "消息 ID 映射已更新",
                event_code="chat.message_id.updated",
                mmc_message_id=mmc_message_id,
                actual_message_id=actual_message_id,
            )
        else:
            logger.warning(
                "消息 ID 映射更新失败",
                event_code="chat.message_id.update_failed",
                mmc_message_id=mmc_message_id,
                actual_message_id=actual_message_id,
            )

    async def message_process(self, message_data: Dict[str, Any]) -> None:
        """处理转化后的统一格式消息
        这个函数本质是预处理一些数据，根据配置信息和消息内容，预处理消息，并分发到合适的消息处理器中
        heart_flow模式：使用思维流系统进行回复
        - 包含思维流状态管理
        - 在回复前进行观察和状态更新
        - 回复后更新思维流状态
        - 消息过滤
        - 记忆激活
        - 意愿计算
        - 消息生成和发送
        - 表情包处理
        - 性能计时
        """
        try:
            # 确保所有任务已启动
            await self._ensure_started()

            if message_data["message_info"].get("group_info") is not None:
                message_data["message_info"]["group_info"]["group_id"] = str(
                    message_data["message_info"]["group_info"]["group_id"]
                )
            if message_data["message_info"].get("user_info") is not None:
                message_data["message_info"]["user_info"]["user_id"] = str(
                    message_data["message_info"]["user_info"]["user_id"]
                )
            # print(message_data)
            # logger.debug(str(message_data))
            message = MessageRecv(message_data)
            group_info = message.message_info.group_info
            user_info = message.message_info.user_info

            continue_flag, modified_message = await events_manager.handle_mai_events(
                EventType.ON_MESSAGE_PRE_PROCESS, message
            )
            if not continue_flag:
                return
            _apply_modified_message(message, modified_message, merge_additional_data=True)

            continue_flag, modified_message = await events_manager.handle_mai_events(
                EventType.ON_MESSAGE_BEFORE_PROCESS, message
            )
            if not continue_flag:
                return
            _apply_modified_message(message, modified_message, merge_additional_data=True)

            if await self.handle_notice_message(message):
                return

            # 处理消息内容，生成纯文本
            await message.process(
                enable_heavy_media_analysis=False,
                enable_voice_transcription=False,
            )

            continue_flag, modified_message = await events_manager.handle_mai_events(
                EventType.ON_MESSAGE_AFTER_PROCESS, message
            )
            if not continue_flag:
                return
            _apply_modified_message(message, modified_message, merge_additional_data=True)

            # 平台层的 @ 检测由底层 is_mentioned_bot_in_message 统一处理；此处不做用户名硬编码匹配

            # 过滤检查
            if _check_ban_words(
                message.processed_plain_text,
                user_info,  # type: ignore
                group_info,
            ) or _check_ban_regex(
                message.raw_message,  # type: ignore
                user_info,  # type: ignore
                group_info,
            ):
                return

            get_chat_manager().register_message(message)

            chat = await get_chat_manager().get_or_create_stream(
                platform=message.message_info.platform,  # type: ignore
                user_info=user_info,  # type: ignore
                group_info=group_info,
            )

            message.update_chat_stream(chat)

            # if await self.check_ban_content(message):
            #     logger.warning(f"检测到消息中含有违法，色情，暴力，反动，敏感内容，消息内容：{message.processed_plain_text}，发送者：{message.message_info.user_info.user_nickname}")
            #     return

            # 命令处理 - 使用新插件系统检查并处理命令
            is_command, cmd_result, continue_process = await self._process_commands(message)

            # 如果是命令且不需要继续处理，则直接返回
            if is_command and not continue_process:
                await MessageStorage.store_message(message, chat)
                logger.info(
                    "命令处理完成，后续消息处理已跳过",
                    event_code="chat.command.completed_skip_message",
                    result=cmd_result,
                )
                return

            continue_flag, modified_message = await events_manager.handle_mai_events(EventType.ON_MESSAGE, message)
            if not continue_flag:
                return
            _apply_modified_message(message, modified_message)

            # 确认从接口发来的message是否有自定义的prompt模板信息
            if message.message_info.template_info and not message.message_info.template_info.template_default:
                template_group_name: Optional[str] = message.message_info.template_info.template_name  # type: ignore
                template_items = message.message_info.template_info.template_items
                if template_group_name and isinstance(template_items, dict):
                    prompt_manager.register_context_prompts(template_group_name, template_items)
                    for template_key, template in template_items.items():
                        logger.debug(
                            "消息自定义提示词模板已注册",
                            event_code="chat.prompt_template.registered",
                            template_key=template_key,
                            template_hash=hash_id(template),
                            template_length=len(str(template)),
                        )
            else:
                template_group_name = None

            async def preprocess():
                await self.heartflow_message_receiver.process_message(message)

            if template_group_name:
                async with prompt_manager.async_message_scope(template_group_name):
                    await preprocess()
            else:
                await preprocess()

        except Exception:
            logger.exception("消息预处理失败", event_code="chat.message.preprocess_failed")


# 创建全局ChatBot实例
chat_bot = ChatBot()
