from typing import List, Any, Optional
from peewee import Model  # 添加 Peewee Model 导入

from src.config.config import global_config
from src.common.data_models.database_data_model import DatabaseMessages
from src.common.database.database_model import Messages
from src.common.logger import get_logger

logger = get_logger(__name__)
DEFAULT_MESSAGE_LIMIT = 1000


def _model_to_instance(model_instance: Model) -> DatabaseMessages:
    """
    将 Peewee 模型实例转换为字典。
    """
    return DatabaseMessages(**model_instance.__data__)


def find_messages(
    message_filter: dict[str, Any],
    sort: Optional[List[tuple[str, int]]] = None,
    limit: int = DEFAULT_MESSAGE_LIMIT,
    limit_mode: str = "latest",
    filter_bot=False,
    filter_command=False,
    filter_intercept_message_level: Optional[int] = None,
) -> List[DatabaseMessages]:
    """
    根据提供的过滤器、排序和限制条件查找消息。

    Args:
        message_filter: 查询过滤器字典，键为模型字段名，值为期望值或包含操作符的字典 (例如 {'$gt': value}).
        sort: 排序条件列表，例如 [('time', 1)] (1 for asc, -1 for desc)。仅在 limit 为 0 时生效。
        limit: 返回的最大文档数，默认 1000；显式传入 0 表示不限制。
        limit_mode: 当 limit > 0 时生效。 'earliest' 表示获取最早的记录， 'latest' 表示获取最新的记录（结果仍按时间正序排列）。默认为 'latest'。

    Returns:
        消息字典列表，如果出错则返回空列表。
    """
    try:
        query = Messages.select()

        # 应用过滤器
        if message_filter:
            conditions = []
            for key, value in message_filter.items():
                if hasattr(Messages, key):
                    field = getattr(Messages, key)
                    if isinstance(value, dict):
                        # 处理 MongoDB 风格的操作符
                        for op, op_value in value.items():
                            if op == "$gt":
                                conditions.append(field > op_value)
                            elif op == "$lt":
                                conditions.append(field < op_value)
                            elif op == "$gte":
                                conditions.append(field >= op_value)
                            elif op == "$lte":
                                conditions.append(field <= op_value)
                            elif op == "$ne":
                                conditions.append(field != op_value)
                            elif op == "$in":
                                conditions.append(field.in_(op_value))
                            elif op == "$nin":
                                conditions.append(field.not_in(op_value))
                            else:
                                logger.warning(
                                    "消息查询过滤器包含未知操作符，已跳过",
                                    event_code="message_repository.filter.unknown_operator",
                                    field=key,
                                    operator=op,
                                )
                    else:
                        # 直接相等比较
                        conditions.append(field == value)
                else:
                    logger.warning(
                        "消息查询过滤器字段不存在，已跳过",
                        event_code="message_repository.filter.unknown_field",
                        field=key,
                    )
            if conditions:
                query = query.where(*conditions)

        # 排除 id 为 "notice" 的消息
        query = query.where(Messages.message_id != "notice")

        # 排除已撤回的消息
        query = query.where(Messages.is_recalled == False)  # noqa: E712

        if filter_bot:
            query = query.where(Messages.user_id != global_config.bot.qq_account)

        if filter_command:
            # 使用按位取反构造 Peewee 的 NOT 条件，避免直接与 False 比较
            query = query.where(~Messages.is_command)

        if filter_intercept_message_level is not None:
            # 过滤掉所有 intercept_message_level > filter_intercept_message_level 的消息
            query = query.where(Messages.intercept_message_level <= filter_intercept_message_level)

        if limit > 0:
            if limit_mode == "earliest":
                # 获取时间最早的 limit 条记录，已经是正序
                query = query.order_by(Messages.time.asc()).limit(limit)
                peewee_results = list(query)
            else:  # 默认为 'latest'
                # 获取时间最晚的 limit 条记录
                query = query.order_by(Messages.time.desc()).limit(limit)
                latest_results_peewee = list(query)
                # 将结果按时间正序排列
                peewee_results = sorted(latest_results_peewee, key=lambda msg: msg.time)
        else:
            # limit 为 0 时，应用传入的 sort 参数
            if sort:
                peewee_sort_terms = []
                for field_name, direction in sort:
                    if hasattr(Messages, field_name):
                        field = getattr(Messages, field_name)
                        if direction == 1:  # ASC
                            peewee_sort_terms.append(field.asc())
                        elif direction == -1:  # DESC
                            peewee_sort_terms.append(field.desc())
                        else:
                            logger.warning(
                                "消息查询排序方向无效，已跳过",
                                event_code="message_repository.sort.invalid_direction",
                                field=field_name,
                                direction=direction,
                            )
                    else:
                        logger.warning(
                            "消息查询排序字段不存在，已跳过",
                            event_code="message_repository.sort.unknown_field",
                            field=field_name,
                        )
                if peewee_sort_terms:
                    query = query.order_by(*peewee_sort_terms)
            peewee_results = list(query)

        return [_model_to_instance(msg) for msg in peewee_results]
    except Exception:
        logger.exception(
            "Peewee 消息查询失败",
            event_code="message_repository.find_failed",
            message_filter=message_filter,
            sort=sort,
            limit=limit,
            limit_mode=limit_mode,
        )
        return []


def count_messages(message_filter: dict[str, Any]) -> int:
    """
    根据提供的过滤器计算消息数量。

    Args:
        message_filter: 查询过滤器字典，键为模型字段名，值为期望值或包含操作符的字典 (例如 {'$gt': value}).

    Returns:
        符合条件的消息数量，如果出错则返回 0。
    """
    try:
        query = Messages.select()

        # 应用过滤器
        if message_filter:
            conditions = []
            for key, value in message_filter.items():
                if hasattr(Messages, key):
                    field = getattr(Messages, key)
                    if isinstance(value, dict):
                        # 处理 MongoDB 风格的操作符
                        for op, op_value in value.items():
                            if op == "$gt":
                                conditions.append(field > op_value)
                            elif op == "$lt":
                                conditions.append(field < op_value)
                            elif op == "$gte":
                                conditions.append(field >= op_value)
                            elif op == "$lte":
                                conditions.append(field <= op_value)
                            elif op == "$ne":
                                conditions.append(field != op_value)
                            elif op == "$in":
                                conditions.append(field.in_(op_value))
                            elif op == "$nin":
                                conditions.append(field.not_in(op_value))
                            else:
                                logger.warning(
                                    "消息计数过滤器包含未知操作符，已跳过",
                                    event_code="message_repository.count_filter.unknown_operator",
                                    field=key,
                                    operator=op,
                                )
                    else:
                        # 直接相等比较
                        conditions.append(field == value)
                else:
                    logger.warning(
                        "消息计数过滤器字段不存在，已跳过",
                        event_code="message_repository.count_filter.unknown_field",
                        field=key,
                    )
            if conditions:
                query = query.where(*conditions)

        # 排除 id 为 "notice" 的消息
        query = query.where(Messages.message_id != "notice")

        # 排除已撤回的消息，保持与 find_messages 的口径一致
        query = query.where(Messages.is_recalled == False)  # noqa: E712

        count = query.count()
        return count
    except Exception:
        logger.exception(
            "Peewee 消息计数失败",
            event_code="message_repository.count_failed",
            message_filter=message_filter,
        )
        return 0


# 你可以在这里添加更多与 messages 集合相关的数据库操作函数，例如 find_one_message, insert_message 等。
# 注意：对于 Peewee，插入操作通常是 Messages.create(...) 或 instance.save()。
# 查找单个消息可以是 Messages.get_or_none(...) 或 query.first()。
