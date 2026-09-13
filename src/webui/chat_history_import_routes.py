"""Authenticated WebUI routes for chat-history learning imports."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, Callable, Coroutine, Literal, Optional

from fastapi import APIRouter, Cookie, File, Header, HTTPException, Query, UploadFile

from src.bw_learner.history_enrichment import (
    find_history_profile_conflicts,
    load_history_candidate_evidence,
    load_history_enrichment_evidence,
    store_history_enrichment,
)
from src.bw_learner.history_import import ChatHistoryFormatError, analyze_qq_chat_export, index_history_windows
from src.bw_learner.history_learning import (
    DEFAULT_HISTORY_EXTRACTION_CONCURRENCY,
    DEPTH_WINDOW_BUDGETS,
    ChatHistoryLearner,
    HistoryCandidates,
    HistoryLearningCancelled,
    HistoryLearningResult,
    group_chat_id,
    history_candidates_from_json,
    store_history_candidates,
)
from src.common.database.database_model import ChatHistoryImportTask
from src.common.logger import get_logger
from src.webui.auth import verify_auth_token_from_cookie_or_header
from src.webui.chat_history_candidate_catalog import (
    CandidateCatalogUnavailableError,
    page_candidate_catalog,
    write_candidate_catalog,
)
from src.webui.chat_history_checkpoint import (
    EXTRACTION_CHECKPOINT_FILENAME,
    PENDING_RESULT_FILENAME,
    PENDING_RESULT_TEMP_FILENAME,
    HistoryCheckpointUnavailableError,
    fsync_directory,
    load_pending_learning_result,
    remove_history_resume_artifacts,
    write_pending_learning_result,
)
from src.webui.chat_history_import_schemas import (
    MAX_PARTICIPANT_SELECTION_OVERRIDES as MAX_PARTICIPANT_SELECTION_OVERRIDES,
    ChatHistoryAnalysisResponse,
    ChatHistoryCandidateListResponse,
    ChatHistoryCandidatePagination,
    ChatHistoryImportDeleteResponse,
    ChatHistoryImportListResponse,
    ChatHistoryImportProgress,
    ChatHistoryImportResponse,
    ChatHistoryImportResume,
    ChatHistoryImportStartRequest,
    ChatHistoryParticipantListResponse,
    ChatHistoryParticipantPagination,
    ChatHistoryParticipantScopeRequest as ChatHistoryParticipantScopeRequest,
    ChatHistoryProfileDecisionRequest,
    ImportedChatResponse as ImportedChatResponse,
    ImportedParticipantResponse,
)
from src.webui.chat_history_resume import (
    ExtractionCheckpointSession,
    can_resume_task,
    has_submitted_profile_decisions,
    mark_task_failed,
    store_core_candidates_once,
    validate_learning_resume_artifacts,
)
from src.webui.error_utils import internal_server_error, log_exception_type


logger = get_logger("webui.chat_history_import")
router = APIRouter(prefix="/chat-history-imports", tags=["Chat history imports"])

IMPORT_ROOT = Path(__file__).resolve().parents[2] / "data" / "chat_history_imports"
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
MAX_CONCURRENT_IMPORTS = 1
MAX_PARTICIPANT_PREVIEW = 30
_IMPORT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_KNOWN_TASK_FILES = (
    "source.json",
    "normalized.jsonl",
    "result.json",
    "result.json.tmp",
    "candidate_catalog.jsonl",
    "candidate_catalog.jsonl.tmp",
    EXTRACTION_CHECKPOINT_FILENAME,
    PENDING_RESULT_FILENAME,
    PENDING_RESULT_TEMP_FILENAME,
)
_NON_CANCELLABLE_PROGRESS_STAGES = frozenset({"storing_catalog", "storing", "storing_enrichment"})
_running_tasks: dict[str, asyncio.Task[None]] = {}
_analyzing_import_ids: set[str] = set()


def verify_auth_token(maibot_session: Optional[str] = None, authorization: Optional[str] = None) -> bool:
    return verify_auth_token_from_cookie_or_header(maibot_session, authorization)


def _safe_source_name(filename: str | None) -> str:
    normalized = (filename or "history.json").replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    cleaned = "".join(character for character in normalized if character >= " " and character != "\x7f").strip()
    return (cleaned or "history.json")[:128]


def _task_dir(import_id: str) -> Path:
    if not _IMPORT_ID_RE.fullmatch(import_id):
        raise ValueError("invalid import id")
    root = IMPORT_ROOT.resolve()
    task_dir = (root / import_id).resolve()
    if task_dir.parent != root:
        raise ValueError("invalid import path")
    return task_dir


def _cleanup_task_files(import_id: str, *, remove_directory: bool = True) -> None:
    try:
        task_dir = _task_dir(import_id)
    except ValueError:
        return
    for filename in _KNOWN_TASK_FILES:
        try:
            (task_dir / filename).unlink(missing_ok=True)
        except OSError as error:
            log_exception_type(logger, "清理聊天记录导入文件失败", error, import_id=import_id)
    if remove_directory:
        try:
            task_dir.rmdir()
        except FileNotFoundError:
            pass
        except OSError as error:
            log_exception_type(logger, "清理聊天记录导入目录失败", error, import_id=import_id)


def _active_local_import_count() -> int:
    running_count = sum(not task.done() for task in _running_tasks.values())
    return len(_analyzing_import_ids) + running_count


def _register_running_task(import_id: str, coroutine: Coroutine[Any, Any, None], task_name: str) -> asyncio.Task[None]:
    """登记后台任务，并在结束时只注销自己，避免误删同 ID 的新任务。"""

    background = asyncio.create_task(coroutine, name=task_name)
    _running_tasks[import_id] = background

    def _discard(finished: asyncio.Task[None]) -> None:
        # done callback 由事件循环延迟调度，此时同一 import_id 可能已登记新任务；
        # 必须按对象身份比对，否则会把正在运行的任务从登记表中抹掉。
        if _running_tasks.get(import_id) is finished:
            del _running_tasks[import_id]

    background.add_done_callback(_discard)
    return background


def _load_json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _can_resume_task(task: ChatHistoryImportTask, options: dict[str, Any]) -> bool:
    try:
        return can_resume_task(task, _task_dir(task.import_id), options)
    except ValueError:
        return False


def _task_to_response(task: ChatHistoryImportTask) -> ChatHistoryImportResponse:
    analysis = _load_json_object(task.analysis_json)
    options = _load_json_object(task.options_json)
    estimates = analysis.pop("estimated_model_calls", {})
    participants = analysis.get("participants", [])
    if isinstance(participants, list):
        analysis["participant_count"] = int(analysis.get("participant_count") or len(participants))
        analysis["eligible_participant_count"] = int(
            analysis.get("eligible_participant_count")
            or sum(1 for participant in participants if isinstance(participant, dict) and not participant.get("is_bot"))
        )
        analysis["participants"] = participants[:MAX_PARTICIPANT_PREVIEW]
    result = _load_json_object(task.result_json) if task.result_json else None
    return ChatHistoryImportResponse(
        import_id=task.import_id,
        source_name=task.source_name,
        source_size=task.source_size,
        status=task.status,
        chat_id=task.chat_id,
        analysis=ChatHistoryAnalysisResponse.model_validate(analysis) if analysis else None,
        estimated_model_calls={str(key): int(value) for key, value in estimates.items()},
        progress=ChatHistoryImportProgress(
            stage=task.progress_stage,
            current=max(0, task.progress_current),
            total=max(1, task.progress_total),
        ),
        resume=ChatHistoryImportResume(
            can_resume=_can_resume_task(task, options),
            stage=task.resume_stage,
            completed_windows=max(0, task.checkpoint_window_count),
            attempt_count=max(0, task.attempt_count),
        ),
        options=options,
        result=result,
        error_message=task.error_message,
        created_at=task.created_at,
        updated_at=task.updated_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
    )


async def _stream_upload(upload: UploadFile, destination: Path) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    try:
        with destination.open("xb") as output:
            destination.chmod(0o600)
            while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="聊天记录文件不能超过 100MB")
                digest.update(chunk)
                output.write(chunk)
    finally:
        await upload.close()
    if size == 0:
        raise HTTPException(status_code=400, detail="聊天记录文件为空")
    return size, digest.hexdigest()


def _analysis_payload(analysis: Any, total_window_count: int) -> dict[str, Any]:
    payload = analysis.to_json()
    payload.pop("normalized_path", None)
    payload["participants"] = list(payload.get("participants", ()))
    payload["participant_count"] = len(payload["participants"])
    payload["eligible_participant_count"] = sum(
        1 for participant in payload["participants"] if isinstance(participant, dict) and not participant.get("is_bot")
    )
    payload["total_window_count"] = total_window_count
    payload["estimated_model_calls"] = {
        depth: (total_window_count if budget is None else min(total_window_count, budget)) + 1
        for depth, budget in DEPTH_WINDOW_BUDGETS.items()
    }
    payload["estimated_model_call_note"] = (
        "每个入选自然窗口至少 1 次提取、至少 1 次跨窗口合并；边界续接或候选分层合并会追加调用。"
    )
    return payload


def _resolve_participant_scope(
    request_body: ChatHistoryImportStartRequest,
    participants: list[Any],
) -> dict[str, Any]:
    human_ids = {
        str(participant.get("source_id"))
        for participant in participants
        if isinstance(participant, dict) and not participant.get("is_bot") and participant.get("source_id")
    }
    requested = request_body.participant_scope
    if requested is None:
        legacy_ids = list(dict.fromkeys(item.strip() for item in request_body.participant_ids if item.strip()))
        mode = "custom" if legacy_ids else "all"
        included_ids = legacy_ids
        excluded_ids: list[str] = []
    else:
        mode = requested.mode
        included_ids = list(dict.fromkeys(item.strip() for item in requested.included_ids if item.strip()))
        excluded_ids = list(dict.fromkeys(item.strip() for item in requested.excluded_ids if item.strip()))

    if mode == "all":
        if included_ids:
            raise HTTPException(status_code=422, detail="全部成员模式不能同时提交包含列表")
        unknown = set(excluded_ids) - human_ids
        if unknown:
            raise HTTPException(status_code=422, detail="排除的参与者不在聊天记录中")
        if human_ids and len(excluded_ids) >= len(human_ids):
            raise HTTPException(status_code=422, detail="至少保留一名参与学习的成员")
        return {"mode": "all", "excluded_ids": excluded_ids}

    if excluded_ids:
        raise HTTPException(status_code=422, detail="自定义成员模式不能同时提交排除列表")
    unknown = set(included_ids) - human_ids
    if unknown:
        raise HTTPException(status_code=422, detail="选择的参与者不在聊天记录中")
    if not included_ids:
        raise HTTPException(status_code=422, detail="自定义成员模式至少选择一名成员")
    return {"mode": "custom", "included_ids": included_ids}


def _get_task_or_404(import_id: str) -> ChatHistoryImportTask:
    task = ChatHistoryImportTask.get_or_none(ChatHistoryImportTask.import_id == import_id)
    if task is None:
        raise HTTPException(status_code=404, detail="导入任务不存在")
    _reconcile_interrupted_task(task)
    return task


def _reconcile_interrupted_task(task: ChatHistoryImportTask) -> None:
    is_interrupted_analysis = task.status == "analyzing" and task.import_id not in _analyzing_import_ids
    is_interrupted_learning = task.status == "running" and task.import_id not in _running_tasks
    if not (is_interrupted_analysis or is_interrupted_learning):
        return
    if is_interrupted_analysis:
        _cleanup_task_files(task.import_id, remove_directory=False)
        task.source_path = ""
        task.normalized_path = ""
        mark_task_failed(task, "任务在文件分析时因服务重启而中断，请删除后重新导入", resume_stage="analyzing")
        return

    resume_stage = "profile_commit" if has_submitted_profile_decisions(task) else task.progress_stage
    mark_task_failed(
        task,
        "任务因服务重启而中断，已保留最近断点，可以继续执行",
        resume_stage=resume_stage,
    )


@router.post("", response_model=ChatHistoryImportResponse)
async def create_chat_history_import(
    file: Annotated[UploadFile, File(description="QQChatExporter JSON 群聊记录")],
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> ChatHistoryImportResponse:
    verify_auth_token(maibot_session, authorization)
    source_name = _safe_source_name(file.filename)
    if not source_name.casefold().endswith(".json"):
        await file.close()
        raise HTTPException(status_code=400, detail="仅支持 JSON 聊天记录文件")
    content_type = file.content_type.split(";", maxsplit=1)[0].strip().casefold() if file.content_type else None
    if content_type not in {None, "application/json", "text/json", "application/octet-stream"}:
        await file.close()
        raise HTTPException(status_code=400, detail="仅支持 JSON 聊天记录文件")
    if _active_local_import_count() >= MAX_CONCURRENT_IMPORTS:
        await file.close()
        raise HTTPException(status_code=409, detail="已有聊天记录导入或学习任务正在运行")

    import_id = uuid.uuid4().hex
    IMPORT_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    task_dir = _task_dir(import_id)
    task_dir.mkdir(mode=0o700)
    source_path = task_dir / "source.json"
    normalized_path = task_dir / "normalized.jsonl"
    task: ChatHistoryImportTask | None = None
    _analyzing_import_ids.add(import_id)
    try:
        source_size, source_hash = await _stream_upload(file, source_path)
        duplicate = (
            ChatHistoryImportTask.select()
            .where(
                (ChatHistoryImportTask.source_hash == source_hash)
                & ChatHistoryImportTask.status.in_(("ready", "running", "awaiting_profile_review", "completed"))
            )
            .first()
        )
        if duplicate is not None:
            raise HTTPException(status_code=409, detail="相同聊天记录已存在导入任务")

        now = time.time()
        task = ChatHistoryImportTask.create(
            import_id=import_id,
            source_hash=source_hash,
            source_name=source_name,
            source_size=source_size,
            status="analyzing",
            source_path=str(source_path),
            normalized_path=str(normalized_path),
            progress_stage="analyzing",
            progress_current=0,
            progress_total=1,
            created_at=now,
            updated_at=now,
        )
        analysis = await asyncio.to_thread(analyze_qq_chat_export, source_path, normalized_path)
        window_index = await asyncio.to_thread(index_history_windows, normalized_path)
        payload = _analysis_payload(analysis, len(window_index))
        now = time.time()
        task.status = "ready"
        task.chat_id = group_chat_id("qq", analysis.chat.source_id)
        task.chat_name = analysis.chat.name
        task.group_id = analysis.chat.source_id
        task.analysis_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        task.progress_stage = "ready"
        task.progress_current = 1
        task.progress_total = 1
        task.source_path = ""
        task.updated_at = now
        task.save()
        source_path.unlink(missing_ok=True)
        return _task_to_response(task)
    except HTTPException:
        if task is not None:
            task.delete_instance()
        _cleanup_task_files(import_id)
        raise
    except ChatHistoryFormatError as error:
        if task is not None:
            task.delete_instance()
        _cleanup_task_files(import_id)
        message = error.args[0] if error.args and isinstance(error.args[0], str) else ""
        detail = "仅支持 QQ 群聊导出记录" if "仅支持 QQ 群聊" in message else "聊天记录 JSON 格式无效"
        raise HTTPException(status_code=400, detail=detail) from None
    except Exception as error:
        if task is not None:
            task.delete_instance()
        _cleanup_task_files(import_id)
        raise internal_server_error(logger, "分析聊天记录失败", error) from None
    finally:
        _analyzing_import_ids.discard(import_id)


@router.get("", response_model=ChatHistoryImportListResponse)
async def list_chat_history_imports(
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> ChatHistoryImportListResponse:
    verify_auth_token(maibot_session, authorization)
    try:
        tasks = list(ChatHistoryImportTask.select().order_by(ChatHistoryImportTask.created_at.desc()).limit(100))
        for task in tasks:
            _reconcile_interrupted_task(task)
        return ChatHistoryImportListResponse(data=[_task_to_response(task) for task in tasks])
    except HTTPException:
        raise
    except Exception as error:
        raise internal_server_error(logger, "获取聊天记录导入任务失败", error) from None


@router.get("/{import_id}", response_model=ChatHistoryImportResponse)
async def get_chat_history_import(
    import_id: str,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> ChatHistoryImportResponse:
    verify_auth_token(maibot_session, authorization)
    return _task_to_response(_get_task_or_404(import_id))


@router.get("/{import_id}/participants", response_model=ChatHistoryParticipantListResponse)
async def list_chat_history_participants(
    import_id: str,
    query: Annotated[str, Query(max_length=100)] = "",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 30,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> ChatHistoryParticipantListResponse:
    verify_auth_token(maibot_session, authorization)
    task = _get_task_or_404(import_id)
    analysis = _load_json_object(task.analysis_json)
    raw_participants = analysis.get("participants", [])
    participants = (
        [item for item in raw_participants if isinstance(item, dict)] if isinstance(raw_participants, list) else []
    )
    normalized_query = query.casefold().strip()
    if normalized_query:
        participants = [
            participant
            for participant in participants
            if normalized_query
            in " ".join(str(participant.get(key) or "").casefold() for key in ("source_id", "name", "card"))
        ]
    total_items = len(participants)
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    if page > total_pages and total_items:
        raise HTTPException(status_code=422, detail="参与者页码超出范围")
    start = (page - 1) * page_size
    data = [ImportedParticipantResponse.model_validate(item) for item in participants[start : start + page_size]]
    return ChatHistoryParticipantListResponse(
        data=data,
        pagination=ChatHistoryParticipantPagination(
            page=page,
            page_size=page_size,
            total_items=total_items,
            total_pages=total_pages,
        ),
    )


@router.get("/{import_id}/candidates", response_model=ChatHistoryCandidateListResponse)
async def list_chat_history_candidates(
    import_id: str,
    kind: Literal["expressions", "behaviors", "jargons", "memories", "profiles"] = "expressions",
    query: Annotated[str, Query(max_length=100)] = "",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 30,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> ChatHistoryCandidateListResponse:
    verify_auth_token(maibot_session, authorization)
    task = _get_task_or_404(import_id)
    normalized_query = query.casefold().strip()
    try:
        data, total_items = await asyncio.to_thread(
            page_candidate_catalog,
            _task_dir(import_id),
            _load_json_object(task.result_json),
            kind=kind,
            query=normalized_query,
            page=page,
            page_size=page_size,
        )
    except CandidateCatalogUnavailableError as error:
        detail = (
            "完整候选目录文件缺失，请删除该任务后重新导入学习"
            if error.reason == "missing"
            else "完整候选目录文件损坏，请删除该任务后重新导入学习"
        )
        raise HTTPException(status_code=409, detail=detail) from None
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    if page > total_pages and total_items:
        raise HTTPException(status_code=422, detail="候选页码超出范围")
    return ChatHistoryCandidateListResponse(
        kind=kind,
        data=data,
        pagination=ChatHistoryCandidatePagination(
            page=page,
            page_size=page_size,
            total_items=total_items,
            total_pages=total_pages,
        ),
    )


async def _update_progress(import_id: str, stage: str, current: int, total: int) -> None:
    ChatHistoryImportTask.update(
        progress_stage=stage[:32],
        progress_current=max(0, current),
        progress_total=max(1, total),
        updated_at=time.time(),
    ).where(ChatHistoryImportTask.import_id == import_id).execute()


def _write_result(import_id: str, result: dict[str, Any]) -> None:
    task_dir = _task_dir(import_id)
    temporary = task_dir / "result.json.tmp"
    destination = task_dir / "result.json"
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with temporary.open("wb") as output:
        temporary.chmod(0o600)
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(destination)
    fsync_directory(task_dir)


def _mark_task_cancelled(import_id: str, normalized_path: Path) -> None:
    now = time.time()
    ChatHistoryImportTask.update(
        status="cancelled",
        progress_stage="cancelled",
        resume_stage=None,
        checkpoint_window_count=0,
        error_message=None,
        normalized_path="",
        cancel_requested=False,
        updated_at=now,
        completed_at=now,
    ).where(ChatHistoryImportTask.import_id == import_id).execute()
    normalized_path.unlink(missing_ok=True)
    remove_history_resume_artifacts(_task_dir(import_id))


async def _commit_learning_result(
    *,
    import_id: str,
    task: ChatHistoryImportTask,
    normalized_path: Path,
    result_payload: dict[str, Any],
    profile_decisions: dict[str, str] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Write a reviewed result and then atomically advance the task state."""

    options = _load_json_object(task.options_json)
    candidates = history_candidates_from_json(result_payload.get("candidates"))
    evidence = await asyncio.to_thread(load_history_candidate_evidence, normalized_path, candidates)
    if should_cancel and should_cancel():
        raise HistoryLearningCancelled("聊天记录学习已取消")
    await _update_progress(import_id, "storing", 0, 1)
    store_core_candidates_once(
        task=task,
        result_payload=result_payload,
        candidates=candidates,
        evidence=evidence,
        store_candidates=store_history_candidates,
    )
    await _update_progress(import_id, "storing", 1, 1)
    result_payload["enrichment_store_result"] = None
    if options.get("extract_memories") is True or options.get("update_profiles") is True:
        current_task = ChatHistoryImportTask.get_by_id(task.id)
        persisted_payload = _load_json_object(current_task.result_json)
        persisted_enrichment_result = persisted_payload.get("enrichment_store_result")
        if current_task.enrichment_store_completed and isinstance(persisted_enrichment_result, dict):
            result_payload["enrichment_store_result"] = persisted_enrichment_result
        else:
            enrichment_result = await store_history_enrichment(
                import_id=import_id,
                chat_id=task.chat_id or "",
                group_id=task.group_id or "",
                chat_name=task.chat_name or task.group_id or "群聊",
                candidates=candidates,
                evidence=evidence,
                extract_memories=options.get("extract_memories") is True,
                update_profiles=options.get("update_profiles") is True,
                profile_decisions=profile_decisions,
                progress=lambda stage, current, total: _update_progress(import_id, stage, current, total),
                should_cancel=should_cancel,
            )
            result_payload["enrichment_store_result"] = enrichment_result.to_json()
            ChatHistoryImportTask.update(
                enrichment_store_completed=True,
                result_json=json.dumps(result_payload, ensure_ascii=False, separators=(",", ":")),
                updated_at=time.time(),
            ).where(ChatHistoryImportTask.import_id == import_id).execute()

    _write_result(import_id, result_payload)
    now = time.time()
    ChatHistoryImportTask.update(
        status="completed",
        result_json=json.dumps(result_payload, ensure_ascii=False, separators=(",", ":")),
        progress_stage="completed",
        progress_current=1,
        progress_total=1,
        resume_stage=None,
        checkpoint_window_count=0,
        normalized_path="",
        updated_at=now,
        completed_at=now,
    ).where(ChatHistoryImportTask.import_id == import_id).execute()
    normalized_path.unlink(missing_ok=True)
    remove_history_resume_artifacts(_task_dir(import_id))
    return result_payload


async def _run_learning(import_id: str) -> None:
    task = _get_task_or_404(import_id)
    normalized_path = _task_dir(import_id) / "normalized.jsonl"
    options = _load_json_object(task.options_json)
    extract_memories = options.get("extract_memories") is True
    update_profiles = options.get("update_profiles") is True
    participant_scope = options.get("participant_scope")
    participant_scope = participant_scope if isinstance(participant_scope, dict) else {}
    if participant_scope.get("mode") == "custom":
        eligible_sender_ids = participant_scope.get("included_ids") or options.get("participant_ids") or None
        excluded_sender_ids = None
    else:
        eligible_sender_ids = (options.get("participant_ids") or None) if not participant_scope else None
        excluded_sender_ids = participant_scope.get("excluded_ids") or None

    def should_cancel() -> bool:
        current = ChatHistoryImportTask.get_or_none(ChatHistoryImportTask.import_id == import_id)
        return current is None or bool(current.cancel_requested)

    try:
        task_dir = _task_dir(import_id)
        result_payload = await asyncio.to_thread(load_pending_learning_result, task_dir)
        if result_payload is None:
            checkpoint_session = await ExtractionCheckpointSession.load(task, task_dir, options)

            result = await ChatHistoryLearner().learn(
                normalized_path,
                chat_id=task.chat_id or "",
                chat_name=task.chat_name or task.group_id or "群聊",
                depth=str(options.get("depth") or "balanced"),
                eligible_sender_ids=eligible_sender_ids,
                excluded_sender_ids=excluded_sender_ids,
                store=False,
                progress=lambda stage, current, total: _update_progress(import_id, stage, current, total),
                should_cancel=should_cancel,
                extract_memories=extract_memories,
                update_profiles=update_profiles,
                extraction_concurrency=options.get(
                    "extraction_concurrency",
                    DEFAULT_HISTORY_EXTRACTION_CONCURRENCY,
                ),
                resume_checkpoints=checkpoint_session.checkpoints,
                checkpoint_callback=checkpoint_session.persist,
            )
            if isinstance(result, HistoryLearningResult):
                result_payload = result.to_json(include_candidate_catalog=False)
            else:
                result_payload = result.to_json()
            candidate_catalog = getattr(result, "candidate_catalog", None)
            if not isinstance(candidate_catalog, HistoryCandidates):
                runtime_candidates = getattr(result, "candidates", None)
                candidate_catalog = (
                    runtime_candidates
                    if isinstance(runtime_candidates, HistoryCandidates)
                    else history_candidates_from_json(
                        result_payload.get("candidate_catalog") or result_payload.get("candidates")
                    )
                )
            if should_cancel():
                raise HistoryLearningCancelled("聊天记录学习已取消")
            await _update_progress(import_id, "storing_catalog", 0, 1)
            result_payload["candidate_catalog"] = await asyncio.to_thread(
                write_candidate_catalog,
                task_dir,
                candidate_catalog,
                complete=bool(getattr(result, "candidate_catalog_complete", True)),
                incomplete_window_ids=tuple(getattr(result, "incomplete_window_ids", ())),
            )
            await _update_progress(import_id, "storing_catalog", 1, 1)
            result_payload["enrichment_store_result"] = None
            await asyncio.to_thread(write_pending_learning_result, task_dir, result_payload)

        runtime_candidates = history_candidates_from_json(result_payload.get("candidates"))
        if update_profiles and runtime_candidates.profiles:
            profile_evidence = await asyncio.to_thread(
                load_history_enrichment_evidence,
                normalized_path,
                runtime_candidates,
            )
            conflicts = find_history_profile_conflicts(
                candidates=runtime_candidates,
                evidence=profile_evidence,
                group_id=task.group_id or "",
                chat_name=task.chat_name or task.group_id or "群聊",
            )
            if conflicts:
                result_payload["profile_review"] = {"conflicts": conflicts, "decisions": None}
                _write_result(import_id, result_payload)
                now = time.time()
                ChatHistoryImportTask.update(
                    status="awaiting_profile_review",
                    result_json=json.dumps(result_payload, ensure_ascii=False, separators=(",", ":")),
                    progress_stage="awaiting_profile_review",
                    progress_current=0,
                    progress_total=1,
                    resume_stage=None,
                    checkpoint_window_count=0,
                    normalized_path=str(normalized_path),
                    updated_at=now,
                ).where(ChatHistoryImportTask.import_id == import_id).execute()
                remove_history_resume_artifacts(task_dir)
                return
        await _commit_learning_result(
            import_id=import_id,
            task=task,
            normalized_path=normalized_path,
            result_payload=result_payload,
            should_cancel=should_cancel,
        )
    except asyncio.CancelledError:
        _mark_task_cancelled(import_id, normalized_path)
        raise
    except HistoryLearningCancelled:
        _mark_task_cancelled(import_id, normalized_path)
    except Exception as error:
        log_exception_type(logger, "聊天记录后台学习失败", error, import_id=import_id)
        current = ChatHistoryImportTask.get_or_none(ChatHistoryImportTask.import_id == import_id)
        if current is not None:
            message = (
                "学习断点损坏，无法安全继续，请删除任务后重新导入"
                if isinstance(error, HistoryCheckpointUnavailableError)
                else "学习失败，已保留最近断点；请检查模型配置和服务日志后继续"
            )
            mark_task_failed(current, message, resume_stage=current.progress_stage)


async def _run_profile_decisions(import_id: str) -> None:
    task = _get_task_or_404(import_id)
    normalized_path = _task_dir(import_id) / "normalized.jsonl"
    result_payload = _load_json_object(task.result_json)
    review = result_payload.get("profile_review")
    decisions = review.get("decisions") if isinstance(review, dict) else None

    def should_cancel() -> bool:
        current = ChatHistoryImportTask.get_or_none(ChatHistoryImportTask.import_id == import_id)
        return current is None or bool(current.cancel_requested)

    try:
        if not isinstance(decisions, dict):
            raise ValueError("画像决策数据无效")
        await _commit_learning_result(
            import_id=import_id,
            task=task,
            normalized_path=normalized_path,
            result_payload=result_payload,
            profile_decisions={str(key): str(value) for key, value in decisions.items()},
            should_cancel=should_cancel,
        )
    except asyncio.CancelledError:
        _mark_task_cancelled(import_id, normalized_path)
        raise
    except HistoryLearningCancelled:
        _mark_task_cancelled(import_id, normalized_path)
    except Exception as error:
        log_exception_type(logger, "聊天记录画像决策写入失败", error, import_id=import_id)
        current = ChatHistoryImportTask.get_or_none(ChatHistoryImportTask.import_id == import_id)
        if current is not None:
            mark_task_failed(
                current,
                "画像决策写入失败，已保留待提交结果；请检查服务日志后继续",
                resume_stage="profile_commit",
            )


@router.post("/{import_id}/start", response_model=ChatHistoryImportResponse)
async def start_chat_history_import(
    import_id: str,
    request_body: ChatHistoryImportStartRequest,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> ChatHistoryImportResponse:
    verify_auth_token(maibot_session, authorization)
    task = _get_task_or_404(import_id)
    if task.status != "ready":
        raise HTTPException(status_code=409, detail="当前任务状态不能开始学习")
    if _active_local_import_count() >= MAX_CONCURRENT_IMPORTS:
        raise HTTPException(status_code=409, detail="已有聊天记录学习任务正在运行")

    analysis = _load_json_object(task.analysis_json)
    participants = analysis.get("participants", [])
    participants = participants if isinstance(participants, list) else []
    participant_scope = _resolve_participant_scope(request_body, participants)
    participant_ids = participant_scope.get("included_ids", [])

    now = time.time()
    options = {
        "depth": request_body.depth,
        "participant_ids": participant_ids,
        "participant_scope": participant_scope,
        "extract_memories": request_body.extract_memories,
        "update_profiles": request_body.update_profiles,
        "extraction_concurrency": request_body.extraction_concurrency,
    }
    remove_history_resume_artifacts(_task_dir(import_id))
    updated = (
        ChatHistoryImportTask.update(
            status="running",
            options_json=json.dumps(options, ensure_ascii=False, separators=(",", ":")),
            result_json=None,
            error_message=None,
            progress_stage="queued",
            progress_current=0,
            progress_total=1,
            resume_stage=None,
            checkpoint_window_count=0,
            attempt_count=1,
            core_store_completed=False,
            enrichment_store_completed=False,
            cancel_requested=False,
            started_at=now,
            completed_at=None,
            updated_at=now,
        )
        .where((ChatHistoryImportTask.import_id == import_id) & (ChatHistoryImportTask.status == "ready"))
        .execute()
    )
    if updated != 1:
        raise HTTPException(status_code=409, detail="任务已被其他请求启动")
    _register_running_task(import_id, _run_learning(import_id), f"chat-history-import-{import_id}")
    return _task_to_response(_get_task_or_404(import_id))


@router.post("/{import_id}/profile-decisions", response_model=ChatHistoryImportResponse)
async def submit_chat_history_profile_decisions(
    import_id: str,
    request_body: ChatHistoryProfileDecisionRequest,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> ChatHistoryImportResponse:
    verify_auth_token(maibot_session, authorization)
    task = _get_task_or_404(import_id)
    if task.status != "awaiting_profile_review":
        raise HTTPException(status_code=409, detail="当前任务不需要画像确认")
    if _active_local_import_count() >= MAX_CONCURRENT_IMPORTS:
        raise HTTPException(status_code=409, detail="已有聊天记录学习任务正在运行")

    result_payload = _load_json_object(task.result_json)
    review = result_payload.get("profile_review")
    conflicts = review.get("conflicts") if isinstance(review, dict) else None
    if not isinstance(conflicts, list) or not conflicts:
        raise HTTPException(status_code=409, detail="任务缺少可确认的画像冲突")
    expected_ids = {
        str(conflict.get("profile_id"))
        for conflict in conflicts
        if isinstance(conflict, dict) and conflict.get("profile_id")
    }
    if not expected_ids or set(request_body.decisions) != expected_ids:
        raise HTTPException(status_code=422, detail="必须为每个画像冲突选择处理方式")

    result_payload["profile_review"] = {
        "conflicts": conflicts,
        "decisions": dict(request_body.decisions),
    }
    now = time.time()
    updated = (
        ChatHistoryImportTask.update(
            status="running",
            result_json=json.dumps(result_payload, ensure_ascii=False, separators=(",", ":")),
            progress_stage="storing",
            progress_current=0,
            progress_total=1,
            cancel_requested=False,
            updated_at=now,
        )
        .where(
            (ChatHistoryImportTask.import_id == import_id) & (ChatHistoryImportTask.status == "awaiting_profile_review")
        )
        .execute()
    )
    if updated != 1:
        raise HTTPException(status_code=409, detail="画像决策已由其他请求提交")
    _register_running_task(import_id, _run_profile_decisions(import_id), f"chat-history-profile-{import_id}")
    return _task_to_response(_get_task_or_404(import_id))


@router.post("/{import_id}/resume", response_model=ChatHistoryImportResponse)
async def resume_chat_history_import(
    import_id: str,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> ChatHistoryImportResponse:
    verify_auth_token(maibot_session, authorization)
    task = _get_task_or_404(import_id)
    if task.status != "failed":
        raise HTTPException(status_code=409, detail="只有失败的任务可以继续")
    options = _load_json_object(task.options_json)
    if not _can_resume_task(task, options):
        raise HTTPException(status_code=409, detail="该任务没有可用的降噪记录，无法继续")
    if _active_local_import_count() >= MAX_CONCURRENT_IMPORTS:
        raise HTTPException(status_code=409, detail="已有聊天记录学习任务正在运行")

    task_dir = _task_dir(import_id)
    if task.resume_stage == "profile_commit":
        if not has_submitted_profile_decisions(task):
            raise HTTPException(status_code=409, detail="画像确认结果缺失，无法继续写入")
    else:
        try:
            await asyncio.to_thread(validate_learning_resume_artifacts, task, task_dir, options)
        except (HistoryCheckpointUnavailableError, CandidateCatalogUnavailableError) as error:
            log_exception_type(logger, "聊天记录学习断点校验失败", error, import_id=import_id)
            raise HTTPException(
                status_code=409, detail="保存的断点已损坏，无法安全继续；请删除任务后重新导入"
            ) from None

    now = time.time()
    updated = (
        ChatHistoryImportTask.update(
            status="running",
            error_message=None,
            progress_stage="resuming",
            progress_current=0,
            progress_total=1,
            cancel_requested=False,
            attempt_count=max(0, task.attempt_count) + 1,
            completed_at=None,
            updated_at=now,
        )
        .where((ChatHistoryImportTask.import_id == import_id) & (ChatHistoryImportTask.status == "failed"))
        .execute()
    )
    if updated != 1:
        raise HTTPException(status_code=409, detail="任务已由其他请求继续")

    runner = _run_profile_decisions if task.resume_stage == "profile_commit" else _run_learning
    _register_running_task(import_id, runner(import_id), f"chat-history-resume-{import_id}")
    return _task_to_response(_get_task_or_404(import_id))


@router.delete("/{import_id}", response_model=ChatHistoryImportDeleteResponse)
async def delete_chat_history_import(
    import_id: str,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
) -> ChatHistoryImportDeleteResponse:
    verify_auth_token(maibot_session, authorization)
    task = _get_task_or_404(import_id)
    running = _running_tasks.get(import_id)
    if task.status == "running":
        if task.progress_stage in _NON_CANCELLABLE_PROGRESS_STAGES:
            raise HTTPException(status_code=409, detail="学习结果正在提交，完成后才能删除任务")
        updated = (
            ChatHistoryImportTask.update(cancel_requested=True, updated_at=time.time())
            .where(
                (ChatHistoryImportTask.import_id == import_id)
                & (ChatHistoryImportTask.status == "running")
                & ChatHistoryImportTask.progress_stage.not_in(_NON_CANCELLABLE_PROGRESS_STAGES)
            )
            .execute()
        )
        if updated != 1:
            raise HTTPException(status_code=409, detail="任务状态已变化，请刷新后重试")
        if running is not None and not running.done():
            running.cancel()
        return ChatHistoryImportDeleteResponse(success=True, message="已请求取消导入任务")

    deleted = (
        ChatHistoryImportTask.delete()
        .where(
            (ChatHistoryImportTask.import_id == import_id)
            & (ChatHistoryImportTask.status == task.status)
            & (ChatHistoryImportTask.updated_at == task.updated_at)
        )
        .execute()
    )
    if deleted != 1:
        raise HTTPException(status_code=409, detail="任务状态已变化，请刷新后重试")
    _cleanup_task_files(import_id)
    return ChatHistoryImportDeleteResponse(success=True, message="导入任务已删除")
