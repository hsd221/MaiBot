from __future__ import annotations

import base64
import binascii
import dataclasses
import json
import math
import os
import re
import shutil
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from src.common.database.database import ROOT_PATH, db
from src.common.database.database_model import LLMRequestTrace, LLMRequestTraceMedia
from src.common.logger import get_logger
from src.config.api_ada_configs import ModelInfo
from src.llm_models.exceptions import (
    EmptyResponseException,
    NetworkConnectionError,
    ReqAbortException,
    RespNotOkException,
    RespParseException,
)
from src.llm_models.model_client.base_client import APIResponse
from src.llm_models.payload_content.message import Message
from src.llm_models.payload_content.resp_format import RespFormat
from src.llm_models.payload_content.tool_option import ToolCall, ToolOption

logger = get_logger("llm.request_trace")

DEFAULT_MAX_TRACE_RECORDS = 500
DEFAULT_MAX_PAYLOAD_CHARS = 256_000
DEFAULT_MAX_MEDIA_FILE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_MEDIA_TRACE_BYTES = 10 * 1024 * 1024
TRACE_PREVIEW_CHARS = 180
REDACTED_VALUE = "[REDACTED]"
TRACE_MEDIA_ROOT = Path(ROOT_PATH) / "data" / "llm_request_trace_media"

_MEDIA_ID_PATTERN = re.compile(r"^(?:image|audio)-[1-9]\d*$")
_MEDIA_FORMATS = {
    ("image", "png"): ("image/png", "png"),
    ("image", "jpeg"): ("image/jpeg", "jpg"),
    ("image", "gif"): ("image/gif", "gif"),
    ("image", "webp"): ("image/webp", "webp"),
    ("audio", "wav"): ("audio/wav", "wav"),
    ("audio", "mp3"): ("audio/mpeg", "mp3"),
    ("audio", "ogg"): ("audio/ogg", "ogg"),
    ("audio", "flac"): ("audio/flac", "flac"),
    ("audio", "mp4"): ("audio/mp4", "m4a"),
    ("audio", "webm"): ("audio/webm", "webm"),
    ("audio", "amr"): ("audio/amr", "amr"),
}

_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "auth_token",
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "client_secret",
    "password",
    "passwd",
    "secret",
    "cookie",
    "set_cookie",
}


@dataclasses.dataclass(frozen=True)
class TraceMediaInput:
    media_id: str
    kind: str
    format: str
    base64_data: str


def _normalized_key(value: Any) -> str:
    key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value).strip())
    return key.lower().replace("-", "_").replace(" ", "_")


def _is_sensitive_key(value: Any) -> bool:
    normalized = _normalized_key(value)
    return normalized in _SENSITIVE_KEYS or normalized.endswith(
        ("_api_key", "_token", "_secret", "_password", "_passwd")
    )


def _object_to_mapping(value: Any) -> dict[str, Any] | None:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json")
        except TypeError:
            dumped = model_dump()
        if isinstance(dumped, dict):
            return dumped
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        dumped = to_dict()
        if isinstance(dumped, dict):
            return dumped
    return None


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 24:
        return "[MAX_DEPTH]"
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return _json_safe(value.value, depth=depth + 1)
    if isinstance(value, bytes):
        return {"type": "bytes", "size_bytes": len(value)}
    if isinstance(value, dict):
        return {
            str(key): REDACTED_VALUE if _is_sensitive_key(key) else _json_safe(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item, depth=depth + 1) for item in value]

    mapping = _object_to_mapping(value)
    if mapping is not None:
        return _json_safe(mapping, depth=depth + 1)
    return {"type": type(value).__name__, "value": str(value)}


def serialize_trace_payload(payload: Any, *, max_chars: int = DEFAULT_MAX_PAYLOAD_CHARS) -> str:
    safe_payload = _json_safe(payload)
    serialized = json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) <= max_chars:
        return serialized

    limit = max(2, max_chars)

    def truncated_payload(preview_chars: int) -> str:
        return json.dumps(
            {
                "truncated": True,
                "original_characters": len(serialized),
                "preview": serialized[:preview_chars],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    empty_preview = truncated_payload(0)
    if len(empty_preview) > limit:
        minimal = json.dumps({"truncated": True}, separators=(",", ":"))
        return minimal if len(minimal) <= limit else "{}"

    low = 0
    high = min(len(serialized), limit)
    best = empty_preview
    while low <= high:
        preview_chars = (low + high) // 2
        candidate = truncated_payload(preview_chars)
        if len(candidate) <= limit:
            best = candidate
            low = preview_chars + 1
        else:
            high = preview_chars - 1
    return best


def _estimated_base64_bytes(value: str) -> int:
    padding = 2 if value.endswith("==") else 1 if value.endswith("=") else 0
    return max(0, (len(value) * 3) // 4 - padding)


def _serialize_tool_call(tool_call: ToolCall) -> dict[str, Any]:
    return {
        "id": tool_call.call_id,
        "name": tool_call.func_name,
        "arguments": tool_call.args or {},
    }


def _serialize_message(message: Message, image_index: int) -> tuple[dict[str, Any], int]:
    raw_content = message.content if isinstance(message.content, list) else [message.content]
    content: list[dict[str, Any]] = []
    for item in raw_content:
        if isinstance(item, str):
            content.append({"type": "text", "text": item})
        else:
            image_format, image_base64 = item
            image_index += 1
            content.append(
                {
                    "type": "image",
                    "media_id": f"image-{image_index}",
                    "format": image_format,
                    "base64_characters": len(image_base64),
                    "estimated_bytes": _estimated_base64_bytes(image_base64),
                }
            )
    return (
        {
            "role": message.role.value,
            "content": content,
            "tool_call_id": message.tool_call_id,
            "tool_calls": [_serialize_tool_call(call) for call in message.tool_calls or []],
        },
        image_index,
    )


def collect_request_media(*, messages: list[Message], audio_base64: str | None) -> list[TraceMediaInput]:
    media: list[TraceMediaInput] = []
    image_index = 0
    for message in messages:
        raw_content = message.content if isinstance(message.content, list) else [message.content]
        for item in raw_content:
            if isinstance(item, str):
                continue
            image_format, image_base64 = item
            image_index += 1
            normalized_format = str(image_format).strip().lower().removeprefix(".")
            if normalized_format == "jpg":
                normalized_format = "jpeg"
            media.append(
                TraceMediaInput(
                    media_id=f"image-{image_index}",
                    kind="image",
                    format=normalized_format,
                    base64_data=image_base64,
                )
            )
    if audio_base64:
        media.append(
            TraceMediaInput(
                media_id="audio-1",
                kind="audio",
                format="wav",
                base64_data=audio_base64,
            )
        )
    return media


def _serialize_tool_option(tool: ToolOption) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": [
            {
                "name": parameter.name,
                "type": parameter.param_type.value,
                "description": parameter.description,
                "required": parameter.required,
                "enum": parameter.enum_values,
            }
            for parameter in tool.params or []
        ],
    }


def build_request_payload(
    *,
    operation: str,
    model_info: ModelInfo,
    messages: list[Message],
    tool_options: list[ToolOption] | None,
    temperature: float | None,
    max_tokens: int | None,
    response_format: RespFormat | None,
    embedding_input: str | None,
    audio_base64: str | None,
    extra_params: dict[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "operation": operation,
        "model": {
            "name": model_info.name,
            "identifier": model_info.model_identifier,
            "provider": model_info.api_provider,
        },
        "parameters": {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": model_info.force_stream_mode,
            "response_format": response_format.to_dict() if response_format else None,
            "extra": extra_params or {},
        },
    }
    if operation == "response":
        serialized_messages: list[dict[str, Any]] = []
        image_index = 0
        for message in messages:
            serialized_message, image_index = _serialize_message(message, image_index)
            serialized_messages.append(serialized_message)
        payload["messages"] = serialized_messages
        payload["tools"] = [_serialize_tool_option(tool) for tool in tool_options or []]
    elif operation == "embedding":
        payload["input"] = embedding_input or ""
    elif operation == "audio":
        payload["audio"] = {
            "encoding": "base64",
            "media_id": "audio-1",
            "format": "wav",
            "base64_characters": len(audio_base64 or ""),
            "estimated_bytes": _estimated_base64_bytes(audio_base64 or ""),
        }
    return payload


def build_response_payload(response: APIResponse) -> dict[str, Any]:
    usage = response.usage
    return {
        "content": response.content,
        "reasoning_content": response.reasoning_content,
        "tool_calls": [_serialize_tool_call(call) for call in response.tool_calls or []],
        "embedding": response.embedding,
        "usage": (
            {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            }
            if usage
            else None
        ),
    }


def _first_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for preferred_key in ("text", "input", "messages", "content", "message"):
            if preferred_key in value:
                found = _first_text(value[preferred_key])
                if found:
                    return found
        for item in value.values():
            found = _first_text(item)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _first_text(item)
            if found:
                return found
    return ""


def _preview(value: Any) -> str:
    return " ".join(_first_text(value).split())[:TRACE_PREVIEW_CHARS]


def _safe_error_message(error: Exception) -> str:
    if isinstance(error, RespNotOkException):
        status_messages = {
            400: "模型服务拒绝了请求参数",
            401: "模型服务认证失败",
            402: "模型服务余额不足",
            403: "模型服务拒绝访问",
            404: "模型或接口不存在",
            413: "模型请求体过大",
            429: "模型服务请求过于频繁",
        }
        if error.status_code in status_messages:
            return status_messages[error.status_code]
        if error.status_code >= 500:
            return "模型服务暂时不可用"
        return f"模型服务返回 HTTP {error.status_code}"
    if isinstance(error, NetworkConnectionError):
        return "模型服务连接失败"
    if isinstance(error, EmptyResponseException):
        return "模型返回空响应"
    if isinstance(error, ReqAbortException):
        return "模型请求已取消"
    if isinstance(error, RespParseException):
        return "模型响应解析失败"
    return "模型请求失败，请查看服务端日志"


def is_valid_trace_media_id(media_id: str) -> bool:
    return len(media_id) <= 64 and bool(_MEDIA_ID_PATTERN.fullmatch(media_id))


def get_trace_media_spec(kind: str, media_format: str) -> tuple[str, str]:
    """返回白名单媒体格式对应的 MIME 类型和安全扩展名。"""
    media_spec = _MEDIA_FORMATS.get((kind, media_format))
    if media_spec is None:
        raise ValueError("unsupported trace media format")
    return media_spec


def _detect_image_format(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    return None


def detect_audio_format(data: bytes) -> str | None:
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav"
    if data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "mp3"
    if data.startswith(b"OggS"):
        return "ogg"
    if data.startswith(b"fLaC"):
        return "flac"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "mp4"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return "webm"
    if data.startswith((b"#!AMR\n", b"#!AMR-WB\n")):
        return "amr"
    return None


def _decode_trace_media(media: TraceMediaInput, *, max_bytes: int) -> tuple[bytes, str, str, str] | None:
    if not isinstance(media.base64_data, str) or len(media.base64_data) > max_bytes * 2 + 16:
        return None
    compact_base64 = "".join(media.base64_data.split())
    if not compact_base64 or _estimated_base64_bytes(compact_base64) > max_bytes:
        return None
    try:
        data = base64.b64decode(compact_base64, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not data or len(data) > max_bytes:
        return None

    if media.kind == "image":
        detected_format = _detect_image_format(data)
    elif media.kind == "audio":
        detected_format = detect_audio_format(data)
    else:
        return None
    if detected_format is None:
        return None
    mime_type, extension = get_trace_media_spec(media.kind, detected_format)
    return data, detected_format, mime_type, extension


def resolve_trace_media_path(
    media: LLMRequestTraceMedia,
    *,
    media_root: Path | None = None,
) -> Path:
    root = Path(TRACE_MEDIA_ROOT if media_root is None else media_root).resolve()
    trace_id = int(media.trace_id)
    if trace_id < 1 or not is_valid_trace_media_id(media.media_id):
        raise ValueError("invalid trace media identity")
    media_format = get_trace_media_spec(media.kind, media.format)
    expected_file_name = f"{media.media_id}.{media_format[1]}"
    if media.file_name != expected_file_name or Path(media.file_name).name != media.file_name:
        raise ValueError("invalid trace media file name")
    candidate = (root / str(trace_id) / media.file_name).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("trace media path escaped storage root")
    return candidate


class ModelRequestTraceRecorder:
    def __init__(
        self,
        *,
        max_records: int = DEFAULT_MAX_TRACE_RECORDS,
        max_payload_chars: int = DEFAULT_MAX_PAYLOAD_CHARS,
        media_root: Path | None = None,
        max_media_file_bytes: int = DEFAULT_MAX_MEDIA_FILE_BYTES,
        max_media_trace_bytes: int = DEFAULT_MAX_MEDIA_TRACE_BYTES,
    ) -> None:
        self.max_records = max(1, max_records)
        self.max_payload_chars = max(1_000, max_payload_chars)
        self.media_root = Path(TRACE_MEDIA_ROOT if media_root is None else media_root)
        self.max_media_file_bytes = max(1, max_media_file_bytes)
        self.max_media_trace_bytes = max(1, max_media_trace_bytes)
        try:
            db.create_tables([LLMRequestTrace, LLMRequestTraceMedia], safe=True)
        except Exception:
            logger.warning("模型请求追踪表初始化失败", event_code="llm.trace.table_init_failed", exc_info=True)

    def _persist_media(self, trace_id: int, request_media: list[TraceMediaInput]) -> None:
        remaining_bytes = self.max_media_trace_bytes
        stored_media_ids: set[str] = set()
        for media in request_media:
            if (
                remaining_bytes <= 0
                or media.media_id in stored_media_ids
                or not is_valid_trace_media_id(media.media_id)
            ):
                continue
            stored_media_ids.add(media.media_id)
            decoded = _decode_trace_media(
                media,
                max_bytes=min(self.max_media_file_bytes, remaining_bytes),
            )
            if decoded is None:
                logger.warning(
                    "模型请求追踪媒体未保存",
                    event_code="llm.trace.media_skipped",
                    trace_id=trace_id,
                    media_id=media.media_id,
                    media_kind=media.kind,
                )
                continue
            data, media_format, mime_type, extension = decoded
            file_name = f"{media.media_id}.{extension}"
            target_dir = (self.media_root.resolve() / str(trace_id)).resolve()
            if not target_dir.is_relative_to(self.media_root.resolve()):
                raise ValueError("trace media directory escaped storage root")
            target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name != "nt":
                target_dir.chmod(0o700)
            target_path = target_dir / file_name
            temporary_path = target_dir / f".{file_name}.tmp"
            try:
                open_flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                if hasattr(os, "O_NOFOLLOW"):
                    open_flags |= os.O_NOFOLLOW
                file_descriptor = os.open(temporary_path, open_flags, 0o600)
                with os.fdopen(file_descriptor, "wb") as media_file:
                    media_file.write(data)
                if os.name != "nt":
                    temporary_path.chmod(0o600)
                os.replace(temporary_path, target_path)
                LLMRequestTraceMedia.create(
                    trace_id=trace_id,
                    media_id=media.media_id,
                    kind=media.kind,
                    format=media_format,
                    mime_type=mime_type,
                    size_bytes=len(data),
                    file_name=file_name,
                )
                remaining_bytes -= len(data)
            except Exception:
                temporary_path.unlink(missing_ok=True)
                target_path.unlink(missing_ok=True)
                raise

    def _remove_media_directories(self, trace_ids: list[int]) -> None:
        storage_root = self.media_root.resolve()
        for trace_id in trace_ids:
            trace_directory = (storage_root / str(trace_id)).resolve()
            if not trace_directory.is_relative_to(storage_root):
                continue
            try:
                shutil.rmtree(trace_directory)
            except FileNotFoundError:
                continue
            except OSError:
                logger.warning(
                    "模型请求追踪媒体清理失败",
                    event_code="llm.trace.media_cleanup_failed",
                    trace_id=trace_id,
                    exc_info=True,
                )

    def _prune(self) -> None:
        excess = LLMRequestTrace.select().count() - self.max_records
        if excess <= 0:
            return
        old_ids = [
            row.id
            for row in LLMRequestTrace.select(LLMRequestTrace.id).order_by(LLMRequestTrace.id.asc()).limit(excess)
        ]
        if old_ids:
            LLMRequestTraceMedia.delete().where(LLMRequestTraceMedia.trace_id.in_(old_ids)).execute()
            LLMRequestTrace.delete().where(LLMRequestTrace.id.in_(old_ids)).execute()
            self._remove_media_directories(old_ids)

    def start_trace(
        self,
        *,
        request_type: str,
        operation: str,
        model_name: str,
        model_identifier: str,
        provider_name: str,
        attempt: int,
        request_payload: dict[str, Any],
        request_media: list[TraceMediaInput] | None = None,
    ) -> int | None:
        try:
            trace = LLMRequestTrace.create(
                request_type=request_type or "unknown",
                operation=operation,
                model_name=model_name,
                model_identifier=model_identifier,
                provider_name=provider_name,
                attempt=attempt,
                status="running",
                started_at=datetime.now(),
                request_preview=_preview(request_payload),
                request_payload=serialize_trace_payload(
                    request_payload,
                    max_chars=self.max_payload_chars,
                ),
            )
            if request_media:
                try:
                    self._persist_media(trace.id, request_media)
                except Exception:
                    logger.warning(
                        "模型请求追踪媒体写入失败",
                        event_code="llm.trace.media_write_failed",
                        trace_id=trace.id,
                        exc_info=True,
                    )
            self._prune()
            return trace.id
        except Exception:
            logger.warning("模型请求追踪写入失败", event_code="llm.trace.start_failed", exc_info=True)
            return None

    def finish_success(self, trace_id: int | None, response: APIResponse, *, duration_seconds: float) -> None:
        if trace_id is None:
            return
        try:
            response_payload = build_response_payload(response)
            usage = response.usage
            LLMRequestTrace.update(
                status="success",
                completed_at=datetime.now(),
                duration_ms=max(0, round(duration_seconds * 1000)),
                response_preview=_preview(response_payload),
                response_payload=serialize_trace_payload(
                    response_payload,
                    max_chars=self.max_payload_chars,
                ),
                error_type=None,
                error_message=None,
                status_code=None,
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                total_tokens=usage.total_tokens if usage else 0,
            ).where(LLMRequestTrace.id == trace_id).execute()
        except Exception:
            logger.warning("模型响应追踪写入失败", event_code="llm.trace.success_failed", exc_info=True)

    def finish_error(self, trace_id: int | None, error: Exception, *, duration_seconds: float) -> None:
        if trace_id is None:
            return
        try:
            status_code = getattr(error, "status_code", None)
            error_payload = {
                "error": {
                    "type": type(error).__name__,
                    "message": _safe_error_message(error),
                    "status_code": status_code if isinstance(status_code, int) else None,
                }
            }
            LLMRequestTrace.update(
                status="error",
                completed_at=datetime.now(),
                duration_ms=max(0, round(duration_seconds * 1000)),
                response_preview=_preview(error_payload),
                response_payload=serialize_trace_payload(
                    error_payload,
                    max_chars=self.max_payload_chars,
                ),
                error_type=type(error).__name__,
                error_message=_safe_error_message(error),
                status_code=status_code if isinstance(status_code, int) else None,
            ).where(LLMRequestTrace.id == trace_id).execute()
        except Exception:
            logger.warning("模型错误追踪写入失败", event_code="llm.trace.error_failed", exc_info=True)


model_request_trace_recorder = ModelRequestTraceRecorder()
