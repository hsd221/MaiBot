"""记忆存储层 — MemoryStore 统一封装 + Qdrant 向量索引管理

提供 SQLite (源数据) + Qdrant (向量索引) 双层存储的统一入口。
"""

import asyncio
import contextvars
import datetime
import json
import uuid
from dataclasses import dataclass, field
from functools import partial, wraps
from typing import Any, Optional

from src.common.logger import get_logger
from src.memory.schema import (
    AtomAssociationModel,
    ConflictObservation,
    EpisodicDetail,
    MemoryAtom,
    MemoryTraceChain,
    RawMessageArchive,
    SemanticDetail,
    VectorIndexState,
    configure_memory_database,
    initialize_database,
    memory_db,
)
from src.memory.types import PayloadSchemaField

logger = get_logger("memory.store")

_QDRANT_POINT_NAMESPACE = uuid.UUID("8e9e1f74-2b30-4f33-8e9b-9b2c972a1a67")
_DATETIME_FIELDS = ("created_at", "last_accessed_at", "last_reinforced_at")
_LEGACY_EMBEDDING_SIGNATURE = "legacy-unknown"
_BM25_REBUILD_FIELDS = {"content", "status"}
ATOM_INDEX_SYNC_PAYLOAD_FIELDS: tuple[str, ...] = (
    "atom_type",
    "weight",
    "importance",
    "confidence",
    "status",
    "source_scene",
    "source_id",
    "privacy_level",
    "embedding_source_hash",
)


def _coerce_datetime(value: Any) -> datetime.datetime:
    """将持久层时间字段归一化为 Peewee DateTimeField 可接受的 datetime。"""
    if isinstance(value, datetime.datetime):
        return value
    if value is None:
        return datetime.datetime.now()
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(value)
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value)
        except (TypeError, ValueError):
            try:
                return datetime.datetime.fromtimestamp(float(value))
            except (TypeError, ValueError):
                logger.warning("无法解析时间字段，使用当前时间", value=value)
                return datetime.datetime.now()
    logger.warning("未知时间字段类型，使用当前时间", value_type=type(value).__name__)
    return datetime.datetime.now()


def _normalize_datetime_fields(data: dict[str, Any], *, fill_missing: bool = False) -> None:
    """原地归一化记忆原子的时间字段。"""
    for field_name in _DATETIME_FIELDS:
        if fill_missing or field_name in data:
            data[field_name] = _coerce_datetime(data.get(field_name))


# ---------------------------------------------------------------------------
# Qdrant 客户端导入（可选依赖）
# ---------------------------------------------------------------------------

try:
    from qdrant_client import QdrantClient as _QdrantClient
    from qdrant_client.http import models as qdrant_models

    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False
    _QdrantClient = None  # type: ignore
    qdrant_models = None  # type: ignore


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass
class MemoryStoreConfig:
    """记忆存储配置

    这些默认值作为 fallback 使用，生产环境值通过 global_config.memory 注入。
    """

    sqlite_path: str = "data/memory.db"
    """SQLite 数据库文件路径"""

    qdrant_url: str = ""
    """Qdrant 服务器 URL（空字符串=本地嵌入模式，设置 URL=服务器/云模式）"""

    qdrant_api_key: Optional[str] = field(default=None, repr=False)
    """Qdrant API 密钥（可选，repr=False 避免日志泄露）"""

    qdrant_local_path: str = "data/qdrant"
    """Qdrant 本地模式数据目录"""

    embedding_dimension: int = 1024
    """嵌入向量维度"""

    embedding_signature: str = ""
    """稳定的 embedding 配置签名，由启动层计算。"""

    embedding_model_name: str = ""
    """当前 embedding 模型的可读名称，仅用于日志。"""

    collection_name_atoms: str = "memory_atoms"
    """记忆原子 Qdrant 集合名称"""

    collection_name_graph: str = "graph_entries"
    """图条目 Qdrant 集合名称"""

    vector_batch_size: int = 100
    """向量批量写入大小"""

    def __repr__(self) -> str:
        """返回配置字符串（隐藏敏感字段）"""
        masked_key = "***" if self.qdrant_api_key else None
        return (
            f"MemoryStoreConfig(sqlite_path={self.sqlite_path!r}, "
            f"qdrant_url={self.qdrant_url!r}, "
            f"qdrant_api_key={masked_key}, "
            f"qdrant_local_path={self.qdrant_local_path!r}, "
            f"embedding_dimension={self.embedding_dimension}, "
            f"embedding_signature={self.embedding_signature!r}, "
            f"collection_name_atoms={self.collection_name_atoms!r}, "
            f"vector_batch_size={self.vector_batch_size})"
        )


# ---------------------------------------------------------------------------
# Qdrant 管理器
# ---------------------------------------------------------------------------


def _serialized_remote_qdrant(method):
    """Keep remote operations atomic across newly asynchronous SDK calls."""

    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        return await self._run_remote_operation(method, *args, **kwargs)

    return wrapped


class QdrantManager:
    """Qdrant 向量索引管理器

    封装 Qdrant 集合管理、向量写入/检索/删除操作。
    当 qdrant-client 未安装时静默降级，所有方法返回空结果。
    """

    def __init__(self, config: MemoryStoreConfig):
        self.config = config
        self._client: Optional[Any] = None
        self._available = QDRANT_AVAILABLE
        self._atom_alias_name = f"{config.collection_name_atoms}__active"
        self._graph_alias_name = f"{config.collection_name_graph}__active"
        self._active_atoms_collection = config.collection_name_atoms
        self._active_graph_collection = config.collection_name_graph
        self._atom_migration_target: Optional[str] = None
        self._graph_migration_target: Optional[str] = None
        self._vector_search_enabled = True
        self._graph_search_enabled = True
        self._embedding_operations_enabled = True
        self._embedding_signature = config.embedding_signature or f"dimension-{config.embedding_dimension}"
        self._embedding_reconfigure_lock = asyncio.Lock()
        self._remote_operation_lock = asyncio.Lock()
        self._remote_operation_owner: Optional[asyncio.Task] = None
        self._remote_operation_cancel_requested = False

        if not self._available:
            logger.warning("qdrant-client 未安装，向量索引功能不可用。pip install qdrant-client")

    async def _run_remote_operation(self, method, *args, **kwargs):
        if not self.config.qdrant_url or self._remote_operation_owner is asyncio.current_task():
            return await method(self, *args, **kwargs)

        # Waiters remain cancellable before taking the lock. Once started,
        # preserve the old inline-operation ordering through all remote calls
        # and the subsequent SQLite/state updates, even if the caller cancels.
        async with self._remote_operation_lock:

            async def invoke():
                self._remote_operation_owner = asyncio.current_task()
                self._remote_operation_cancel_requested = False
                try:
                    return await method(self, *args, **kwargs)
                finally:
                    # A shutdown may cancel invoke itself, not just its caller.
                    # Finish all post-I/O state changes before surfacing it.
                    if self._remote_operation_cancel_requested:
                        raise asyncio.CancelledError

            operation = asyncio.create_task(invoke())
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                while not operation.done():
                    try:
                        await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not operation.cancelled():
                    operation.exception()
                raise
            finally:
                self._remote_operation_owner = None

    async def _run_qdrant_io(self, method, *args, **kwargs):
        """Only blocking server SDK calls move threads; local SQLite stays put.

        Keep the executor Future rather than a to_thread Task: shutdown cancels
        all Tasks, but the result is required before committing migration state.
        https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.run_in_executor
        """
        if self.config.qdrant_url:
            loop = asyncio.get_running_loop()
            context = contextvars.copy_context()
            call = partial(context.run, method, *args, **kwargs)
            # Keep the concurrent future alive when graceful shutdown cancels
            # every asyncio Task; shield prevents cancellation from reaching
            # the in-flight network request.
            future = loop.run_in_executor(None, call)
            while True:
                try:
                    return await asyncio.shield(future)
                except asyncio.CancelledError:
                    self._remote_operation_cancel_requested = True
                    if future.done():
                        return future.result()
        return method(*args, **kwargs)

    @property
    def atom_migration_pending(self) -> bool:
        return self._atom_migration_target is not None

    @property
    def atom_migration_target(self) -> Optional[str]:
        return self._atom_migration_target

    @property
    def graph_migration_pending(self) -> bool:
        return self._graph_migration_target is not None

    @property
    def graph_migration_target(self) -> Optional[str]:
        return self._graph_migration_target

    @property
    def vector_search_enabled(self) -> bool:
        return self._vector_search_enabled and self._client is not None

    @property
    def graph_search_enabled(self) -> bool:
        return self._graph_search_enabled and self._client is not None

    @property
    def active_atoms_collection(self) -> str:
        if self._client and hasattr(self._client, "get_aliases"):
            return self._atom_alias_name
        return self._active_atoms_collection

    @property
    def active_graph_collection(self) -> str:
        if self._client and hasattr(self._client, "get_aliases"):
            return self._graph_alias_name
        return self._active_graph_collection

    def _atom_write_collection(self) -> str:
        """Return the collection that accepts vectors in the current embedding space."""
        return self._atom_migration_target or self.active_atoms_collection

    def _atom_mutation_collections(self) -> list[str]:
        """Return all atom collections that must receive deletes/payload updates."""
        collections = [self.active_atoms_collection]
        if self._atom_migration_target and self._atom_migration_target not in collections:
            collections.append(self._atom_migration_target)
        return collections

    def _graph_write_collection(self) -> str:
        """Return the graph collection that accepts vectors in the current embedding space."""
        return self._graph_migration_target or self.active_graph_collection

    def _graph_mutation_collections(self) -> list[str]:
        """Return all graph collections that must receive destructive mutations."""
        collections = [self.active_graph_collection]
        if self._graph_migration_target and self._graph_migration_target not in collections:
            collections.append(self._graph_migration_target)
        return collections

    @staticmethod
    def _vector_profile(vector: list[float], payload: dict[str, Any]) -> tuple[Optional[str], Optional[int]]:
        signature = getattr(vector, "embedding_signature", None)
        dimension = getattr(vector, "embedding_dimension", None)
        if signature is None:
            signature = payload.get("embedding_signature")
        if dimension is None:
            dimension = payload.get("embedding_dimension")
        try:
            normalized_dimension = int(dimension) if dimension is not None else None
        except (TypeError, ValueError):
            normalized_dimension = None
        return (str(signature) if signature else None), normalized_dimension

    def _embedding_vector_is_current(self, vector: list[float], payload: dict[str, Any]) -> bool:
        """Reject profiled vectors generated before the current runtime switch."""
        if not self._embedding_operations_enabled:
            return False
        signature, dimension = self._vector_profile(vector, payload)
        if signature is not None and signature != self._embedding_signature:
            return False
        if dimension is not None and dimension != int(self.config.embedding_dimension):
            return False
        return True

    def _with_atom_embedding_metadata(
        self,
        point_id: str | int,
        payload: dict[str, Any],
        vector: Optional[list[float]] = None,
    ) -> dict[str, Any]:
        normalized_payload = {**payload, "atom_id": str(point_id)}
        if self.config.embedding_signature:
            signature, dimension = self._vector_profile(vector or [], payload)
            normalized_payload["embedding_signature"] = signature or self._embedding_signature
            normalized_payload["embedding_dimension"] = dimension or self.config.embedding_dimension
        return normalized_payload

    def _with_graph_embedding_metadata(
        self,
        point_id: str | int,
        payload: dict[str, Any],
        vector: Optional[list[float]] = None,
    ) -> dict[str, Any]:
        normalized_payload = {**payload, "entry_id": str(point_id)}
        if self.config.embedding_signature:
            signature, dimension = self._vector_profile(vector or [], payload)
            normalized_payload["embedding_signature"] = signature or self._embedding_signature
            normalized_payload["embedding_dimension"] = dimension or self.config.embedding_dimension
        return normalized_payload

    @staticmethod
    def _versioned_collection_name(base_name: str, signature: str, dimension: int) -> str:
        suffix = f"__emb_{signature[:12]}_{int(dimension)}"
        return f"{base_name[: max(1, 180 - len(suffix))]}{suffix}"

    def _get_collection_names(self) -> set[str]:
        if self._client is None:
            return set()
        return {str(collection.name) for collection in self._client.get_collections().collections}

    def _get_aliases(self) -> dict[str, str]:
        if self._client is None or not hasattr(self._client, "get_aliases"):
            return {}
        response = self._client.get_aliases()
        return {
            str(alias.alias_name): str(alias.collection_name)
            for alias in getattr(response, "aliases", []) or []
            if getattr(alias, "alias_name", None) and getattr(alias, "collection_name", None)
        }

    def _switch_alias(self, alias_name: str, collection_name: str) -> None:
        # Qdrant applies all alias changes in this request atomically, which
        # makes a delete+create pair safe for blue/green collection cutover.
        # Source: https://qdrant.tech/documentation/concepts/collections/#collection-aliases
        if self._client is None or qdrant_models is None or not hasattr(self._client, "update_collection_aliases"):
            raise RuntimeError("当前 Qdrant 客户端不支持 collection alias")

        aliases = self._get_aliases()
        operations: list[Any] = []
        if alias_name in aliases:
            if aliases[alias_name] == collection_name:
                return
            operations.append(
                qdrant_models.DeleteAliasOperation(
                    delete_alias=qdrant_models.DeleteAlias(alias_name=alias_name),
                )
            )
        operations.append(
            qdrant_models.CreateAliasOperation(
                create_alias=qdrant_models.CreateAlias(
                    collection_name=collection_name,
                    alias_name=alias_name,
                )
            )
        )
        if not self._client.update_collection_aliases(change_aliases_operations=operations):
            raise RuntimeError(f"Qdrant alias '{alias_name}' 切换失败")

    def _load_vector_state(self, index_name: Optional[str] = None) -> Optional[VectorIndexState]:
        resolved_index_name = index_name or self.config.collection_name_atoms
        try:
            with memory_db:
                return VectorIndexState.get_or_none(VectorIndexState.index_name == resolved_index_name)
        except Exception:
            logger.exception(
                "读取向量索引迁移状态失败",
                event_code="memory.qdrant.migration_state_read_failed",
                index_name=resolved_index_name,
            )
            return None

    def get_atom_migration_state(self) -> Optional[VectorIndexState]:
        """Return the persisted atom-index migration state."""
        return self._load_vector_state()

    def get_graph_migration_state(self) -> Optional[VectorIndexState]:
        """Return the persisted graph-index migration state."""
        return self._load_vector_state(self.config.collection_name_graph)

    def _save_vector_state(self, index_name: Optional[str] = None, **updates: Any) -> VectorIndexState:
        resolved_index_name = index_name or self.config.collection_name_atoms
        now = datetime.datetime.now()
        with memory_db:
            state, _ = VectorIndexState.get_or_create(
                index_name=resolved_index_name,
                defaults={"updated_at": now},
            )
            for field_name, value in updates.items():
                setattr(state, field_name, value)
            state.updated_at = now
            state.save()
            return state

    @staticmethod
    def _normalize_point_id(point_id: str | int) -> str | int:
        """将业务 ID 转成 Qdrant 可接受的 point id。

        Qdrant 只接受 UUID 字符串或无符号整数。默认生成的记忆 atom_id 已是 UUID，
        这里保持原值以兼容已有索引；非 UUID 字符串使用 uuid5 做稳定映射。
        """
        if isinstance(point_id, int):
            if 0 <= point_id <= 2**64 - 1:
                return point_id
            point_id = str(point_id)

        value = str(point_id)
        try:
            return str(uuid.UUID(value))
        except (TypeError, ValueError):
            pass

        if value.isdecimal():
            try:
                numeric_id = int(value)
                if str(numeric_id) == value:
                    return numeric_id
            except ValueError:
                pass

        return str(uuid.uuid5(_QDRANT_POINT_NAMESPACE, value))

    @staticmethod
    def _payload_field_names(payload_schema: list[PayloadSchemaField]) -> set[str]:
        return {field["name"] for field in payload_schema}

    @staticmethod
    def _build_filter(
        filters: Optional[dict[str, Any]],
        payload_schema: list[PayloadSchemaField],
    ) -> Any:
        """构建 Qdrant payload filter，忽略非 payload 控制字段。"""
        if not filters or qdrant_models is None:
            return None

        allowed_fields = QdrantManager._payload_field_names(payload_schema)
        conditions = []
        for key, value in filters.items():
            if key not in allowed_fields or value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                values = list(value)
                if not values:
                    continue
                conditions.append(
                    qdrant_models.FieldCondition(
                        key=key,
                        match=qdrant_models.MatchAny(any=values),
                    )
                )
            else:
                conditions.append(
                    qdrant_models.FieldCondition(
                        key=key,
                        match=qdrant_models.MatchValue(value=value),
                    )
                )

        if not conditions:
            return None
        return qdrant_models.Filter(must=conditions)

    @staticmethod
    def _collection_vector_size(collection_info: Any) -> Optional[int]:
        """读取 Qdrant collection 的未命名向量维度。"""
        config = getattr(collection_info, "config", None)
        params = getattr(config, "params", None)
        vectors = getattr(params, "vectors", None)
        if vectors is None:
            return None
        if isinstance(vectors, dict):
            if "" in vectors:
                return getattr(vectors[""], "size", None)
            return None
        return getattr(vectors, "size", None)

    def _query_points(
        self,
        collection_name: str,
        query_vector: list[float],
        qdrant_filter: Any = None,
        limit: int = 10,
    ) -> list[Any]:
        """兼容新旧 qdrant-client 的向量查询 API。"""
        if self._client is None:
            return []
        if hasattr(self._client, "search"):
            return self._client.search(
                collection_name=collection_name,
                query_vector=query_vector,
                query_filter=qdrant_filter,
                limit=limit,
                with_payload=True,
            )

        response = self._client.query_points(
            collection_name=collection_name,
            query=query_vector,
            query_filter=qdrant_filter,
            limit=limit,
            with_payload=True,
        )
        points = getattr(response, "points", response)
        return list(points or [])

    async def _ensure_collection_for_dimension(
        self,
        collection_name: str,
        payload_schema: list[PayloadSchemaField],
        dimension: int,
    ) -> None:
        """Ensure a physical collection has exactly the requested dimension."""
        if not self._available or not self._client:
            return

        exists = collection_name in (await self._run_qdrant_io(self._get_collection_names))
        if exists:
            collection_info = await self._run_qdrant_io(self._client.get_collection, collection_name=collection_name)
            vector_size = self._collection_vector_size(collection_info)
            if vector_size is None:
                raise RuntimeError(
                    f"Qdrant collection '{collection_name}' vector configuration is incompatible with unnamed vectors"
                )
            if vector_size != int(dimension):
                raise RuntimeError(
                    f"Qdrant collection '{collection_name}' vector size {vector_size} != requested dimension {dimension}"
                )
            return

        await self._run_qdrant_io(
            self._client.create_collection,
            collection_name=collection_name,
            vectors_config=qdrant_models.VectorParams(
                size=int(dimension),
                distance=qdrant_models.Distance.COSINE,
            ),
        )
        for field_schema in payload_schema:
            await self._run_qdrant_io(
                self._client.create_payload_index,
                collection_name=collection_name,
                field_name=field_schema["name"],
                field_type=field_schema["type"],
            )
        logger.info(
            "Qdrant 版本化集合已创建",
            event_code="memory.qdrant.versioned_collection_created",
            collection=collection_name,
            dimension=dimension,
        )

    async def _initialize_versioned_index(
        self,
        *,
        index_name: str,
        alias_name: str,
        payload_schema: list[PayloadSchemaField],
    ) -> tuple[str, Optional[str], bool]:
        """Adopt one legacy collection and prepare a versioned migration target."""
        if self._client is None:
            return index_name, None, False

        collection_names = await self._run_qdrant_io(self._get_collection_names)
        aliases = await self._run_qdrant_io(self._get_aliases)
        state = self._load_vector_state(index_name)
        active_collection = aliases.get(alias_name)
        if active_collection and active_collection not in collection_names:
            raise RuntimeError(f"Qdrant alias '{alias_name}' 指向不存在的 collection")

        expected_collection = self._versioned_collection_name(
            index_name,
            self._embedding_signature,
            self.config.embedding_dimension,
        )
        if not active_collection:
            if state is not None and state.active_collection in collection_names:
                active_collection = str(state.active_collection)
            elif index_name in collection_names:
                active_collection = index_name
            else:
                active_collection = expected_collection
                await self._ensure_collection_for_dimension(
                    active_collection,
                    payload_schema,
                    self.config.embedding_dimension,
                )
            await self._run_qdrant_io(self._switch_alias, alias_name, active_collection)

        active_info = await self._run_qdrant_io(self._client.get_collection, collection_name=active_collection)
        active_dimension = self._collection_vector_size(active_info)
        if active_dimension is None:
            raise RuntimeError(f"Qdrant collection '{active_collection}' 缺少未命名向量配置")

        if state is None:
            points_count = int(getattr(active_info, "points_count", 0) or 0)
            active_signature = self._embedding_signature
            if points_count > 0 and active_collection != expected_collection:
                # Legacy collections predate persisted model metadata.  A
                # matching dimension cannot prove that their vectors came
                # from the configured model, so rebuild them once.
                active_signature = _LEGACY_EMBEDDING_SIGNATURE
            state = self._save_vector_state(
                index_name=index_name,
                active_signature=active_signature,
                active_dimension=active_dimension,
                active_collection=active_collection,
                status="ready",
                target_signature=None,
                target_dimension=None,
                target_collection=None,
                last_processed_id=None,
                migrated_count=0,
                total_count=0,
                last_error=None,
            )
        elif (
            state.target_collection == active_collection
            and state.target_signature == self._embedding_signature
            and state.target_dimension == self.config.embedding_dimension
            and active_dimension == self.config.embedding_dimension
        ):
            # Alias switching is the authoritative activation step.  If the
            # process stopped before SQLite state was updated, recover the
            # completed activation instead of rebuilding a valid target.
            state = self._save_vector_state(
                index_name=index_name,
                active_signature=self._embedding_signature,
                active_dimension=self.config.embedding_dimension,
                active_collection=active_collection,
                target_signature=None,
                target_dimension=None,
                target_collection=None,
                status="ready",
                last_processed_id=None,
                last_error=None,
            )
        elif state.active_collection != active_collection or state.active_dimension != active_dimension:
            # The alias is authoritative.  If it unexpectedly points at a
            # collection other than the persisted active one, its model space
            # is unknown and must be rebuilt before reads resume.
            state = self._save_vector_state(
                index_name=index_name,
                active_signature=_LEGACY_EMBEDDING_SIGNATURE,
                active_dimension=active_dimension,
                active_collection=active_collection,
            )

        target = None
        if (
            state.status in {"migrating", "failed"}
            and state.target_signature == self._embedding_signature
            and state.target_dimension == self.config.embedding_dimension
            and state.target_collection
        ):
            target = state.target_collection

        needs_migration = (
            state.active_signature != self._embedding_signature
            or state.active_dimension != self.config.embedding_dimension
        )
        if needs_migration or target:
            target = target or self._versioned_collection_name(
                index_name,
                self._embedding_signature,
                self.config.embedding_dimension,
            )
            await self._ensure_collection_for_dimension(
                target,
                payload_schema,
                self.config.embedding_dimension,
            )
            target_changed = (
                state.target_signature != self._embedding_signature
                or state.target_dimension != self.config.embedding_dimension
                or state.target_collection != target
            )
            state_updates: dict[str, Any] = {
                "status": "migrating",
                "target_signature": self._embedding_signature,
                "target_dimension": self.config.embedding_dimension,
                "target_collection": target,
                "last_error": None,
            }
            if target_changed:
                state_updates.update(
                    last_processed_id=None,
                    migrated_count=0,
                    total_count=0,
                )
            self._save_vector_state(
                index_name=index_name,
                **state_updates,
            )
            logger.warning(
                "检测到 embedding 配置变化，已准备后台重建",
                event_code="memory.qdrant.migration_required",
                old_signature=state.active_signature,
                new_signature=self._embedding_signature,
                old_dimension=state.active_dimension,
                new_dimension=self.config.embedding_dimension,
                target_collection=target,
                index_name=index_name,
            )
            return active_collection, target, False
        return active_collection, None, True

    async def _initialize_versioned_atoms(self) -> None:
        """Adopt the legacy atom collection and prepare a versioned target when needed."""
        active, target, search_enabled = await self._initialize_versioned_index(
            index_name=self.config.collection_name_atoms,
            alias_name=self._atom_alias_name,
            payload_schema=self._atoms_payload_schema(),
        )
        self._active_atoms_collection = active
        self._atom_migration_target = target
        self._vector_search_enabled = search_enabled

    async def _initialize_graph_collection(self) -> None:
        """Adopt the graph collection and detect embedding-space changes."""
        active, target, search_enabled = await self._initialize_versioned_index(
            index_name=self.config.collection_name_graph,
            alias_name=self._graph_alias_name,
            payload_schema=self._graph_payload_schema(),
        )
        self._active_graph_collection = active
        self._graph_migration_target = target
        self._graph_search_enabled = search_enabled

    @_serialized_remote_qdrant
    async def initialize(self) -> None:
        """初始化 Qdrant 连接并确保集合存在

        支持两种模式:
        - 服务器/云模式: config.qdrant_url 非空时使用
        - 本地嵌入模式: config.qdrant_url 为空时使用 (无需 Docker)
        """
        if not self._available:
            return

        try:
            if self.config.qdrant_url:
                self._client = await self._run_qdrant_io(
                    _QdrantClient,
                    url=self.config.qdrant_url,
                    api_key=self.config.qdrant_api_key,
                )
                mode = "server"
                source = self.config.qdrant_url
            else:
                self._client = await self._run_qdrant_io(
                    _QdrantClient,
                    path=self.config.qdrant_local_path,
                )
                mode = "local"
                source = self.config.qdrant_local_path

            if hasattr(self._client, "update_collection_aliases") and hasattr(self._client, "get_aliases"):
                await self._initialize_versioned_atoms()
                await self._initialize_graph_collection()
            else:
                # Compatibility path for older adapters and small test fakes.
                await self._ensure_collection(self.config.collection_name_atoms, self._atoms_payload_schema())
                await self._ensure_collection(self.config.collection_name_graph, self._graph_payload_schema())
            logger.info(
                "Qdrant 已就绪",
                event_code="memory.qdrant.ready",
                mode=mode,
                source=source,
                atom_collection=self.active_atoms_collection,
                graph_collection=self.active_graph_collection,
                vector_search_enabled=self.vector_search_enabled,
                migration_pending=self.atom_migration_pending,
                graph_search_enabled=self.graph_search_enabled,
                graph_migration_pending=self.graph_migration_pending,
            )
        except Exception:
            logger.exception("Qdrant 初始化失败", event_code="memory.qdrant.init_failed")
            self._client = None

    @_serialized_remote_qdrant
    async def reconfigure_embedding(self, profile: Any) -> bool:
        """Prepare versioned indexes for a validated runtime embedding profile.

        Reads and writes are paused while the new physical collections are
        prepared.  The caller activates the matching embedding runtime only
        after this method succeeds, so an in-flight vector from the previous
        profile can never be labelled as belonging to the new collection.
        """
        signature = str(getattr(profile, "signature", "") or "")
        model_name = str(getattr(profile, "model_name", "") or "")
        try:
            dimension = int(profile.dimension)
        except (TypeError, ValueError) as exc:
            raise ValueError("embedding profile dimension is invalid") from exc
        if not signature or dimension <= 0:
            raise ValueError("embedding profile signature and dimension are required")

        async with self._embedding_reconfigure_lock:
            if signature == self._embedding_signature and dimension == int(self.config.embedding_dimension):
                return True

            previous = {
                "signature": self._embedding_signature,
                "config_signature": self.config.embedding_signature,
                "dimension": self.config.embedding_dimension,
                "model_name": self.config.embedding_model_name,
                "atom_target": self._atom_migration_target,
                "graph_target": self._graph_migration_target,
                "active_atoms": self._active_atoms_collection,
                "active_graph": self._active_graph_collection,
                "atom_search": self._vector_search_enabled,
                "graph_search": self._graph_search_enabled,
            }
            self._embedding_operations_enabled = False
            self._vector_search_enabled = False
            self._graph_search_enabled = False
            try:
                self._embedding_signature = signature
                self.config.embedding_signature = signature
                self.config.embedding_dimension = dimension
                self.config.embedding_model_name = model_name

                if self._client is not None:
                    if not (
                        hasattr(self._client, "update_collection_aliases") and hasattr(self._client, "get_aliases")
                    ):
                        raise RuntimeError("当前 Qdrant 客户端不支持运行时 collection alias 迁移")
                    await self._initialize_versioned_atoms()
                    await self._initialize_graph_collection()
                else:
                    self._atom_migration_target = None
                    self._graph_migration_target = None

                logger.warning(
                    "运行时 embedding 配置已切换，向量索引进入重建阶段",
                    event_code="memory.qdrant.embedding_reconfigured",
                    old_signature=previous["signature"],
                    new_signature=signature,
                    old_dimension=previous["dimension"],
                    new_dimension=dimension,
                )
                return True
            except Exception:
                self._embedding_signature = str(previous["signature"])
                self.config.embedding_signature = previous["config_signature"]
                self.config.embedding_dimension = int(previous["dimension"])
                self.config.embedding_model_name = previous["model_name"]
                self._atom_migration_target = previous["atom_target"]
                self._graph_migration_target = previous["graph_target"]
                self._active_atoms_collection = str(previous["active_atoms"])
                self._active_graph_collection = str(previous["active_graph"])
                self._vector_search_enabled = bool(previous["atom_search"])
                self._graph_search_enabled = bool(previous["graph_search"])
                logger.exception(
                    "运行时 embedding 配置切换失败，继续使用旧 profile",
                    event_code="memory.qdrant.embedding_reconfigure_failed",
                    requested_signature=signature,
                    requested_dimension=dimension,
                )
                return False
            finally:
                self._embedding_operations_enabled = True

    @_serialized_remote_qdrant
    async def close(self) -> None:
        """关闭 Qdrant 连接"""
        self._client = None

    # -- 集合管理 -----------------------------------------------------------

    @staticmethod
    def _atoms_payload_schema() -> list[PayloadSchemaField]:
        """memory_atoms 集合的 payload 字段 schema"""
        return [
            {"name": "atom_id", "type": "keyword"},
            {"name": "atom_type", "type": "keyword"},
            {"name": "user_id", "type": "keyword"},
            {"name": "group_id", "type": "keyword"},
            {"name": "weight", "type": "float"},
            {"name": "importance", "type": "float"},
            {"name": "confidence", "type": "float"},
            {"name": "status", "type": "keyword"},
            {"name": "privacy_level", "type": "keyword"},
            {"name": "source_scene", "type": "keyword"},
            {"name": "source_id", "type": "keyword"},
            {"name": "embedding_signature", "type": "keyword"},
            {"name": "embedding_dimension", "type": "integer"},
            {"name": "embedding_source_hash", "type": "keyword"},
        ]

    @staticmethod
    def _graph_payload_schema() -> list[PayloadSchemaField]:
        """graph_entries 集合的 payload 字段 schema"""
        return [
            {"name": "entry_id", "type": "keyword"},
            {"name": "subject", "type": "keyword"},
            {"name": "predicate", "type": "keyword"},
            {"name": "object", "type": "keyword"},
            {"name": "confidence", "type": "float"},
            {"name": "embedding_signature", "type": "keyword"},
            {"name": "embedding_dimension", "type": "integer"},
            {"name": "embedding_source_hash", "type": "keyword"},
        ]

    async def _ensure_collection(self, collection_name: str, payload_schema: list[PayloadSchemaField]) -> None:
        """确保集合存在，不存在则创建"""
        if not self._available or not self._client:
            return

        try:
            collections = (await self._run_qdrant_io(self._client.get_collections)).collections
            exists = any(c.name == collection_name for c in collections)
        except Exception:
            exists = False

        if exists:
            collection_info = await self._run_qdrant_io(self._client.get_collection, collection_name=collection_name)
            vector_size = self._collection_vector_size(collection_info)
            if vector_size is None:
                raise RuntimeError(
                    f"Qdrant collection '{collection_name}' vector configuration is incompatible with unnamed vectors"
                )
            if vector_size != self.config.embedding_dimension:
                raise RuntimeError(
                    f"Qdrant collection '{collection_name}' vector size {vector_size} "
                    f"!= configured embedding_dimension {self.config.embedding_dimension}"
                )
            return

        if not exists:
            try:
                await self._run_qdrant_io(
                    self._client.create_collection,
                    collection_name=collection_name,
                    vectors_config=qdrant_models.VectorParams(
                        size=self.config.embedding_dimension,
                        distance=qdrant_models.Distance.COSINE,
                    ),
                )
                # 创建 payload 索引
                for field_schema in payload_schema:
                    await self._run_qdrant_io(
                        self._client.create_payload_index,
                        collection_name=collection_name,
                        field_name=field_schema["name"],
                        field_type=field_schema["type"],
                    )
                logger.info(
                    "Qdrant 集合已创建", event_code="memory.qdrant.collection_created", collection=collection_name
                )
            except Exception:
                logger.exception(
                    "Qdrant 集合创建失败",
                    event_code="memory.qdrant.collection_create_failed",
                    collection=collection_name,
                )

    # -- 向量写入 -----------------------------------------------------------

    @_serialized_remote_qdrant
    async def upsert_atom_vector(
        self,
        point_id: str,
        vector: list[float],
        payload: dict[str, Any],
    ) -> bool:
        """写入/更新记忆原子向量"""
        return await self.upsert_atom_vector_to_collection(
            collection_name=self._atom_write_collection(),
            point_id=point_id,
            vector=vector,
            payload=payload,
        )

    @_serialized_remote_qdrant
    async def upsert_atom_vector_to_collection(
        self,
        collection_name: str,
        point_id: str,
        vector: list[float],
        payload: dict[str, Any],
    ) -> bool:
        """Write an atom vector to one explicit physical collection or alias."""
        if not self._available:
            return True
        if not self._client:
            return False
        if not self._embedding_vector_is_current(vector, payload):
            vector_signature, vector_dimension = self._vector_profile(vector, payload)
            logger.warning(
                "拒绝写入非当前 profile 的原子向量",
                event_code="memory.qdrant.atom_vector_profile_rejected",
                point_id=point_id,
                vector_signature=vector_signature,
                vector_dimension=vector_dimension,
                expected_signature=self._embedding_signature,
                expected_dimension=self.config.embedding_dimension,
            )
            return False
        try:
            await self._run_qdrant_io(
                self._client.upsert,
                collection_name=collection_name,
                points=[
                    qdrant_models.PointStruct(
                        id=self._normalize_point_id(point_id),
                        vector=vector,
                        payload=self._with_atom_embedding_metadata(point_id, payload, vector),
                    )
                ],
            )
            return True
        except Exception:
            logger.exception(
                "Qdrant 原子向量写入失败",
                event_code="memory.qdrant.atom_vector_upsert_failed",
                point_id=point_id,
                vector_dimension=len(vector),
                collection=collection_name,
            )
            return False

    @_serialized_remote_qdrant
    async def upsert_graph_vector(
        self,
        point_id: str,
        vector: list[float],
        payload: dict[str, Any],
    ) -> bool:
        """写入/更新图条目向量"""
        return await self.upsert_graph_vector_to_collection(
            collection_name=self._graph_write_collection(),
            point_id=point_id,
            vector=vector,
            payload=payload,
        )

    @_serialized_remote_qdrant
    async def upsert_graph_vector_to_collection(
        self,
        collection_name: str,
        point_id: str,
        vector: list[float],
        payload: dict[str, Any],
    ) -> bool:
        """Write a graph vector to one explicit physical collection or alias."""
        if not self._available:
            return True
        if not self._client:
            return False
        if not self._embedding_vector_is_current(vector, payload):
            vector_signature, vector_dimension = self._vector_profile(vector, payload)
            logger.warning(
                "拒绝写入非当前 profile 的图向量",
                event_code="memory.qdrant.graph_vector_profile_rejected",
                point_id=point_id,
                vector_signature=vector_signature,
                vector_dimension=vector_dimension,
                expected_signature=self._embedding_signature,
                expected_dimension=self.config.embedding_dimension,
            )
            return False
        try:
            await self._run_qdrant_io(
                self._client.upsert,
                collection_name=collection_name,
                points=[
                    qdrant_models.PointStruct(
                        id=self._normalize_point_id(point_id),
                        vector=vector,
                        payload=self._with_graph_embedding_metadata(point_id, payload, vector),
                    )
                ],
            )
            return True
        except Exception:
            logger.exception(
                "Qdrant 图向量写入失败",
                event_code="memory.qdrant.graph_vector_upsert_failed",
                point_id=point_id,
                vector_dimension=len(vector),
                collection=collection_name,
            )
            return False

    @_serialized_remote_qdrant
    async def batch_upsert_atom_vectors(
        self,
        points: list[tuple[str, list[float], dict[str, Any]]],
    ) -> int:
        """批量写入记忆原子向量

        Returns:
            int: 成功写入的数量
        """
        if not self._available:
            return len(points)
        if not self._client:
            return 0
        try:
            accepted_points = [(pid, vec, pl) for pid, vec, pl in points if self._embedding_vector_is_current(vec, pl)]
            if len(accepted_points) != len(points):
                logger.warning(
                    "批量原子向量中存在非当前 profile 的结果，已拒绝对应写入",
                    event_code="memory.qdrant.atom_vectors_profile_rejected",
                    rejected_count=len(points) - len(accepted_points),
                    expected_signature=self._embedding_signature,
                    expected_dimension=self.config.embedding_dimension,
                )
            point_structs = [
                qdrant_models.PointStruct(
                    id=self._normalize_point_id(pid),
                    vector=vec,
                    payload=self._with_atom_embedding_metadata(pid, pl, vec),
                )
                for pid, vec, pl in accepted_points
            ]
            if not point_structs:
                return 0
            await self._run_qdrant_io(
                self._client.upsert,
                collection_name=self._atom_write_collection(),
                points=point_structs,
            )
            return len(point_structs)
        except Exception:
            logger.exception(
                "Qdrant 原子向量批量写入失败",
                event_code="memory.qdrant.atom_vectors_batch_upsert_failed",
                count=len(points),
                collection=self._atom_write_collection(),
            )
            return 0

    # -- 向量检索 -----------------------------------------------------------

    @_serialized_remote_qdrant
    async def search_similar_atoms(
        self,
        query_vector: list[float],
        filters: Optional[dict[str, Any]] = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """向量检索相似记忆原子

        Args:
            query_vector: 查询向量
            filters: 过滤条件，如 {"atom_type": "episodic"}
            limit: 返回数量

        Returns:
            list[dict]: 每个元素包含 payload 和 score
        """
        if not self._available or not self.vector_search_enabled:
            return []
        if not self._embedding_vector_is_current(query_vector, {}):
            logger.info("忽略非当前 profile 的原子查询向量")
            return []

        try:
            qdrant_filter = self._build_filter(filters, self._atoms_payload_schema())
            results = await self._run_qdrant_io(
                self._query_points,
                collection_name=self.active_atoms_collection,
                query_vector=query_vector,
                qdrant_filter=qdrant_filter,
                limit=limit,
            )
            return [
                {
                    "id": str(hit.id),
                    "score": hit.score,
                    "payload": hit.payload,
                }
                for hit in results
            ]
        except Exception:
            logger.exception(
                "Qdrant 原子向量检索失败",
                event_code="memory.qdrant.atom_vector_search_failed",
                limit=limit,
                has_filters=bool(filters),
            )
            return []

    @_serialized_remote_qdrant
    async def search_similar_graph_entries(
        self,
        query_vector: list[float],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """向量检索相似图条目"""
        if not self._available or not self.graph_search_enabled:
            return []
        if not self._embedding_vector_is_current(query_vector, {}):
            logger.info("忽略非当前 profile 的图查询向量")
            return []

        try:
            results = await self._run_qdrant_io(
                self._query_points,
                collection_name=self.active_graph_collection,
                query_vector=query_vector,
                limit=limit,
            )
            return [
                {
                    "id": str(hit.id),
                    "score": hit.score,
                    "payload": hit.payload,
                }
                for hit in results
            ]
        except Exception:
            logger.exception(
                "Qdrant 图条目检索失败",
                event_code="memory.qdrant.graph_vector_search_failed",
                limit=limit,
            )
            return []

    async def _list_index_points(
        self,
        *,
        collection_name: str,
        business_id_key: str,
        index_label: str,
        payload_keys: tuple[str, ...] = ("embedding_source_hash",),
        page_size: int = 256,
    ) -> Optional[list[dict[str, Any]]]:
        """Read physical IDs and verified business IDs from one Qdrant collection."""
        if not self._available or not self._client:
            return None

        point_summaries: list[dict[str, Any]] = []
        offset: Any = None
        untrusted = 0
        try:
            while True:
                points, next_offset = await self._run_qdrant_io(
                    self._client.scroll,
                    collection_name=collection_name,
                    limit=page_size,
                    offset=offset,
                    with_payload=[business_id_key, "embedding_signature", "embedding_dimension", *payload_keys],
                    with_vectors=False,
                )
                for point in points:
                    physical_id = point.id
                    point_payload = getattr(point, "payload", None) or {}
                    payload_business_id = point_payload.get(business_id_key)
                    business_id: Optional[str] = None
                    if payload_business_id is not None:
                        candidate = str(payload_business_id)
                        if self._normalize_point_id(candidate) == self._normalize_point_id(physical_id):
                            business_id = candidate
                    if business_id is None:
                        untrusted += 1
                    point_summary = {"physical_id": physical_id, "business_id": business_id}
                    for metadata_key in (
                        "embedding_signature",
                        "embedding_dimension",
                        *payload_keys,
                    ):
                        if point_payload.get(metadata_key) is not None:
                            point_summary[metadata_key] = point_payload[metadata_key]
                    point_summaries.append(point_summary)

                if next_offset is None:
                    break
                if next_offset == offset:
                    raise RuntimeError("Qdrant scroll offset did not advance")
                offset = next_offset

            if untrusted:
                logger.warning(
                    "Qdrant 业务 ID 缺失或与物理 ID 不匹配",
                    event_code="memory.qdrant.index_ids_untrusted_payload",
                    index_name=index_label,
                    count=untrusted,
                )
            return point_summaries
        except Exception:
            logger.exception(
                "Qdrant point 列表获取失败",
                event_code="memory.qdrant.index_points_list_failed",
                index_name=index_label,
                collection=collection_name,
            )
            return None

    @_serialized_remote_qdrant
    async def list_atom_points(
        self,
        page_size: int = 256,
        collection_name: Optional[str] = None,
    ) -> Optional[list[dict[str, Any]]]:
        """分页读取 Qdrant 原子的物理 ID 与可信业务 ID。"""
        return await self._list_index_points(
            collection_name=collection_name or self.active_atoms_collection,
            business_id_key="atom_id",
            index_label=self.config.collection_name_atoms,
            payload_keys=ATOM_INDEX_SYNC_PAYLOAD_FIELDS,
            page_size=page_size,
        )

    @_serialized_remote_qdrant
    async def list_graph_points(
        self,
        page_size: int = 256,
        collection_name: Optional[str] = None,
    ) -> Optional[list[dict[str, Any]]]:
        """分页读取 Qdrant 图条目的物理 ID 与可信业务 ID。"""
        return await self._list_index_points(
            collection_name=collection_name or self.active_graph_collection,
            business_id_key="entry_id",
            index_label=self.config.collection_name_graph,
            page_size=page_size,
        )

    @_serialized_remote_qdrant
    async def list_atom_ids(
        self,
        page_size: int = 256,
        collection_name: Optional[str] = None,
    ) -> Optional[set[str]]:
        """分页读取 Qdrant 中已验证可映射回业务主键的原子 ID。"""
        atom_points = await self.list_atom_points(page_size=page_size, collection_name=collection_name)
        if atom_points is None:
            return None
        return {
            point["business_id"]
            for point in atom_points
            if isinstance(point.get("business_id"), str) and point["business_id"]
        }

    # -- 向量删除 -----------------------------------------------------------

    @_serialized_remote_qdrant
    async def delete_atom_vector(self, point_id: str | int) -> bool:
        """删除指定记忆原子的向量"""
        if not self._available:
            return True
        if not self._client:
            return False
        for collection_name in self._atom_mutation_collections():
            if not await self.delete_atom_vector_from_collection(collection_name, point_id):
                return False
        return True

    @_serialized_remote_qdrant
    async def delete_atom_vector_from_collection(
        self,
        collection_name: str,
        point_id: str | int,
    ) -> bool:
        """Delete one atom point from an explicit physical collection or alias."""
        if not self._available:
            return True
        if not self._client:
            return False
        try:
            await self._run_qdrant_io(
                self._client.delete,
                collection_name=collection_name,
                points_selector=qdrant_models.PointIdsList(points=[self._normalize_point_id(point_id)]),
            )
            return True
        except Exception:
            logger.exception(
                "Qdrant 原子向量删除失败",
                event_code="memory.qdrant.atom_vector_delete_failed",
                point_id=point_id,
                collection=collection_name,
            )
            return False

    @_serialized_remote_qdrant
    async def set_atom_payload(self, point_id: str, payload: dict[str, Any]) -> bool:
        """更新 Qdrant 中记忆原子的 payload 字段（不改变向量）

        用于权重、状态、置信度等非内容字段的增量更新，
        无需重新生成 embedding。

        Args:
            point_id: 原子 ID
            payload: 要设置/更新的字段字典

        Returns:
            bool: 是否成功
        """
        if not self._available:
            return True
        if not self._client:
            return False
        updated = False
        for collection_name in self._atom_mutation_collections():
            try:
                await self._run_qdrant_io(
                    self._client.set_payload,
                    collection_name=collection_name,
                    payload=payload,
                    points=[self._normalize_point_id(point_id)],
                )
                updated = True
            except KeyError:
                logger.debug(
                    "Qdrant 原子不存在，等待一致性协调",
                    event_code="memory.qdrant.atom_payload_missing",
                    point_id=point_id,
                    collection=collection_name,
                )
            except Exception:
                logger.exception(
                    "Qdrant 原子 payload 设置失败",
                    event_code="memory.qdrant.atom_payload_set_failed",
                    point_id=point_id,
                    payload_keys=list(payload.keys()),
                    collection=collection_name,
                )
                return False
        return updated

    @_serialized_remote_qdrant
    async def delete_graph_vector(self, entry_id: str) -> bool:
        """删除指定图条目的向量"""
        if not self._available:
            return True
        if not self._client:
            return False
        for collection_name in self._graph_mutation_collections():
            if not await self.delete_graph_vector_from_collection(collection_name, entry_id):
                return False
        return True

    @_serialized_remote_qdrant
    async def delete_graph_vector_from_collection(self, collection_name: str, entry_id: str | int) -> bool:
        """Delete one graph point from an explicit physical collection or alias."""
        if not self._available:
            return True
        if not self._client:
            return False
        try:
            await self._run_qdrant_io(
                self._client.delete,
                collection_name=collection_name,
                points_selector=qdrant_models.PointIdsList(points=[self._normalize_point_id(entry_id)]),
            )
            return True
        except Exception:
            logger.exception(
                "Qdrant 图向量删除失败",
                event_code="memory.qdrant.graph_vector_delete_failed",
                entry_id=entry_id,
                collection=collection_name,
            )
            return False

    def _mark_index_migration_progress(
        self,
        *,
        index_name: str,
        target_collection: Optional[str],
        last_processed_id: Optional[str],
        migrated_count: int,
        total_count: int,
    ) -> None:
        if not target_collection:
            return
        self._save_vector_state(
            index_name=index_name,
            status="migrating",
            target_signature=self._embedding_signature,
            target_dimension=self.config.embedding_dimension,
            target_collection=target_collection,
            last_processed_id=last_processed_id,
            migrated_count=max(0, int(migrated_count)),
            total_count=max(0, int(total_count)),
            last_error=None,
        )

    def _mark_index_migration_failure(
        self,
        *,
        index_name: str,
        target_collection: Optional[str],
        error: str,
    ) -> None:
        if not target_collection:
            return
        self._save_vector_state(
            index_name=index_name,
            status="failed",
            target_signature=self._embedding_signature,
            target_dimension=self.config.embedding_dimension,
            target_collection=target_collection,
            last_error=str(error)[:4000],
        )

    async def _activate_index_migration(
        self,
        *,
        index_name: str,
        alias_name: str,
        target: Optional[str],
    ) -> bool:
        if not target or not self._client:
            return False

        try:
            await self._run_qdrant_io(self._switch_alias, alias_name, target)
        except Exception:
            logger.exception(
                "Qdrant 向量迁移 alias 切换失败",
                event_code="memory.qdrant.migration_activation_failed",
                index_name=index_name,
                target_collection=target,
            )
            return False

        try:
            self._save_vector_state(
                index_name=index_name,
                active_signature=self._embedding_signature,
                active_dimension=self.config.embedding_dimension,
                active_collection=target,
                target_signature=None,
                target_dimension=None,
                target_collection=None,
                status="ready",
                last_processed_id=None,
                last_error=None,
            )
        except Exception:
            logger.exception(
                "Qdrant alias 已切换，但迁移状态保存失败",
                event_code="memory.qdrant.migration_state_activation_failed",
                index_name=index_name,
                target_collection=target,
            )
            return False

        logger.info(
            "Qdrant 向量索引迁移已激活",
            event_code="memory.qdrant.migration_activated",
            index_name=index_name,
            active_collection=target,
            embedding_signature=self._embedding_signature,
            embedding_dimension=self.config.embedding_dimension,
        )
        return True

    @_serialized_remote_qdrant
    async def mark_atom_migration_progress(
        self,
        *,
        last_processed_id: Optional[str],
        migrated_count: int,
        total_count: int,
    ) -> None:
        """Persist a resumable atom migration cursor after a completed batch."""
        self._mark_index_migration_progress(
            index_name=self.config.collection_name_atoms,
            target_collection=self._atom_migration_target,
            last_processed_id=last_processed_id,
            migrated_count=migrated_count,
            total_count=total_count,
        )

    @_serialized_remote_qdrant
    async def mark_atom_migration_failure(self, error: str) -> None:
        """Record an atom migration failure without changing the active alias."""
        self._mark_index_migration_failure(
            index_name=self.config.collection_name_atoms,
            target_collection=self._atom_migration_target,
            error=error,
        )

    @_serialized_remote_qdrant
    async def activate_atom_migration(self) -> bool:
        """Atomically point the stable atom alias at a fully rebuilt collection."""
        target = self._atom_migration_target
        if not await self._activate_index_migration(
            index_name=self.config.collection_name_atoms,
            alias_name=self._atom_alias_name,
            target=target,
        ):
            return False
        self._active_atoms_collection = str(target)
        self._atom_migration_target = None
        self._vector_search_enabled = True
        return True

    @_serialized_remote_qdrant
    async def mark_graph_migration_progress(
        self,
        *,
        last_processed_id: Optional[str],
        migrated_count: int,
        total_count: int,
    ) -> None:
        """Persist a resumable graph migration cursor after a completed batch."""
        self._mark_index_migration_progress(
            index_name=self.config.collection_name_graph,
            target_collection=self._graph_migration_target,
            last_processed_id=last_processed_id,
            migrated_count=migrated_count,
            total_count=total_count,
        )

    @_serialized_remote_qdrant
    async def mark_graph_migration_failure(self, error: str) -> None:
        """Record a graph migration failure without changing the active alias."""
        self._mark_index_migration_failure(
            index_name=self.config.collection_name_graph,
            target_collection=self._graph_migration_target,
            error=error,
        )

    @_serialized_remote_qdrant
    async def activate_graph_migration(self) -> bool:
        """Atomically point the stable graph alias at a fully rebuilt collection."""
        target = self._graph_migration_target
        if not await self._activate_index_migration(
            index_name=self.config.collection_name_graph,
            alias_name=self._graph_alias_name,
            target=target,
        ):
            return False
        self._active_graph_collection = str(target)
        self._graph_migration_target = None
        self._graph_search_enabled = True
        return True

    # -- 集合管理工具 -------------------------------------------------------

    @_serialized_remote_qdrant
    async def collection_info(self, collection_name: str) -> Optional[dict[str, Any]]:
        """获取集合信息"""
        if not self._available or not self._client:
            return None
        try:
            info = await self._run_qdrant_io(self._client.get_collection, collection_name)
            return {
                "name": collection_name,
                "vectors_count": info.points_count,
                "status": info.status,
            }
        except Exception:
            logger.exception(
                "Qdrant 集合信息获取失败",
                event_code="memory.qdrant.collection_info_failed",
                collection=collection_name,
            )
            return None

    @_serialized_remote_qdrant
    async def delete_collection(self, collection_name: str) -> bool:
        """删除集合"""
        if not self._available or not self._client:
            return False
        try:
            await self._run_qdrant_io(self._client.delete_collection, collection_name)
            logger.info("Qdrant 集合已删除", event_code="memory.qdrant.collection_deleted", collection=collection_name)
            return True
        except Exception:
            logger.exception(
                "Qdrant 集合删除失败",
                event_code="memory.qdrant.collection_delete_failed",
                collection=collection_name,
            )
            return False


# ---------------------------------------------------------------------------
# MemoryStore — 统一记忆存储入口
# ---------------------------------------------------------------------------


class MemoryStore:
    """统一记忆存储入口，封装 SQLite + Qdrant 双层存储

    使用方式:
        store = MemoryStore(config)
        await store.initialize()
        # ...
        store = MemoryStore.get_instance()
    """

    _instance: Optional["MemoryStore"] = None

    def __new__(cls, *args, **kwargs) -> "MemoryStore":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False  # type: ignore
        return cls._instance

    def __init__(self, config: Optional[MemoryStoreConfig] = None):
        if self._initialized:  # type: ignore
            return

        self.config = config or MemoryStoreConfig()
        self.qdrant = QdrantManager(self.config)
        self._bm25_retriever: Any = None

    def get_bm25_retriever(self) -> Any:
        """Return the store-owned BM25 index shared by all memory retrievers."""
        if self._bm25_retriever is None:
            from src.memory.bm25_retrieval import BM25Retriever

            self._bm25_retriever = BM25Retriever(self)
        return self._bm25_retriever

    def _invalidate_bm25_index(self) -> None:
        if self._bm25_retriever is not None:
            self._bm25_retriever.invalidate_cache()

    def _notify_bm25_update(self, atom_id: str, updates: dict[str, Any]) -> None:
        if self._bm25_retriever is None:
            return
        if _BM25_REBUILD_FIELDS & updates.keys():
            self._bm25_retriever.invalidate_cache()
            return
        self._bm25_retriever.update_cached_metadata(atom_id, updates)

    @classmethod
    def get_instance(cls) -> "MemoryStore":
        """获取 MemoryStore 单例实例"""
        if cls._instance is None:
            logger.error("MemoryStore 尚未初始化就被调用", caller="get_instance")
            raise RuntimeError("MemoryStore 未初始化，请先调用 MemoryStore(config)")
        return cls._instance

    async def initialize(self) -> None:
        """异步初始化：确保数据库表存在 + 连接 Qdrant"""
        # 仅当用户显式改了 sqlite_path 时重设 ORM 数据库路径；默认路径保持 schema.py 的绑定，避免破坏测试中的临时 DB 绑定。
        if self.config.sqlite_path != MemoryStoreConfig.sqlite_path:
            configure_memory_database(self.config.sqlite_path)
        # 确保 SQLite 数据库表已创建（从 schema 模块级自动初始化移至此处）
        initialize_database()
        await self.qdrant.initialize()
        self._initialized = True
        logger.info(
            "MemoryStore 初始化完成", event_code="memory.store.initialized", sqlite_path=self.config.sqlite_path
        )

    async def close(self) -> None:
        """关闭存储连接"""
        await self.qdrant.close()
        if not memory_db.is_closed():
            memory_db.close()
        self._bm25_retriever = None
        self._initialized = False
        type(self)._instance = None
        logger.info("MemoryStore 已关闭", event_code="memory.store.closed")

    # -- 原子 CRUD ----------------------------------------------------------

    async def insert_atom(self, atom_data: dict[str, Any]) -> str:
        """插入一条记忆原子

        Args:
            atom_data: 原子字段字典（不含 atom_id，自动生成）

        Returns:
            str: 生成的 atom_id
        """
        atom_data = dict(atom_data)
        atom_id = str(atom_data.get("atom_id") or uuid.uuid4())
        atom_data["atom_id"] = atom_id
        _normalize_datetime_fields(atom_data, fill_missing=True)

        # JSON 字段序列化
        if isinstance(atom_data.get("entities"), (list, dict)):
            atom_data["entities"] = json.dumps(atom_data["entities"], ensure_ascii=False)

        try:
            with memory_db.atomic():
                MemoryAtom.create(**atom_data)
        except Exception:
            logger.exception("记忆原子写入失败", event_code="memory.atom.insert_failed", atom_id=atom_id)
            raise

        self._invalidate_bm25_index()

        logger.debug(
            "记忆原子写入完成",
            event_code="memory.atom.inserted",
            atom_id=atom_id,
            atom_type=atom_data.get("atom_type", "unknown"),
        )
        return atom_id

    async def update_atom(self, atom_id: str, updates: dict[str, Any]) -> bool:
        """更新记忆原子

        Args:
            atom_id: 原子 ID
            updates: 要更新的字段字典

        Returns:
            bool: 是否成功
        """
        updates = dict(updates)
        _normalize_datetime_fields(updates)

        # JSON 字段序列化
        if isinstance(updates.get("entities"), (list, dict)):
            updates["entities"] = json.dumps(updates["entities"], ensure_ascii=False)

        try:
            with memory_db:
                query = MemoryAtom.update(**updates).where(MemoryAtom.atom_id == atom_id)
                rows = query.execute()
            if rows > 0:
                self._notify_bm25_update(atom_id, updates)
                logger.debug("记忆原子更新完成", event_code="memory.atom.updated", atom_id=atom_id, rows=rows)
            return rows > 0
        except Exception:
            logger.exception("记忆原子更新失败", event_code="memory.atom.update_failed", atom_id=atom_id)
            return False

    async def update_atoms_batch(
        self,
        updates_list: list[tuple[str, dict[str, Any]]],
    ) -> int:
        """批量更新记忆原子（单事务）

        在单个事务中执行所有更新，将 N 次独立事务压缩为 1 次。
        每个更新仍为独立 SQL UPDATE，但共享同一个数据库事务。

        Args:
            updates_list: [(atom_id, updates_dict), ...]

        Returns:
            int: 成功更新的行数
        """
        if not updates_list:
            return 0
        count = 0
        successful_updates: list[tuple[str, dict[str, Any]]] = []
        try:
            with memory_db.atomic():
                for atom_id, updates in updates_list:
                    normalized_updates = dict(updates)
                    _normalize_datetime_fields(normalized_updates)
                    if isinstance(normalized_updates.get("entities"), (list, dict)):
                        normalized_updates["entities"] = json.dumps(normalized_updates["entities"], ensure_ascii=False)
                    query = MemoryAtom.update(**normalized_updates).where(MemoryAtom.atom_id == atom_id)
                    rows = query.execute()
                    count += rows
                    if rows > 0:
                        successful_updates.append((atom_id, normalized_updates))
            if any(_BM25_REBUILD_FIELDS & updates.keys() for _, updates in successful_updates):
                self._invalidate_bm25_index()
            else:
                for atom_id, updates in successful_updates:
                    self._notify_bm25_update(atom_id, updates)
            return count
        except Exception:
            logger.exception(
                "记忆原子批量更新失败", event_code="memory.atom.batch_update_failed", count=len(updates_list)
            )
            return 0

    async def delete_atom(self, atom_id: str) -> bool:
        """删除记忆原子及其向量索引

        Args:
            atom_id: 原子 ID

        Returns:
            bool: 是否成功
        """
        try:
            with memory_db.atomic():
                EpisodicDetail.delete().where(EpisodicDetail.atom == atom_id).execute()
                SemanticDetail.delete().where(SemanticDetail.atom == atom_id).execute()
                MemoryTraceChain.delete().where(MemoryTraceChain.atom_id == atom_id).execute()
                ConflictObservation.delete().where(
                    (ConflictObservation.atom_a_id == atom_id) | (ConflictObservation.atom_b_id == atom_id)
                ).execute()
                AtomAssociationModel.delete().where(
                    (AtomAssociationModel.atom_a_id == atom_id) | (AtomAssociationModel.atom_b_id == atom_id)
                ).execute()
                query = MemoryAtom.delete().where(MemoryAtom.atom_id == atom_id)
                rows = query.execute()
            if rows > 0:
                self._invalidate_bm25_index()
                await self.qdrant.delete_atom_vector(atom_id)
                logger.debug("记忆原子已删除", event_code="memory.atom.deleted", atom_id=atom_id)
            return rows > 0
        except Exception:
            logger.exception("记忆原子删除失败", event_code="memory.atom.delete_failed", atom_id=atom_id)
            return False

    async def archive_atom(self, atom_id: str) -> bool:
        """归档记忆原子并从向量索引中移除。"""
        try:
            atom = MemoryAtom.get_or_none(MemoryAtom.atom_id == atom_id)
            if atom is None:
                logger.warning(
                    "记忆原子归档失败，原子不存在", event_code="memory.atom.archive_missing", atom_id=atom_id
                )
                return False

            metadata: dict[str, Any] = {
                "atom_id": atom.atom_id,
                "atom_type": atom.atom_type,
                "importance": atom.importance,
                "confidence": atom.confidence,
                "weight": atom.weight,
                "source_scene": atom.source_scene,
                "source_id": atom.source_id,
                "privacy_level": atom.privacy_level,
                "reinforcement_count": atom.reinforcement_count,
                "ttl_days": atom.ttl_days,
                "decay_type": atom.decay_type,
                "trace_chain_id": atom.trace_chain_id,
                "embedding_id": atom.embedding_id,
            }
            timestamp = atom.created_at.timestamp() if atom.created_at else datetime.datetime.now().timestamp()

            with memory_db.atomic():
                RawMessageArchive.create(
                    stream_id=f"memory_archive_{atom.source_scene or 'unknown'}",
                    message_id=atom.atom_id,
                    user_id="system",
                    content=json.dumps(
                        {"content": atom.content, "metadata": metadata},
                        ensure_ascii=False,
                    ),
                    timestamp=timestamp,
                    chat_type=f"memory_archive_{atom.atom_type or 'unknown'}",
                )
                rows = MemoryAtom.update(status="archived").where(MemoryAtom.atom_id == atom_id).execute()

            if rows > 0:
                self._invalidate_bm25_index()
                await self.qdrant.delete_atom_vector(atom_id)
                logger.debug("记忆原子已归档", event_code="memory.atom.archived", atom_id=atom_id)
            return rows > 0
        except Exception:
            logger.exception("记忆原子归档失败", event_code="memory.atom.archive_failed", atom_id=atom_id)
            return False

    async def migrate_atom(self, atom_id: str, target_type: str) -> bool:
        """迁移记忆原子的类型，并同步 Qdrant payload。"""
        if not target_type:
            logger.warning(
                "记忆原子迁移失败，目标类型为空", event_code="memory.atom.migrate_empty_target", atom_id=atom_id
            )
            return False

        try:
            rows = MemoryAtom.update(atom_type=target_type).where(MemoryAtom.atom_id == atom_id).execute()
            if rows > 0:
                self._notify_bm25_update(atom_id, {"atom_type": target_type})
                await self.qdrant.set_atom_payload(atom_id, {"atom_type": target_type})
                logger.debug(
                    "记忆原子类型已迁移", event_code="memory.atom.migrated", atom_id=atom_id, target_type=target_type
                )
            return rows > 0
        except Exception:
            logger.exception(
                "记忆原子迁移失败",
                event_code="memory.atom.migrate_failed",
                atom_id=atom_id,
                target_type=target_type,
            )
            return False

    async def get_atom(self, atom_id: str) -> Optional[dict[str, Any]]:
        """获取单条记忆原子

        Args:
            atom_id: 原子 ID

        Returns:
            Optional[dict]: 原子数据，不存在时返回 None
        """
        try:
            atom = MemoryAtom.get_or_none(MemoryAtom.atom_id == atom_id)
            if atom is None:
                return None
            return self._atom_to_dict(atom)
        except Exception:
            logger.exception("记忆原子获取失败", event_code="memory.atom.get_failed", atom_id=atom_id)
            return None

    async def get_atoms_batch(self, atom_ids: list[str]) -> dict[str, dict[str, Any]]:
        """批量获取记忆原子（单查询）

        使用 SELECT ... WHERE atom_id IN (...) 一次加载所有原子，
        将 N 次独立查询压缩为 1 次。

        Args:
            atom_ids: 原子 ID 列表

        Returns:
            dict[str, dict]: {atom_id: atom_data_dict} 映射，不存在的 ID 不包含在结果中
        """
        if not atom_ids:
            return {}

        def read_atoms():
            atoms = MemoryAtom.select().where(MemoryAtom.atom_id.in_(atom_ids))
            return {atom.atom_id: self._atom_to_dict(atom) for atom in atoms}

        try:
            return await self._run_sqlite_read(read_atoms)
        except Exception:
            logger.exception("记忆原子批量获取失败", event_code="memory.atom.batch_get_failed", count=len(atom_ids))
            return {}

    @staticmethod
    async def _run_sqlite_read(read):
        """Offload bulk reads while preserving connection-local transaction visibility.

        Writes and BM25 cache updates keep their existing ordered execution.
        Cancellation drains the worker before connection reconfiguration/close can
        proceed; reads never publish cache changes.
        """
        if memory_db.in_transaction() or memory_db.database == ":memory:":
            return read()

        def read_with_connection():
            with memory_db.connection_context():
                return read()

        # An executor Future is not returned by all_tasks(): shutdown may cancel
        # every Task, but must not erase our handle to an active SQLite reader.
        context = contextvars.copy_context()
        operation = asyncio.get_running_loop().run_in_executor(None, context.run, read_with_connection)
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not operation.cancelled():
                operation.exception()
            raise

    async def list_atom_ids(self, status: Optional[str] = None) -> Optional[set[str]]:
        """读取 SQLite 中的记忆原子 ID；查询失败时返回 ``None``。"""
        try:
            with memory_db:
                query = MemoryAtom.select(MemoryAtom.atom_id)
                if status:
                    query = query.where(MemoryAtom.status == status)
                return {str(atom.atom_id) for atom in query}
        except Exception:
            logger.exception(
                "记忆原子 ID 列表获取失败",
                event_code="memory.atom.ids_list_failed",
                status=status,
            )
            return None

    async def list_atom_source_hashes(self, status: Optional[str] = None) -> Optional[dict[str, str]]:
        """读取 SQLite 原子当前内容的稳定哈希，用于识别陈旧向量。"""
        from src.llm_models.embedding import embedding_source_hash

        try:
            with memory_db:
                query = MemoryAtom.select(MemoryAtom.atom_id, MemoryAtom.content)
                if status:
                    query = query.where(MemoryAtom.status == status)
                return {str(atom.atom_id): embedding_source_hash(str(atom.content or "")) for atom in query}
        except Exception:
            logger.exception(
                "记忆原子内容哈希列表获取失败",
                event_code="memory.atom.source_hashes_list_failed",
                status=status,
            )
            return None

    async def list_atom_index_payloads(self, status: Optional[str] = None) -> Optional[dict[str, dict[str, Any]]]:
        """读取 SQLite 原子的 Qdrant 索引载荷真值，用于识别 payload 漂移。"""
        from src.llm_models.embedding import embedding_source_hash

        try:
            with memory_db:
                query = MemoryAtom.select()
                if status:
                    query = query.where(MemoryAtom.status == status)
                payloads: dict[str, dict[str, Any]] = {}
                for atom in query:
                    payloads[str(atom.atom_id)] = {
                        "atom_type": atom.atom_type,
                        "weight": atom.weight,
                        "importance": atom.importance,
                        "confidence": atom.confidence,
                        "status": atom.status,
                        "source_scene": atom.source_scene,
                        "source_id": atom.source_id,
                        "privacy_level": atom.privacy_level,
                        "embedding_source_hash": embedding_source_hash(str(atom.content or "")),
                    }
                return payloads
        except Exception:
            logger.exception(
                "记忆原子索引载荷列表获取失败",
                event_code="memory.atom.index_payloads_list_failed",
                status=status,
            )
            return None

    async def list_atoms(
        self,
        atom_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """列出记忆原子

        Args:
            atom_type: 按类型过滤
            status: 按状态过滤
            limit: 返回数量上限
            offset: 偏移量

        Returns:
            list[dict]: 原子数据列表
        """

        def read_atoms():
            conditions = []
            if atom_type:
                conditions.append(MemoryAtom.atom_type == atom_type)
            if status:
                conditions.append(MemoryAtom.status == status)

            if conditions:
                query = MemoryAtom.select().where(*conditions)
            else:
                query = MemoryAtom.select()
            query = query.order_by(MemoryAtom.created_at.desc()).limit(limit).offset(offset)
            return [self._atom_to_dict(a) for a in query]

        try:
            return await self._run_sqlite_read(read_atoms)
        except Exception:
            logger.exception(
                "记忆原子列表获取失败",
                event_code="memory.atom.list_failed",
                atom_type=atom_type,
                status=status,
                limit=limit,
                offset=offset,
            )
            return []

    # -- 向量检索 -----------------------------------------------------------

    async def search_similar(
        self,
        query_vector: list[float],
        filters: Optional[dict[str, Any]] = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """向量检索相似记忆原子

        Args:
            query_vector: 查询向量
            filters: Qdrant 过滤条件
            limit: 返回数量

        Returns:
            list[dict]: 检索结果，含 payload 和 score
        """
        results = await self.qdrant.search_similar_atoms(query_vector, filters, limit)
        logger.debug(
            "记忆向量搜索完成",
            event_code="memory.vector_search.completed",
            query_len=len(query_vector),
            results_count=len(results),
            limit=limit,
            has_filters=bool(filters),
        )
        if not results:
            logger.debug(
                "记忆向量搜索结果为空",
                event_code="memory.vector_search.empty",
                query_len=len(query_vector),
                limit=limit,
            )
        return results

    # -- Qdrant 重建 --------------------------------------------------------

    async def rebuild_qdrant_index(self) -> int:
        """执行一个可恢复的 Qdrant 重建批次。

        Returns:
            int: 本批次重建的向量数量
        """
        if not getattr(self.qdrant, "atom_migration_pending", False):
            return 0

        from src.memory.vector_migration import VectorIndexMigrationTask

        task = VectorIndexMigrationTask(
            self,
            batch_size=self.config.vector_batch_size,
        )
        return await task.run()

    # -- 统计信息 -----------------------------------------------------------

    async def get_statistics(self) -> dict[str, Any]:
        """获取记忆存储统计信息"""
        try:
            with memory_db:
                total_atoms = MemoryAtom.select().count()
                active_atoms = MemoryAtom.select().where(MemoryAtom.status == "active").count()
                type_distribution = {}
                for atom_type in ["episodic", "factual", "relational", "preference", "planned"]:
                    count = MemoryAtom.select().where(MemoryAtom.atom_type == atom_type).count()
                    if count > 0:
                        type_distribution[atom_type] = count

            active_collection = getattr(
                self.qdrant,
                "active_atoms_collection",
                self.config.collection_name_atoms,
            )
            qdrant_info = await self.qdrant.collection_info(active_collection)

            return {
                "total_atoms": total_atoms,
                "active_atoms": active_atoms,
                "type_distribution": type_distribution,
                "qdrant_available": QDRANT_AVAILABLE and self.qdrant._client is not None,
                "qdrant_atoms_collection": qdrant_info,
            }
        except Exception as e:
            logger.exception("记忆存储统计信息获取失败", event_code="memory.store.stats_failed")
            return {"error": str(e)}

    # -- 内部工具 -----------------------------------------------------------

    @staticmethod
    def _atom_to_dict(atom: MemoryAtom) -> dict[str, Any]:
        """将 MemoryAtom Peewee 实例转为字典"""
        data = {
            "atom_id": atom.atom_id,
            "atom_type": atom.atom_type,
            "content": atom.content,
            "importance": atom.importance,
            "confidence": atom.confidence,
            "weight": atom.weight,
            "created_at": atom.created_at.isoformat() if atom.created_at else None,
            "last_accessed_at": atom.last_accessed_at.isoformat() if atom.last_accessed_at else None,
            "last_reinforced_at": atom.last_reinforced_at.isoformat() if atom.last_reinforced_at else None,
            "ttl_days": atom.ttl_days,
            "decay_type": atom.decay_type,
            "reinforcement_count": atom.reinforcement_count,
            "source_scene": atom.source_scene,
            "source_id": atom.source_id,
            "privacy_level": atom.privacy_level,
            "status": atom.status,
            "trace_chain_id": atom.trace_chain_id,
            "embedding_id": atom.embedding_id,
        }
        # 反序列化 JSON 字段
        if atom.entities:
            try:
                data["entities"] = json.loads(atom.entities)
            except (json.JSONDecodeError, TypeError):
                data["entities"] = atom.entities
        return data
