import asyncio
import base64
import contextlib
import hashlib
import re
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Optional

from src.chat.emoji_system.emoji_description import is_semantic_emoji_description
from src.chat.emoji_system.emoji_usage_scene import schedule_emoji_usage_scene_learning
from src.chat.utils.utils_image import (
    VISION_DESCRIPTION_CACHE_TYPE,
    get_image_manager,
    is_animated_image_base64_data,
    read_gif_description_cache,
)
from src.chat.utils.utils_voice import get_voice_text
from src.common.background_tasks import spawn_background_task
from src.common.database.database_model import Emoji, EmojiDescriptionCache, ImageDescriptions, Messages
from src.common.logger import get_logger

logger = get_logger("chat_media_background")


_MEDIA_PLACEHOLDERS = {
    "image": "[图片]",
    "emoji": "[表情包]",
    "voice": "[语音消息]",
}


@dataclass
class _MediaTaskState:
    kind: str
    media_hash: str
    status: str = "pending"
    result_text: Optional[str] = None
    task: Optional[asyncio.Task] = None
    message_refs: list[tuple[str, int, str | None]] = field(default_factory=list)


@dataclass
class _MessageMediaRef:
    kind: str
    task_key: str
    occurrence_index: int
    chat_id: str | None = None


# 下面两个结构是识别结果缓存，键（媒体哈希 / message_id）随消息量无限增长，
# 必须设上界，否则长期运行的进程会把每条历史消息的媒体状态永久留在内存里。
_MAX_TRACKED_MEDIA_TASKS = 2048
_MAX_TRACKED_MESSAGES = 2048
_MAX_MEDIA_REFS_PER_MESSAGE = 256

_media_task_states: "OrderedDict[str, _MediaTaskState]" = OrderedDict()
_message_media_refs: "OrderedDict[str, list[_MessageMediaRef]]" = OrderedDict()
# 回填锁则是纯粹的临时互斥，用引用计数在最后一个使用者退出时立即回收。
_backfill_locks: dict[str, asyncio.Lock] = {}
_backfill_lock_users: dict[str, int] = {}
_media_analysis_semaphore = asyncio.Semaphore(3)


def _trim_media_task_states() -> None:
    """裁剪识别缓存，优先淘汰最旧且已结束的任务。"""
    if len(_media_task_states) <= _MAX_TRACKED_MEDIA_TASKS:
        return
    # 正在识别中的条目不能淘汰：_run_media_task 结束后还要回写状态供复用。
    evictable = [key for key, state in _media_task_states.items() if state.status != "processing"]
    for key in evictable[: len(_media_task_states) - _MAX_TRACKED_MEDIA_TASKS]:
        _media_task_states.pop(key, None)


def _trim_message_media_refs() -> None:
    while len(_message_media_refs) > _MAX_TRACKED_MESSAGES:
        message_id, refs = _message_media_refs.popitem(last=False)
        for ref in refs:
            _remove_state_message_ref(message_id, ref)


def _remove_state_message_ref(message_id: str, ref: _MessageMediaRef) -> None:
    state = _media_task_states.get(ref.task_key)
    if state is not None:
        with contextlib.suppress(ValueError):
            state.message_refs.remove((message_id, ref.occurrence_index, ref.chat_id))


def _hash_media_data(media_data: str) -> str:
    normalized_data = media_data.encode("ascii", errors="ignore").decode("ascii")
    try:
        media_bytes = base64.b64decode(normalized_data)
    except Exception:
        media_bytes = normalized_data.encode("utf-8", errors="ignore")
    return hashlib.md5(media_bytes, usedforsecurity=False).hexdigest()


def _make_task_key(kind: str, media_data: str) -> str:
    return f"{kind}:{_hash_media_data(media_data)}"


def _is_successful_result(kind: str, result_text: Optional[str]) -> bool:
    if not result_text:
        return False
    if kind == "image":
        return result_text.startswith("[图片：")
    if kind == "emoji":
        return result_text.startswith("[表情包：")
    if kind == "voice":
        return result_text.startswith("[语音：")
    return False


def _format_cached_result(kind: str, description: str | None) -> Optional[str]:
    if not description:
        return None
    description = str(description).strip()
    if not description:
        return None
    if kind == "image":
        return description if description.startswith("[图片：") else f"[图片：{description}]"
    if kind == "emoji":
        return description if description.startswith("[表情包：") else f"[表情包：{description}]"
    if kind == "voice":
        return description if description.startswith("[语音：") else f"[语音：{description}]"
    return None


def _load_cached_media_result(kind: str, media_hash: str, media_data: Optional[str] = None) -> Optional[str]:
    """Best-effort persistent cache lookup for media tasks.

    Image and emoji tasks may only reuse business caches after the shared raw
    visual observation exists. Voice transcription has no persistent cache yet.
    """
    try:
        if kind not in {"image", "emoji"}:
            return None

        vision_record = ImageDescriptions.get_or_none(
            (ImageDescriptions.image_description_hash == media_hash)
            & (ImageDescriptions.type == VISION_DESCRIPTION_CACHE_TYPE)
        )
        vision_description = getattr(vision_record, "description", None)
        if not vision_description or not str(vision_description).strip():
            return None

        if kind == "image":
            return _format_cached_result("image", vision_description)

        if kind == "emoji":
            if media_data and is_animated_image_base64_data(media_data):
                cache_record = EmojiDescriptionCache.get_or_none(EmojiDescriptionCache.emoji_hash == media_hash)
                cached_description = read_gif_description_cache(getattr(cache_record, "description", None))
                if not cached_description or not is_semantic_emoji_description(cached_description):
                    return None
                return _format_cached_result("emoji", cached_description)

            emoji_record = Emoji.get_or_none(Emoji.emoji_hash == media_hash)
            registered_description = getattr(emoji_record, "description", None)
            if registered_description and is_semantic_emoji_description(registered_description):
                return _format_cached_result("emoji", registered_description)

            cache_record = EmojiDescriptionCache.get_or_none(EmojiDescriptionCache.emoji_hash == media_hash)
            cached_description = getattr(cache_record, "description", None)
            if cached_description and is_semantic_emoji_description(cached_description):
                return _format_cached_result("emoji", cached_description)
    except Exception as e:
        logger.debug(f"读取{kind}持久缓存失败，继续后台识别: {e}")

    return None


def _media_token_pattern(kind: str) -> str:
    if kind == "image":
        return r"\[图片(?:：[^\]]+)?\]"
    if kind == "emoji":
        return r"\[表情包(?:：[^\]]+)?\]"
    return r"\[(?:语音消息|语音：[^\]]+)\]"


def _replace_placeholder_occurrence(kind: str, content: str, result_text: str, occurrence_index: int) -> str:
    placeholder = _MEDIA_PLACEHOLDERS[kind]
    for index, match in enumerate(re.finditer(_media_token_pattern(kind), content)):
        if index != occurrence_index:
            continue
        if match.group(0) != placeholder:
            return content
        return f"{content[: match.start()]}{result_text}{content[match.end() :]}"
    return content


def _remember_message_ref(
    message_id: Optional[str],
    task_key: str,
    state: _MediaTaskState,
    chat_id: str | None = None,
) -> None:
    if not message_id:
        return
    message_id = str(message_id)
    normalized_chat_id = str(chat_id or "").strip() or None
    refs = _message_media_refs.setdefault(message_id, [])
    _message_media_refs.move_to_end(message_id)
    _trim_message_media_refs()
    occurrence_index = (
        max(
            (ref.occurrence_index for ref in refs if ref.kind == state.kind and ref.chat_id == normalized_chat_id),
            default=-1,
        )
        + 1
    )
    refs.append(
        _MessageMediaRef(
            kind=state.kind,
            task_key=task_key,
            occurrence_index=occurrence_index,
            chat_id=normalized_chat_id,
        )
    )
    state.message_refs.append((message_id, occurrence_index, normalized_chat_id))
    while len(refs) > _MAX_MEDIA_REFS_PER_MESSAGE:
        removed_ref = refs.pop(0)
        _remove_state_message_ref(message_id, removed_ref)


def _schedule_placeholder_backfill(kind: str, message_id: str, result_text: str, occurrence_index: int) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("当前没有运行中的事件循环，跳过媒体占位回填调度")
        return
    # 必须持有强引用：事件循环只弱引用任务，丢弃返回值会让回填在执行中途被回收，
    # 表现为图片/语音描述偶尔不落库。
    spawn_background_task(
        _backfill_message_placeholder(kind, message_id, result_text, occurrence_index),
        name=f"media-backfill-{kind}",
    )


@contextlib.asynccontextmanager
async def _backfill_lock(message_id: str) -> AsyncIterator[None]:
    """按 message_id 串行化回填，并在最后一个使用者退出时回收锁。

    锁只在回填期间有意义，若按 message_id 无限留存，每条含媒体的消息都会
    永久占用一个 Lock 对象。引用计数的增减之间没有 await，不会与其他协程交错。
    """
    lock = _backfill_locks.get(message_id)
    if lock is None:
        lock = asyncio.Lock()
        _backfill_locks[message_id] = lock
    _backfill_lock_users[message_id] = _backfill_lock_users.get(message_id, 0) + 1
    try:
        async with lock:
            yield
    finally:
        remaining = _backfill_lock_users.get(message_id, 1) - 1
        if remaining > 0:
            _backfill_lock_users[message_id] = remaining
        else:
            _backfill_lock_users.pop(message_id, None)
            _backfill_locks.pop(message_id, None)


async def _backfill_message_placeholder(kind: str, message_id: str, result_text: str, occurrence_index: int) -> None:
    async with _backfill_lock(message_id):
        for attempt in range(6):
            try:
                message_record = (
                    Messages.select().where(Messages.message_id == message_id).order_by(Messages.time.desc()).first()
                )
                if not message_record:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue

                processed_text = message_record.processed_plain_text or ""
                backfilled_text = _replace_placeholder_occurrence(kind, processed_text, result_text, occurrence_index)
                if backfilled_text == processed_text:
                    return

                message_record.processed_plain_text = backfilled_text
                # Recall markers can be updated by a storage worker after this
                # row was read; only write the field owned by media backfill.
                message_record.save(only=[Messages.processed_plain_text])
                logger.debug(f"后台媒体识别完成，已回填消息 {message_id} 的 {kind} 占位")
                return
            except Exception as e:
                logger.warning(f"回填消息 {message_id} 的 {kind} 占位失败: {e}")
                await asyncio.sleep(0.5 * (attempt + 1))


async def backfill_stored_message(message_id: str | None) -> None:
    """Retry completed media once storage finishes, even after the early retry window."""
    if not message_id:
        return
    for ref in list(_message_media_refs.get(str(message_id), [])):
        state = _media_task_states.get(ref.task_key)
        if state is not None and state.status == "done" and state.result_text:
            await _backfill_message_placeholder(ref.kind, str(message_id), state.result_text, ref.occurrence_index)


async def _analyze_media(kind: str, media_data: str) -> Optional[str]:
    if kind == "image":
        return await get_image_manager().get_image_description(media_data)
    if kind == "emoji":
        return await get_image_manager().get_emoji_description(media_data)
    if kind == "voice":
        return await get_voice_text(media_data)
    return None


async def _run_media_task(task_key: str, media_data: str) -> None:
    state = _media_task_states.get(task_key)
    if state is None:
        # 裁剪不会淘汰 processing 中的条目，走到这里说明状态已被外部清理，直接放弃。
        logger.debug(f"媒体识别任务状态已不存在，跳过: {task_key}")
        return
    try:
        async with _media_analysis_semaphore:
            result_text = await _analyze_media(state.kind, media_data)
        if not _is_successful_result(state.kind, result_text):
            state.status = "failed"
            logger.warning(f"后台{state.kind}识别未得到可用结果，保留原始占位")
            return

        state.result_text = result_text
        state.status = "done"
        for message_id, occurrence_index, chat_id in list(state.message_refs):
            _schedule_placeholder_backfill(state.kind, message_id, result_text, occurrence_index)
            if state.kind == "emoji":
                schedule_emoji_usage_scene_learning(
                    state.media_hash,
                    message_id,
                    occurrence_index,
                    result_text,
                    chat_id=chat_id,
                )
    except Exception as e:
        state.status = "failed"
        logger.warning(f"后台{state.kind}识别任务失败: {e}")
    finally:
        state.task = None


def _schedule_media_task(
    kind: str,
    media_data: str,
    message_id: Optional[str],
    *,
    chat_id: str | None = None,
) -> None:
    if not media_data:
        return

    media_hash = _hash_media_data(media_data)
    task_key = f"{kind}:{media_hash}"
    state = _media_task_states.get(task_key)
    if state is None:
        state = _MediaTaskState(kind=kind, media_hash=media_hash)
        _media_task_states[task_key] = state
        _trim_media_task_states()
    else:
        _media_task_states.move_to_end(task_key)

    _remember_message_ref(message_id, task_key, state, chat_id)

    if state.status in {"pending", "failed"} and (
        cached_result := _load_cached_media_result(kind, media_hash, media_data)
    ):
        state.status = "done"
        state.result_text = cached_result
        if message_id:
            occurrence_index = _message_media_refs[str(message_id)][-1].occurrence_index
            _schedule_placeholder_backfill(kind, str(message_id), cached_result, occurrence_index)
            if kind == "emoji":
                schedule_emoji_usage_scene_learning(
                    media_hash,
                    str(message_id),
                    occurrence_index,
                    cached_result,
                    chat_id=chat_id,
                )
        return

    if state.status == "done" and state.result_text and message_id:
        occurrence_index = _message_media_refs[str(message_id)][-1].occurrence_index
        _schedule_placeholder_backfill(kind, str(message_id), state.result_text, occurrence_index)
        if kind == "emoji":
            schedule_emoji_usage_scene_learning(
                media_hash,
                str(message_id),
                occurrence_index,
                state.result_text,
                chat_id=chat_id,
            )
        return

    if state.status == "processing" and state.task:
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("当前没有运行中的事件循环，跳过后台媒体识别调度")
        return

    state.status = "processing"
    state.task = loop.create_task(_run_media_task(task_key, media_data))


def schedule_image_description_task(image_base64: str, message_id: Optional[str] = None) -> None:
    _schedule_media_task("image", image_base64, message_id)


def schedule_emoji_description_task(
    emoji_base64: str,
    message_id: Optional[str] = None,
    *,
    chat_id: str | None = None,
) -> None:
    _schedule_media_task("emoji", emoji_base64, message_id, chat_id=chat_id)


def schedule_voice_transcription_task(voice_base64: str, message_id: Optional[str] = None) -> None:
    _schedule_media_task("voice", voice_base64, message_id)


def enhance_media_placeholders(message_id: Optional[str], content: Optional[str]) -> str:
    if not content:
        return ""
    if not message_id:
        return content

    enhanced_content = content
    for media_ref in _message_media_refs.get(str(message_id), []):
        state = _media_task_states.get(media_ref.task_key)
        if not state or state.status != "done" or not state.result_text:
            continue
        enhanced_content = _replace_placeholder_occurrence(
            media_ref.kind,
            enhanced_content,
            state.result_text,
            media_ref.occurrence_index,
        )

    return enhanced_content
