"""人物信息管理 API 路由

对接新记忆系统的 user_profiles 表，并保持旧 WebUI PersonInfo API 兼容。
"""

from fastapi import APIRouter, HTTPException, Header, Query, Cookie
from pydantic import BaseModel, Field
from typing import Any, Iterator, Optional, List, Dict
from src.common.logger import get_logger
from src.webui.error_utils import internal_server_error
from .auth import verify_auth_token_from_cookie_or_header
import json
import zlib
from collections import Counter
from starlette.concurrency import run_in_threadpool
from datetime import datetime

from src.memory.user_profile import ProfileStore, UserProfile

logger = get_logger("webui.person")

# 创建路由器
router = APIRouter(prefix="/person", tags=["Person"])

_WEBUI_META_KEY = "_webui_person_info"


class PersonInfoResponse(BaseModel):
    """人物信息响应"""

    id: int
    is_known: bool
    person_id: str
    person_name: Optional[str]
    name_reason: Optional[str]
    platform: str
    user_id: str
    nickname: Optional[str]
    group_nick_name: Optional[List[Dict[str, str]]]  # 解析后的 JSON
    memory_points: Optional[str]
    know_times: Optional[float]
    know_since: Optional[float]
    last_know: Optional[float]
    profile_traits: Dict[str, float] = Field(default_factory=dict)
    profile_interests: List[str] = Field(default_factory=list)
    profile_preferences: Dict[str, str] = Field(default_factory=dict)
    profile_facts: Dict[str, str] = Field(default_factory=dict)
    profile_stats: Dict[str, Any] = Field(default_factory=dict)
    profile_expression_style: Optional[str] = None
    profile_expression_patterns: Dict[str, Any] = Field(default_factory=dict)
    mood_history_count: int = 0
    last_extracted_at: Optional[float] = None
    person_type: str = "person"
    identity_source: str = "manual"
    verification_status: str = "verified"
    cardname: Optional[str] = None


class PersonListResponse(BaseModel):
    """人物列表响应"""

    success: bool
    total: int
    page: int
    page_size: int
    data: List[PersonInfoResponse]


class PersonDetailResponse(BaseModel):
    """人物详情响应"""

    success: bool
    data: PersonInfoResponse


class PersonUpdateRequest(BaseModel):
    """人物信息更新请求"""

    person_name: Optional[str] = None
    name_reason: Optional[str] = None
    nickname: Optional[str] = None
    memory_points: Optional[str] = None
    is_known: Optional[bool] = None


class PersonUpdateResponse(BaseModel):
    """人物信息更新响应"""

    success: bool
    message: str
    data: Optional[PersonInfoResponse] = None


class PersonDeleteResponse(BaseModel):
    """人物删除响应"""

    success: bool
    message: str


class BatchDeleteRequest(BaseModel):
    """批量删除请求"""

    person_ids: List[str]


class BatchDeleteResponse(BaseModel):
    """批量删除响应"""

    success: bool
    message: str
    deleted_count: int
    failed_count: int
    failed_ids: List[str] = []


def verify_auth_token(
    maibot_session: Optional[str] = None,
    authorization: Optional[str] = None,
) -> bool:
    """验证认证 Token，支持 Cookie 和 Header"""
    return verify_auth_token_from_cookie_or_header(maibot_session, authorization)


def parse_group_nick_name(group_nick_name_str: Optional[str]) -> Optional[List[Dict[str, str]]]:
    """解析群昵称 JSON 字符串"""
    if not group_nick_name_str:
        return None
    try:
        return json.loads(group_nick_name_str)
    except (json.JSONDecodeError, TypeError):
        return None


def _stable_int_id(value: str) -> int:
    """从 user_id 生成稳定的正整数 ID，兼容旧前端表格 key。"""
    return zlib.crc32(value.encode("utf-8")) & 0x7FFFFFFF


def _to_timestamp(value: Any) -> Optional[float]:
    """将画像时间字段转换为前端使用的秒级时间戳。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_fact(profile: UserProfile, *keys: str) -> Optional[str]:
    """按候选键从 profile.facts 中取第一个非空字符串。"""
    for key in keys:
        value = profile.facts.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _profile_meta(profile: UserProfile) -> Dict[str, Any]:
    """读取 WebUI 私有元数据，不参与画像语义聚合。"""
    meta = profile.stats.get(_WEBUI_META_KEY) if isinstance(profile.stats, dict) else None
    return meta if isinstance(meta, dict) else {}


def _first_meta(profile: UserProfile, *keys: str) -> Optional[str]:
    """按候选键从 WebUI 私有元数据中取第一个非空字符串。"""
    meta = _profile_meta(profile)
    for key in keys:
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _profile_platform(profile: UserProfile) -> str:
    """直接返回身份层平台，只对老画像保留旧事实回退。"""
    legacy_platform = _first_meta(profile, "platform") or _first_fact(profile, "platform", "平台")
    if profile.platform == "legacy" and legacy_platform:
        return legacy_platform
    return profile.platform or legacy_platform or "legacy"


def _profile_display_name(profile: UserProfile) -> str:
    """从画像事实中恢复显示名，缺省使用 user_id。"""
    return (
        _first_meta(profile, "person_name", "display_name")
        or _first_fact(profile, "person_name", "display_name", "name", "username", "昵称", "姓名")
        or profile.nickname
        or profile.user_id
    )


def _profile_nickname(profile: UserProfile) -> Optional[str]:
    """从画像事实中恢复昵称。"""
    return (
        _first_meta(profile, "nickname")
        or profile.nickname
        or _first_fact(
            profile,
            "nickname",
            "nick_name",
            "user_nickname",
            "群昵称",
        )
    )


def _public_profile_stats(profile: UserProfile) -> Dict[str, Any]:
    """过滤 WebUI/内部私有统计字段后返回给前端展示。"""
    if not isinstance(profile.stats, dict):
        return {}
    return {key: value for key, value in profile.stats.items() if not key.startswith("_")}


def _group_nicknames(profile: UserProfile) -> Optional[List[Dict[str, str]]]:
    """读取 WebUI 兼容的群昵称列表。"""
    if profile.group_nicknames:
        return [dict(item) for item in profile.group_nicknames]
    raw = profile.facts.get("group_nick_name") or profile.facts.get("group_nicknames")
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        parsed = parse_group_nick_name(raw)
        if parsed:
            return parsed
    return None


def _profile_search_text(person: Dict[str, Any]) -> str:
    """构建搜索文本。"""
    parts = [
        person.get("person_id"),
        person.get("person_name"),
        person.get("nickname"),
        person.get("platform"),
        person.get("user_id"),
        person.get("memory_points"),
        person.get("name_reason"),
    ]
    parts.extend(person.get("profile_interests") or [])
    parts.append(json.dumps(person.get("profile_preferences") or {}, ensure_ascii=False))
    parts.append(json.dumps(person.get("profile_facts") or {}, ensure_ascii=False))
    return "\n".join(str(part) for part in parts if part).lower()


def profile_to_person_dict(profile: UserProfile) -> Dict[str, Any]:
    """将新记忆画像转换成旧 WebUI PersonInfo 形状。"""
    meta = _profile_meta(profile)
    default_known = profile.person_type == "person" and profile.verification_status == "verified"
    is_known = default_known and bool(meta.get("is_known", True))
    name_reason = _first_meta(profile, "name_reason") or _first_fact(profile, "name_reason") or "来自新记忆系统用户画像"
    person_name = _profile_display_name(profile)

    return {
        "id": _stable_int_id(profile.profile_id),
        "is_known": bool(is_known),
        "person_id": profile.profile_id,
        "person_name": person_name,
        "name_reason": name_reason,
        "platform": _profile_platform(profile),
        "user_id": profile.user_id,
        "nickname": _profile_nickname(profile),
        "group_nick_name": _group_nicknames(profile),
        "memory_points": _first_meta(profile, "memory_points") or profile.impression or None,
        "know_times": _to_timestamp(profile.created_at),
        "know_since": _to_timestamp(profile.created_at),
        "last_know": _to_timestamp(profile.updated_at),
        "profile_traits": dict(profile.traits or {}),
        "profile_interests": list(profile.interests or []),
        "profile_preferences": dict(profile.preferences or {}),
        "profile_facts": dict(profile.facts or {}),
        "profile_stats": _public_profile_stats(profile),
        "profile_expression_style": profile.expression_style or None,
        "profile_expression_patterns": dict(profile.expression_patterns or {}),
        "mood_history_count": len(profile.mood_history or []),
        "last_extracted_at": _to_timestamp(profile.last_extracted_at),
        "person_type": profile.person_type,
        "identity_source": profile.identity_source,
        "verification_status": profile.verification_status,
        "cardname": profile.cardname or None,
    }


def _iter_profile_person_dicts(
    search: Optional[str] = None,
    platform: Optional[str] = None,
    is_known: Optional[bool] = None,
) -> Iterator[Dict[str, Any]]:
    """流式兼容旧 PersonInfo 筛选，避免 N+1 查询和完整画像列表。"""
    search_text = search.strip().lower() if search else ""
    for profile in ProfileStore().iter_profiles():
        person = profile_to_person_dict(profile)
        if platform and person["platform"] != platform:
            continue
        if is_known is not None and person["is_known"] != is_known:
            continue
        if search_text and search_text not in _profile_search_text(person):
            continue
        yield person


def list_profile_person_dicts(
    search: Optional[str] = None,
    platform: Optional[str] = None,
    is_known: Optional[bool] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """列出画像；有 limit 时只读取足够的匹配项。"""
    from itertools import islice

    persons = _iter_profile_person_dicts(search, platform, is_known)
    return list(persons if limit is None else islice(persons, max(0, limit)))


def _profile_person_page(
    search: Optional[str], platform: Optional[str], is_known: Optional[bool], page: int, page_size: int
) -> tuple[int, List[Dict[str, Any]]]:
    start = (page - 1) * page_size
    page_data = []
    total = 0
    for person in _iter_profile_person_dicts(search, platform, is_known):
        if start <= total < start + page_size:
            page_data.append(person)
        total += 1
    return total, page_data


def get_profile_person_dict(person_id: str) -> Optional[Dict[str, Any]]:
    """获取单个新记忆画像的旧 PersonInfo 字典。"""
    profile = ProfileStore().get_profile(person_id)
    if profile is None:
        return None
    return profile_to_person_dict(profile)


def get_profile_person_stats() -> Dict[str, Any]:
    """统计新记忆系统画像数量。"""
    platform_counts = Counter()
    known = total = 0
    for person in _iter_profile_person_dicts():
        platform_counts[person["platform"]] += 1
        known += bool(person["is_known"])
        total += 1
    return {
        "total": total,
        "known": known,
        "unknown": total - known,
        "platforms": dict(platform_counts),
    }


@router.get("/list", response_model=PersonListResponse)
async def get_person_list(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    search: Optional[str] = Query(None, description="搜索关键词"),
    is_known: Optional[bool] = Query(None, description="是否已认识筛选"),
    platform: Optional[str] = Query(None, description="平台筛选"),
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
):
    """获取人物信息列表（来自新记忆系统用户画像）"""
    try:
        verify_auth_token(maibot_session, authorization)
        total, persons = await run_in_threadpool(_profile_person_page, search, platform, is_known, page, page_size)
        page_data = [PersonInfoResponse(**person) for person in persons]
        return PersonListResponse(success=True, total=total, page=page, page_size=page_size, data=page_data)
    except HTTPException:
        raise
    except Exception as e:
        raise internal_server_error(logger, "获取人物列表失败", e) from None


@router.get("/stats/summary")
async def get_person_stats(maibot_session: Optional[str] = Cookie(None), authorization: Optional[str] = Header(None)):
    """获取人物信息统计数据（来自新记忆系统用户画像）"""
    try:
        verify_auth_token(maibot_session, authorization)
        return {"success": True, "data": await run_in_threadpool(get_profile_person_stats)}
    except HTTPException:
        raise
    except Exception as e:
        raise internal_server_error(logger, "获取统计数据失败", e) from None


@router.post("/batch/delete", response_model=BatchDeleteResponse)
async def batch_delete_persons(
    request: BatchDeleteRequest,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
):
    """批量删除人物画像。"""
    try:
        verify_auth_token(maibot_session, authorization)
        profile_store = ProfileStore()
        failed_ids: List[str] = []
        deleted_count = 0

        for person_id in request.person_ids:
            if not profile_store.profile_exists(person_id):
                failed_ids.append(person_id)
                continue
            profile_store.delete_profile(person_id)
            deleted_count += 1

        return BatchDeleteResponse(
            success=True,
            message=f"已删除 {deleted_count} 个用户画像",
            deleted_count=deleted_count,
            failed_count=len(failed_ids),
            failed_ids=failed_ids,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise internal_server_error(logger, "批量删除人物信息失败", e, detail="批量删除失败") from None


@router.get("/{person_id}", response_model=PersonDetailResponse)
async def get_person_detail(
    person_id: str, maibot_session: Optional[str] = Cookie(None), authorization: Optional[str] = Header(None)
):
    """获取人物详细信息（来自新记忆系统用户画像）"""
    try:
        verify_auth_token(maibot_session, authorization)
        profile = ProfileStore().get_profile(person_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="用户画像不存在")
        return PersonDetailResponse(success=True, data=PersonInfoResponse(**profile_to_person_dict(profile)))
    except HTTPException:
        raise
    except Exception as e:
        raise internal_server_error(logger, "获取人物详情失败", e) from None


@router.patch("/{person_id}", response_model=PersonUpdateResponse)
async def update_person(
    person_id: str,
    request: PersonUpdateRequest,
    maibot_session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None),
):
    """更新人物画像的 WebUI 兼容字段。"""
    try:
        verify_auth_token(maibot_session, authorization)
        profile_store = ProfileStore()
        profile = profile_store.get_profile(person_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="用户画像不存在")

        if request.person_name is not None:
            value = request.person_name.strip()
            meta = _profile_meta(profile)
            if value:
                meta["person_name"] = value
            else:
                meta.pop("person_name", None)
            profile.facts.pop("person_name", None)
            profile.stats[_WEBUI_META_KEY] = meta
        if request.name_reason is not None:
            value = request.name_reason.strip()
            meta = _profile_meta(profile)
            if value:
                meta["name_reason"] = value
            else:
                meta.pop("name_reason", None)
            profile.facts.pop("name_reason", None)
            profile.stats[_WEBUI_META_KEY] = meta
        if request.nickname is not None:
            value = request.nickname.strip()
            meta = _profile_meta(profile)
            if value:
                meta["nickname"] = value
            else:
                meta.pop("nickname", None)
            profile.facts.pop("nickname", None)
            profile.stats[_WEBUI_META_KEY] = meta
        if request.memory_points is not None:
            value = request.memory_points.strip()
            meta = _profile_meta(profile)
            if value:
                meta["memory_points"] = value
            else:
                meta.pop("memory_points", None)
            profile.stats[_WEBUI_META_KEY] = meta
        if request.is_known is not None:
            meta = _profile_meta(profile)
            meta["is_known"] = request.is_known
            profile.stats[_WEBUI_META_KEY] = meta

        profile_store.save_profile(profile)
        updated = profile_store.get_profile(person_id) or profile
        return PersonUpdateResponse(
            success=True,
            message="用户画像已更新",
            data=PersonInfoResponse(**profile_to_person_dict(updated)),
        )
    except HTTPException:
        raise
    except Exception as e:
        raise internal_server_error(logger, "更新人物信息失败", e) from None


@router.delete("/{person_id}", response_model=PersonDeleteResponse)
async def delete_person(
    person_id: str, maibot_session: Optional[str] = Cookie(None), authorization: Optional[str] = Header(None)
):
    """删除人物画像。"""
    try:
        verify_auth_token(maibot_session, authorization)
        profile_store = ProfileStore()
        if not profile_store.profile_exists(person_id):
            raise HTTPException(status_code=404, detail="用户画像不存在")
        profile_store.delete_profile(person_id)
        return PersonDeleteResponse(success=True, message="用户画像已删除")
    except HTTPException:
        raise
    except Exception as e:
        raise internal_server_error(logger, "删除人物信息失败", e) from None
