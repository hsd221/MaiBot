import re
import json
import asyncio
import contextvars
import functools
from typing import Union

from src.common.database.database_model import Messages, Images
from src.common.logger import get_logger
from .chat_stream import ChatStream
from .message import MessageSending, MessageRecv
from .recall_registry import recall_registry

logger = get_logger("message_storage")


async def _run_durable(func, *args):
    """Run a blocking write to completion even when the caller is cancelled.

    Cancelling ``asyncio.to_thread`` stops waiting while its worker thread keeps
    running.  That leaves a durable message write without
    the follow-up recall reconciliation.  An executor future plus shielding
    lets us absorb repeated cancellation until the worker result is collected.
    The second return value tells the caller to propagate cancellation after
    all required durable work has settled.
    Worker exceptions are retrieved and propagate to the caller.
    """

    context = contextvars.copy_context()
    callback = functools.partial(context.run, func, *args)
    future = asyncio.get_running_loop().run_in_executor(None, callback)
    was_cancelled = False
    while True:
        try:
            result = await asyncio.shield(future)
            return result, was_cancelled
        except asyncio.CancelledError:
            was_cancelled = True
            if future.done():
                return future.result(), was_cancelled
            continue


class MessageStorage:
    @staticmethod
    def _serialize_keywords(keywords) -> str:
        """将关键词列表序列化为JSON字符串"""
        if isinstance(keywords, list):
            return json.dumps(keywords, ensure_ascii=False)
        return "[]"

    @staticmethod
    def _deserialize_keywords(keywords_str: str) -> list:
        """将JSON字符串反序列化为关键词列表"""
        if not keywords_str:
            return []
        try:
            return json.loads(keywords_str)
        except (json.JSONDecodeError, TypeError):
            return []

    @staticmethod
    def _serialize_additional_config(additional_config) -> str:
        if isinstance(additional_config, str):
            return additional_config
        if isinstance(additional_config, dict):
            return json.dumps(additional_config, ensure_ascii=False)
        return "{}"

    @staticmethod
    def _serialize_selected_expressions(selected_expressions) -> str:
        if isinstance(selected_expressions, str):
            return selected_expressions
        if isinstance(selected_expressions, list):
            return json.dumps(selected_expressions, ensure_ascii=False)
        if selected_expressions is None:
            return ""
        return json.dumps(selected_expressions, ensure_ascii=False)

    @staticmethod
    async def store_message(message: Union[MessageSending, MessageRecv], chat_stream: ChatStream) -> None:
        """存储消息到数据库"""
        # 通知消息不存储
        if isinstance(message, MessageRecv) and message.is_notify:
            logger.debug("通知消息，跳过存储")
            return

        # 撤回事件可能先于消息落库抵达，此时数据库无行可标记。
        # 在写入前拦截，避免已撤回的消息被补写进库。
        incoming_msg_id = getattr(message.message_info, "message_id", None)
        if recall_registry.is_recalled(incoming_msg_id):
            recall_registry.resolve_pending(incoming_msg_id)
            logger.info(
                "消息已被撤回，跳过存储",
                event_code="chat.recall.store_skipped",
                message_id=incoming_msg_id,
            )
            return

        stored, cancelled = await _run_durable(MessageStorage._store_message_sync, message, chat_stream)
        if stored and recall_registry.is_recalled(message.message_info.message_id):
            marked, mark_cancelled = await _run_durable(
                MessageStorage.mark_message_recalled, message.message_info.message_id
            )
            cancelled |= mark_cancelled
            if marked:
                recall_registry.resolve_pending(message.message_info.message_id)
        if cancelled:
            raise asyncio.CancelledError
        if stored:
            from .media_background import backfill_stored_message

            await backfill_stored_message(message.message_info.message_id)

    @staticmethod
    def _store_message_sync(message: Union[MessageSending, MessageRecv], chat_stream: ChatStream) -> bool:
        try:
            pattern = r"<MainRule>.*?</MainRule>|<schedule>.*?</schedule>|<UserMessage>.*?</UserMessage>"

            # print(message)

            processed_plain_text = message.processed_plain_text

            # print(processed_plain_text)

            if processed_plain_text:
                processed_plain_text = MessageStorage.replace_image_descriptions(processed_plain_text)
                filtered_processed_plain_text = re.sub(pattern, "", processed_plain_text, flags=re.DOTALL)
            else:
                filtered_processed_plain_text = ""

            if isinstance(message, MessageSending):
                display_message = message.display_message
                if display_message:
                    filtered_display_message = re.sub(pattern, "", display_message, flags=re.DOTALL)
                else:
                    filtered_display_message = ""
                interest_value = 0
                is_mentioned = False
                is_at = False
                reply_probability_boost = 0.0
                reply_to = message.reply_to
                priority_mode = ""
                priority_info = {}
                is_emoji = False
                is_picid = False
                is_notify = False
                is_command = False
                key_words = ""
                key_words_lite = ""
                selected_expressions = MessageStorage._serialize_selected_expressions(message.selected_expressions)
                intercept_message_level = 0
            else:
                filtered_display_message = ""
                interest_value = message.interest_value
                is_mentioned = message.is_mentioned
                is_at = message.is_at
                reply_probability_boost = message.reply_probability_boost
                reply_to = ""
                priority_mode = message.priority_mode
                priority_info = message.priority_info
                is_emoji = message.is_emoji
                is_picid = message.is_picid
                is_notify = message.is_notify
                is_command = message.is_command
                intercept_message_level = getattr(message, "intercept_message_level", 0)
                # 序列化关键词列表为JSON字符串
                key_words = MessageStorage._serialize_keywords(message.key_words)
                key_words_lite = MessageStorage._serialize_keywords(message.key_words_lite)
                selected_expressions = ""

            chat_info_dict = chat_stream.to_dict()
            user_info_dict = message.message_info.user_info.to_dict()  # type: ignore

            # message_id 现在是 TextField，直接使用字符串值
            msg_id = message.message_info.message_id

            # 安全地获取 group_info, 如果为 None 则视为空字典
            group_info_from_chat = chat_info_dict.get("group_info") or {}
            # 安全地获取 user_info, 如果为 None 则视为空字典 (以防万一)
            user_info_from_chat = chat_info_dict.get("user_info") or {}

            Messages.create(
                message_id=msg_id,
                time=float(message.message_info.time),  # type: ignore
                chat_id=chat_stream.stream_id,
                # Flattened chat_info
                reply_to=reply_to,
                is_mentioned=is_mentioned,
                is_at=is_at,
                reply_probability_boost=reply_probability_boost,
                chat_info_stream_id=chat_info_dict.get("stream_id"),
                chat_info_platform=chat_info_dict.get("platform"),
                chat_info_user_platform=user_info_from_chat.get("platform"),
                chat_info_user_id=user_info_from_chat.get("user_id"),
                chat_info_user_nickname=user_info_from_chat.get("user_nickname"),
                chat_info_user_cardname=user_info_from_chat.get("user_cardname"),
                chat_info_group_platform=group_info_from_chat.get("platform"),
                chat_info_group_id=group_info_from_chat.get("group_id"),
                chat_info_group_name=group_info_from_chat.get("group_name"),
                chat_info_create_time=float(chat_info_dict.get("create_time", 0.0)),
                chat_info_last_active_time=float(chat_info_dict.get("last_active_time", 0.0)),
                # Flattened user_info (message sender)
                user_platform=user_info_dict.get("platform"),
                user_id=user_info_dict.get("user_id"),
                user_nickname=user_info_dict.get("user_nickname"),
                user_cardname=user_info_dict.get("user_cardname"),
                # Text content
                processed_plain_text=filtered_processed_plain_text,
                display_message=filtered_display_message,
                interest_value=interest_value,
                priority_mode=priority_mode,
                priority_info=priority_info,
                is_emoji=is_emoji,
                is_picid=is_picid,
                is_notify=is_notify,
                is_command=is_command,
                intercept_message_level=intercept_message_level,
                key_words=key_words,
                key_words_lite=key_words_lite,
                additional_config=MessageStorage._serialize_additional_config(
                    getattr(message.message_info, "additional_config", None)
                ),
                selected_expressions=selected_expressions,
            )
            return True
        except Exception:
            logger.exception("存储消息失败")
            return False

    # 如果需要其他存储相关的函数，可以在这里添加
    @staticmethod
    def update_message(mmc_message_id: str | None, qq_message_id: str | None) -> bool:
        """实时更新数据库的自身发送消息ID"""
        try:
            if not qq_message_id:
                logger.info("消息不存在message_id，无法更新")
                return False
            if matched_message := (
                Messages.select().where((Messages.message_id == mmc_message_id)).order_by(Messages.time.desc()).first()
            ):
                # 更新找到的消息记录
                Messages.update(message_id=qq_message_id).where(Messages.id == matched_message.id).execute()  # type: ignore
                logger.debug(f"更新消息ID成功: {matched_message.message_id} -> {qq_message_id}")
                return True
            else:
                logger.debug("未找到匹配的消息")
                return False

        except Exception as e:
            logger.error(f"更新消息ID失败: {e}")
            return False

    @staticmethod
    def mark_message_recalled(message_id: str | None) -> bool:
        """将指定消息标记为已撤回。

        返回是否在数据库中匹配到了对应行。未匹配通常意味着消息尚未落库，
        调用方应保留内存登记，等待 store_message 阶段拦截。
        """
        if not message_id:
            return False
        try:
            updated = (
                Messages.update(is_recalled=True)
                .where((Messages.message_id == str(message_id)) & (Messages.message_id != "notice"))
                .execute()
            )
            if updated:
                logger.info(
                    "消息已标记为撤回",
                    event_code="chat.recall.marked",
                    message_id=message_id,
                    rows=updated,
                )
                return True
            logger.debug(
                "撤回消息尚未落库，等待写入时拦截",
                event_code="chat.recall.mark_deferred",
                message_id=message_id,
            )
            return False
        except Exception:
            logger.exception(
                "标记消息撤回失败",
                event_code="chat.recall.mark_failed",
                message_id=message_id,
            )
            return False

    @staticmethod
    def replace_image_descriptions(text: str) -> str:
        """将[图片：描述]替换为[picid:image_id]"""
        pattern = r"\[图片：([^\]]+)\]"
        descriptions = {match.strip() for match in re.findall(pattern, text)}
        if not descriptions:
            return text
        try:
            # A description is not an identity. Only resolve an unambiguous match.
            matches: dict[str, set[str]] = {}
            for row in Images.select(Images.description, Images.image_id).where(Images.description.in_(descriptions)):
                matches.setdefault(row.description, set()).add(row.image_id)
        except Exception:
            return text

        def replace_match(match):
            image_ids = matches.get(match.group(1).strip(), set())
            return f"[picid:{next(iter(image_ids))}]" if len(image_ids) == 1 else match.group(0)

        return re.sub(pattern, replace_match, text)
