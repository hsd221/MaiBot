import datetime
import math
import time

from peewee import BooleanField, DateTimeField, DoubleField, FloatField, IntegerField, Model, TextField

from src.common.logger import get_logger
from .database import db

logger = get_logger("database_model")
# 请在此处定义您的数据库实例。
# 您需要取消注释并配置适合您的数据库的部分。
# 例如，对于 SQLite:
# db = SqliteDatabase('RiyaBot.db')
#
# 对于 PostgreSQL:
# db = PostgresqlDatabase('your_db_name', user='your_user', password='your_password',
#                         host='localhost', port=5432)
#
# 对于 MySQL:
# db = MySQLDatabase('your_db_name', user='your_user', password='your_password',
#                    host='localhost', port=3306)


# 定义一个基础模型是一个好习惯，所有其他模型都应继承自它。
# 这允许您在一个地方为所有模型指定数据库。


class BaseModel(Model):
    class Meta:
        # 将下面的 'db' 替换为您实际的数据库实例变量名。
        database = db  # 例如: database = my_actual_db_instance
        pass  # 在用户定义数据库实例之前，此处为占位符


class ChatStreams(BaseModel):
    """
    用于存储流式记录数据的模型，类似于提供的 MongoDB 结构。
    """

    # stream_id: "a544edeb1a9b73e3e1d77dff36e41264"
    # 假设 stream_id 是唯一的，并为其创建索引以提高查询性能。
    stream_id = TextField(unique=True, index=True)

    # create_time: 1746096761.4490178 (时间戳，精确到小数点后7位)
    # DoubleField 用于存储浮点数，适合此类时间戳。
    create_time = DoubleField()

    # group_info 字段:
    #   platform: "qq"
    #   group_id: "941657197"
    #   group_name: "测试"
    group_platform = TextField(null=True)  # 群聊信息可能不存在
    group_id = TextField(null=True)
    group_name = TextField(null=True)

    # last_active_time: 1746623771.4825106 (时间戳，精确到小数点后7位)
    last_active_time = DoubleField()

    # platform: "qq" (顶层平台字段)
    platform = TextField()

    # user_info 字段:
    #   platform: "qq"
    #   user_id: "1787882683"
    #   user_nickname: "墨梓柒(IceSakurary)"
    #   user_cardname: ""
    user_platform = TextField()
    user_id = TextField()
    user_nickname = TextField()
    # user_cardname 可能为空字符串或不存在，设置 null=True 更具灵活性。
    user_cardname = TextField(null=True)

    class Meta:
        # 如果 BaseModel.Meta.database 已设置，则此模型将继承该数据库配置。
        # 如果不使用带有数据库实例的 BaseModel，或者想覆盖它，
        # 请取消注释并在下面设置数据库实例：
        # database = db
        table_name = "chat_streams"  # 可选：明确指定数据库中的表名


class LLMUsage(BaseModel):
    """
    用于存储 API 使用日志数据的模型。
    """

    model_name = TextField(index=True)  # 添加索引
    model_assign_name = TextField(null=True)  # 添加索引
    model_api_provider = TextField(null=True)  # 添加索引
    user_id = TextField(index=True)  # 添加索引
    request_type = TextField(index=True)  # 添加索引
    endpoint = TextField()
    prompt_tokens = IntegerField()
    completion_tokens = IntegerField()
    total_tokens = IntegerField()
    cost = DoubleField()
    time_cost = DoubleField(null=True)
    status = TextField()
    timestamp = DateTimeField(index=True)  # 更改为 DateTimeField 并添加索引

    class Meta:
        # 如果 BaseModel.Meta.database 已设置，则此模型将继承该数据库配置。
        # database = db
        table_name = "llm_usage"


class LLMRequestTrace(BaseModel):
    """模型请求与响应的结构化追踪记录。"""

    request_type = TextField(index=True)
    operation = TextField(index=True)
    model_name = TextField(index=True)
    model_identifier = TextField()
    provider_name = TextField(index=True)
    attempt = IntegerField(default=1)
    status = TextField(index=True)
    started_at = DateTimeField(default=datetime.datetime.now, index=True)
    completed_at = DateTimeField(null=True)
    duration_ms = IntegerField(null=True)
    request_preview = TextField(default="")
    response_preview = TextField(default="")
    request_payload = TextField()
    response_payload = TextField(null=True)
    error_type = TextField(null=True)
    error_message = TextField(null=True)
    status_code = IntegerField(null=True)
    prompt_tokens = IntegerField(default=0)
    completion_tokens = IntegerField(default=0)
    total_tokens = IntegerField(default=0)

    class Meta:
        table_name = "llm_request_trace"


class LLMRequestTraceMedia(BaseModel):
    """模型请求追踪关联的本地媒体快照元数据。"""

    trace_id = IntegerField(index=True)
    media_id = TextField()
    kind = TextField()
    format = TextField()
    mime_type = TextField()
    size_bytes = IntegerField()
    file_name = TextField()

    class Meta:
        table_name = "llm_request_trace_media"
        indexes = ((("trace_id", "media_id"), True),)


class Emoji(BaseModel):
    """表情包"""

    full_path = TextField(unique=True, index=True)  # 文件的完整路径 (包括文件名)
    format = TextField()  # 图片格式
    emoji_hash = TextField(index=True)  # 表情包的哈希值
    description = TextField()  # 表情包的描述
    query_count = IntegerField(default=0)  # 查询次数（用于统计表情包被查询描述的次数）
    is_registered = BooleanField(default=False)  # 是否已注册
    is_banned = BooleanField(default=False)  # 是否被禁止注册
    # emotion: list[str]  # 表情包的情感标签 - 存储为文本，应用层处理序列化/反序列化
    emotion = TextField(null=True)
    record_time = FloatField()  # 记录时间（被创建的时间）
    register_time = FloatField(null=True)  # 注册时间（被注册为可用表情包的时间）
    usage_count = IntegerField(default=0)  # 使用次数（被使用的次数）
    last_used_time = FloatField(null=True)  # 上次使用时间

    class Meta:
        # database = db # 继承自 BaseModel
        table_name = "emoji"


class EmojiUsageScene(BaseModel):
    """从真人发送记录中归纳出的表情包使用场景。"""

    emoji_hash = TextField(index=True)
    scene = TextField()
    sample_count = IntegerField(default=1)
    created_at = FloatField()
    last_active_time = FloatField()

    class Meta:
        table_name = "emoji_usage_scene"


class EmojiUsageEvent(BaseModel):
    """单次真人表情出现的幂等学习记录。"""

    emoji_hash = TextField(index=True)
    message_row_id = IntegerField(index=True)
    message_id = TextField(index=True)
    occurrence_index = IntegerField(default=0)
    chat_id = TextField(index=True)
    user_id = TextField(null=True)
    scene_id = IntegerField(null=True, index=True)
    status = TextField(default="pending")
    created_at = FloatField()

    class Meta:
        table_name = "emoji_usage_event"
        indexes = ((("chat_id", "message_id", "occurrence_index", "emoji_hash"), True),)


class Messages(BaseModel):
    """
    用于存储消息数据的模型。
    """

    message_id = TextField(index=True)  # 消息 ID (更改自 IntegerField)
    time = DoubleField()  # 消息时间戳

    chat_id = TextField(index=True)  # 对应的 ChatStreams stream_id

    reply_to = TextField(null=True)

    interest_value = DoubleField(null=True)
    key_words = TextField(null=True)
    key_words_lite = TextField(null=True)

    is_mentioned = BooleanField(null=True)
    is_at = BooleanField(null=True)
    reply_probability_boost = DoubleField(null=True)
    # 从 chat_info 扁平化而来的字段
    chat_info_stream_id = TextField()
    chat_info_platform = TextField()
    chat_info_user_platform = TextField()
    chat_info_user_id = TextField()
    chat_info_user_nickname = TextField()
    chat_info_user_cardname = TextField(null=True)
    chat_info_group_platform = TextField(null=True)  # 群聊信息可能不存在
    chat_info_group_id = TextField(null=True)
    chat_info_group_name = TextField(null=True)
    chat_info_create_time = DoubleField()
    chat_info_last_active_time = DoubleField()

    # 从顶层 user_info 扁平化而来的字段 (消息发送者信息)
    user_platform = TextField(null=True)
    user_id = TextField(null=True)
    user_nickname = TextField(null=True)
    user_cardname = TextField(null=True)

    processed_plain_text = TextField(null=True)  # 处理后的纯文本消息
    display_message = TextField(null=True)  # 显示的消息

    priority_mode = TextField(null=True)
    priority_info = TextField(null=True)

    additional_config = TextField(null=True)
    is_emoji = BooleanField(default=False)
    is_picid = BooleanField(default=False)
    is_command = BooleanField(default=False)
    intercept_message_level = IntegerField(default=0)
    is_notify = BooleanField(default=False)
    is_recalled = BooleanField(default=False, index=True)  # 消息是否已被撤回

    selected_expressions = TextField(null=True)

    class Meta:
        # database = db # 继承自 BaseModel
        table_name = "messages"


class ActionRecords(BaseModel):
    """
    用于存储动作记录数据的模型。
    """

    action_id = TextField(index=True)  # 消息 ID (更改自 IntegerField)
    time = DoubleField()  # 消息时间戳

    action_reasoning = TextField(null=True)

    action_name = TextField()
    action_data = TextField()
    action_done = BooleanField(default=False)

    action_build_into_prompt = BooleanField(default=False)
    action_prompt_display = TextField()

    chat_id = TextField(index=True)  # 对应的 ChatStreams stream_id
    chat_info_stream_id = TextField()
    chat_info_platform = TextField()

    class Meta:
        # database = db # 继承自 BaseModel
        table_name = "action_records"


class Images(BaseModel):
    """
    用于存储图像信息的模型。
    """

    image_id = TextField(default="")  # 图片唯一ID
    emoji_hash = TextField(index=True)  # 图像的哈希值
    description = TextField(null=True)  # 图像的描述
    path = TextField(unique=True)  # 图像文件的路径
    # base64 = TextField()  # 图片的base64编码
    count = IntegerField(default=1)  # 图片被引用的次数
    timestamp = FloatField()  # 时间戳
    type = TextField()  # 图像类型，例如 "emoji"
    vlm_processed = BooleanField(default=False)  # 是否已经过VLM处理

    class Meta:
        table_name = "images"


class ImageDescriptions(BaseModel):
    """
    用于存储图像描述信息的模型。
    """

    type = TextField()  # 类型，例如 "emoji"
    image_description_hash = TextField(index=True)  # 图像的哈希值
    description = TextField()  # 图像的描述
    timestamp = FloatField()  # 时间戳

    class Meta:
        # database = db # 继承自 BaseModel
        table_name = "image_descriptions"


class EmojiDescriptionCache(BaseModel):
    """
    存储表情包的详细描述和情感标签缓存
    """

    emoji_hash = TextField(unique=True, index=True)
    description = TextField()  # 详细描述
    emotion_tags = TextField(null=True)  # 情感标签，逗号分隔
    timestamp = FloatField()

    class Meta:
        table_name = "emoji_description_cache"


class OnlineTime(BaseModel):
    """
    用于存储在线时长记录的模型。
    """

    # timestamp: "$date": "2025-05-01T18:52:18.191Z" (存储为字符串)
    timestamp = TextField(default=datetime.datetime.now)  # 时间戳
    duration = IntegerField()  # 时长，单位分钟
    start_timestamp = DateTimeField(default=datetime.datetime.now)
    end_timestamp = DateTimeField(index=True)

    class Meta:
        # database = db # 继承自 BaseModel
        table_name = "online_time"


class GroupInfo(BaseModel):
    """
    用于存储群组信息数据的模型。
    """

    group_id = TextField(unique=True, index=True)  # 群组唯一ID
    group_name = TextField(null=True)  # 群组名称 (允许为空)
    platform = TextField()  # 平台
    group_impression = TextField(null=True)  # 群组印象
    member_list = TextField(null=True)  # 群成员列表 (JSON格式)
    topic = TextField(null=True)  # 群组基本信息

    create_time = FloatField(null=True)  # 创建时间 (时间戳)
    last_active = FloatField(null=True)  # 最后活跃时间
    member_count = IntegerField(null=True, default=0)  # 成员数量

    class Meta:
        # database = db # 继承自 BaseModel
        table_name = "group_info"


class Expression(BaseModel):
    """
    用于存储表达风格的模型。
    """

    situation = TextField()
    style = TextField()
    content_list = TextField(null=True)
    count = IntegerField(default=1)
    last_active_time = FloatField()
    chat_id = TextField(index=True)
    create_date = FloatField(null=True)  # 创建日期，允许为空以兼容老数据
    checked = BooleanField(default=False)  # 是否已检查
    rejected = BooleanField(default=False)  # 是否被拒绝但未更新
    modified_by = TextField(null=True)  # 最后修改来源：'ai' 或 'user'，为空表示未检查

    class Meta:
        table_name = "expression"


class Jargon(BaseModel):
    """
    用于存储俚语的模型
    """

    content = TextField()
    raw_content = TextField(null=True)
    meaning = TextField(null=True)
    chat_id = TextField(index=True)
    is_global = BooleanField(default=False)
    count = IntegerField(default=0)
    is_jargon = BooleanField(null=True)  # None表示未判定，True表示是黑话，False表示不是黑话
    last_inference_count = IntegerField(null=True)  # 最后一次判定的count值，用于避免重启后重复判定
    is_complete = BooleanField(default=False)  # 是否已完成所有推断（count>=100后不再推断）
    inference_with_context = TextField(null=True)  # 基于上下文的推断结果（JSON格式）
    inference_content_only = TextField(null=True)  # 仅基于词条的推断结果（JSON格式）

    class Meta:
        table_name = "jargon"


class BehaviorPattern(BaseModel):
    """
    用于存储行为学习到的可复用场景-行为-结果模式。
    """

    chat_id = TextField(index=True)
    actor_type = TextField(index=True)
    learning_type = TextField(index=True)
    action = TextField()
    outcome = TextField()
    source_text = TextField(null=True)
    source_ids = TextField(null=True)
    count = IntegerField(default=1)
    score = FloatField(default=1.0)
    enabled = BooleanField(default=True)
    selected_count = IntegerField(default=0)
    last_selected_time = FloatField(null=True)
    last_active_time = FloatField()
    create_date = FloatField(null=True)

    class Meta:
        table_name = "behavior_pattern"


class ChatHistory(BaseModel):
    """
    用于存储聊天历史概括的模型
    """

    chat_id = TextField(index=True)  # 聊天ID
    start_time = DoubleField()  # 起始时间
    end_time = DoubleField()  # 结束时间
    original_text = TextField()  # 对话原文
    participants = TextField()  # 参与的所有人的昵称，JSON格式存储
    theme = TextField()  # 主题：这段对话的主要内容，一个简短的标题
    keywords = TextField()  # 关键词：这段对话的关键词，JSON格式存储
    summary = TextField()  # 概括：对这段话的平文本概括
    key_point = TextField(null=True)  # 关键信息：话题中的关键信息点，JSON格式存储
    count = IntegerField(default=0)  # 被检索次数
    forget_times = IntegerField(default=0)  # 被遗忘检查的次数

    class Meta:
        table_name = "chat_history"


class ChatHistoryImportTask(BaseModel):
    """可审计的聊天记录导入与学习任务。"""

    import_id = TextField(unique=True, index=True)
    source_hash = TextField(index=True)
    source_name = TextField()
    source_size = IntegerField(default=0)
    status = TextField(index=True)
    chat_id = TextField(null=True, index=True)
    chat_name = TextField(null=True)
    group_id = TextField(null=True)
    source_path = TextField(default="")
    normalized_path = TextField(default="")
    analysis_json = TextField(default="{}")
    options_json = TextField(default="{}")
    result_json = TextField(null=True)
    error_message = TextField(null=True)
    progress_stage = TextField(default="analyzing")
    progress_current = IntegerField(default=0)
    progress_total = IntegerField(default=1)
    resume_stage = TextField(null=True)
    checkpoint_window_count = IntegerField(default=0)
    attempt_count = IntegerField(default=0)
    core_store_completed = BooleanField(default=False)
    enrichment_store_completed = BooleanField(default=False)
    cancel_requested = BooleanField(default=False)
    created_at = FloatField()
    updated_at = FloatField()
    started_at = FloatField(null=True)
    completed_at = FloatField(null=True)

    class Meta:
        table_name = "chat_history_import_task"


class PluginInstallation(BaseModel):
    """插件安装来源与当前已安装版本。"""

    plugin_id = TextField(primary_key=True)
    install_method = TextField()
    registry_url = TextField(null=True)
    repository_url = TextField(null=True)
    source_ref = TextField(null=True)
    source_commit = TextField(null=True)
    artifact_sha256 = TextField(null=True)
    installed_version = TextField()
    installed_at = DateTimeField()
    updated_at = DateTimeField()
    last_checked_at = DateTimeField(null=True)

    class Meta:
        table_name = "plugin_installation"


MODELS = [
    ChatStreams,
    LLMUsage,
    LLMRequestTrace,
    LLMRequestTraceMedia,
    Emoji,
    EmojiUsageScene,
    EmojiUsageEvent,
    Messages,
    Images,
    ImageDescriptions,
    EmojiDescriptionCache,
    OnlineTime,
    Expression,
    ActionRecords,
    Jargon,
    BehaviorPattern,
    ChatHistory,
    ChatHistoryImportTask,
    PluginInstallation,
]


def _quote_sqlite_identifier(identifier: str) -> str:
    """安全引用 SQLite 表名或列名。"""
    if not isinstance(identifier, str):
        raise TypeError("SQLite 标识符必须是字符串")
    if not identifier or "\x00" in identifier:
        raise ValueError("SQLite 标识符不能为空或包含空字节")
    escaped_identifier = identifier.replace('"', '""')
    return f'"{escaped_identifier}"'


def _sqlite_literal(value) -> str:
    """将受信任的模型默认值编码为单个 SQLite 字面量。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("SQLite 默认浮点值必须是有限数")
        return repr(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        value = str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, bytes):
        return f"X'{value.hex()}'"
    raise TypeError(f"不支持的 SQLite 默认值类型: {type(value).__name__}")


def _field_column_name(field_name: str, field_obj) -> str:
    column_name = getattr(field_obj, "column_name", None)
    return column_name if isinstance(column_name, str) and column_name else field_name


def _table_info_sql(table_name: str) -> str:
    return f"PRAGMA table_info({_quote_sqlite_identifier(table_name)})"


def create_tables():
    """
    创建所有在模型中定义的数据库表。
    """
    with db:
        db.create_tables(MODELS)


def initialize_database(sync_constraints=False, drop_extra_fields=False):
    """
    检查所有定义的表是否存在，如果不存在则创建它们。
    检查所有表的所有字段是否存在，如果缺失则自动添加。

    Args:
        sync_constraints (bool): 是否同步字段约束。默认为 False。
                               如果为 True，会检查并修复字段的 NULL 约束不一致问题。
        drop_extra_fields (bool): 是否删除模型未声明的现有字段。默认为 False，
                                  防止启动时静默丢失旧版本或手工扩展的数据。
    """

    try:
        with db:  # 管理 table_exists 检查的连接
            for model in MODELS:
                table_name = model._meta.table_name
                if not db.table_exists(model):
                    logger.warning(f"表 '{table_name}' 未找到，正在创建...")
                    db.create_tables([model])
                    logger.info(f"表 '{table_name}' 创建成功")
                    continue

                # 检查字段
                cursor = db.execute_sql(_table_info_sql(table_name))
                existing_columns = {row[1] for row in cursor.fetchall()}
                model_columns = {
                    _field_column_name(field_name, field_obj) for field_name, field_obj in model._meta.fields.items()
                }

                if missing_fields := model_columns - existing_columns:
                    logger.warning(f"表 '{table_name}' 缺失字段: {missing_fields}")

                for field_name, field_obj in model._meta.fields.items():
                    column_name = _field_column_name(field_name, field_obj)
                    if column_name not in existing_columns:
                        logger.info(f"表 '{table_name}' 缺失字段 '{column_name}'，正在添加...")
                        field_type = field_obj.__class__.__name__
                        sql_type = {
                            "TextField": "TEXT",
                            "IntegerField": "INTEGER",
                            "FloatField": "FLOAT",
                            "DoubleField": "DOUBLE",
                            "BooleanField": "INTEGER",
                            "DateTimeField": "DATETIME",
                        }.get(field_type, "TEXT")
                        alter_sql = (
                            f"ALTER TABLE {_quote_sqlite_identifier(table_name)} "
                            f"ADD COLUMN {_quote_sqlite_identifier(column_name)} {sql_type}"
                        )
                        alter_sql += " NULL" if field_obj.null else " NOT NULL"
                        if hasattr(field_obj, "default") and field_obj.default is not None:
                            # 正确处理不同类型的默认值，跳过lambda函数
                            default_value = field_obj.default
                            if callable(default_value):
                                # 跳过lambda函数或其他可调用对象，这些无法在SQL中表示
                                pass
                            else:
                                try:
                                    alter_sql += f" DEFAULT {_sqlite_literal(default_value)}"
                                except (TypeError, ValueError):
                                    logger.warning(
                                        f"字段 '{column_name}' 的数据库默认值类型不受支持，已跳过默认值",
                                        exc_info=True,
                                    )
                        try:
                            db.execute_sql(alter_sql)
                            logger.info(f"字段 '{column_name}' 添加成功")
                        except Exception as e:
                            logger.error(f"添加字段 '{column_name}' 失败: {e}")

                # 检查并删除多余字段（新增逻辑）
                extra_fields = existing_columns - model_columns
                if extra_fields:
                    logger.warning(f"表 '{table_name}' 存在多余字段: {extra_fields}")
                for field_name in extra_fields if drop_extra_fields else ():
                    try:
                        logger.warning(f"表 '{table_name}' 存在多余字段 '{field_name}'，正在尝试删除...")
                        db.execute_sql(
                            f"ALTER TABLE {_quote_sqlite_identifier(table_name)} "
                            f"DROP COLUMN {_quote_sqlite_identifier(field_name)}"
                        )
                        logger.info(f"字段 '{field_name}' 删除成功")
                    except Exception as e:
                        logger.error(f"删除字段 '{field_name}' 失败: {e}")

        # 如果启用了约束同步，执行约束检查和修复
        if sync_constraints:
            logger.debug("开始同步数据库字段约束...")
            sync_field_constraints()
            logger.debug("数据库字段约束同步完成")

    except Exception as e:
        logger.exception(f"检查表或字段是否存在时出错: {e}")
        # 如果检查失败（例如数据库不可用），则退出
        return

    logger.info("数据库初始化完成")


def sync_field_constraints():
    """
    同步数据库字段约束，确保现有数据库字段的 NULL 约束与模型定义一致。
    如果发现不一致，会自动修复字段约束。
    """

    try:
        with db:
            for model in MODELS:
                table_name = model._meta.table_name
                if not db.table_exists(model):
                    logger.warning(f"表 '{table_name}' 不存在，跳过约束检查")
                    continue

                logger.debug(f"检查表 '{table_name}' 的字段约束...")

                # 获取当前表结构信息
                cursor = db.execute_sql(_table_info_sql(table_name))
                current_schema = {
                    row[1]: {"type": row[2], "notnull": bool(row[3]), "default": row[4]} for row in cursor.fetchall()
                }

                # 检查每个模型字段的约束
                constraints_to_fix = []
                for field_name, field_obj in model._meta.fields.items():
                    column_name = _field_column_name(field_name, field_obj)
                    if column_name not in current_schema:
                        continue  # 字段不存在，跳过

                    current_notnull = current_schema[column_name]["notnull"]
                    model_allows_null = field_obj.null

                    # 如果模型允许 null 但数据库字段不允许 null，需要修复
                    if model_allows_null and current_notnull:
                        constraints_to_fix.append(
                            {
                                "field_name": field_name,
                                "column_name": column_name,
                                "field_obj": field_obj,
                                "action": "allow_null",
                                "current_constraint": "NOT NULL",
                                "target_constraint": "NULL",
                            }
                        )
                        logger.warning(f"字段 '{field_name}' 约束不一致: 模型允许NULL，但数据库为NOT NULL")

                    # 如果模型不允许 null 但数据库字段允许 null，也需要修复（但要小心）
                    elif not model_allows_null and not current_notnull:
                        constraints_to_fix.append(
                            {
                                "field_name": field_name,
                                "column_name": column_name,
                                "field_obj": field_obj,
                                "action": "disallow_null",
                                "current_constraint": "NULL",
                                "target_constraint": "NOT NULL",
                            }
                        )
                        logger.warning(f"字段 '{field_name}' 约束不一致: 模型不允许NULL，但数据库允许NULL")

                # 修复约束不一致的字段
                if constraints_to_fix:
                    logger.info(f"表 '{table_name}' 需要修复 {len(constraints_to_fix)} 个字段约束")
                    _fix_table_constraints(table_name, model, constraints_to_fix)
                else:
                    logger.debug(f"表 '{table_name}' 的字段约束已同步")

    except Exception as e:
        logger.exception(f"同步字段约束时出错: {e}")


def _constraint_null_default(field_obj) -> str:
    if isinstance(field_obj, TextField):
        return _sqlite_literal("")
    if isinstance(field_obj, (IntegerField, FloatField, DoubleField)):
        return _sqlite_literal(0)
    if isinstance(field_obj, BooleanField):
        return _sqlite_literal(False)
    if isinstance(field_obj, DateTimeField):
        return _sqlite_literal(datetime.datetime.now())
    return _sqlite_literal("")


def _rebuild_table_constraints(table_name, model, constraints_to_fix, backup_table) -> None:
    quoted_table = _quote_sqlite_identifier(table_name)
    quoted_backup_table = _quote_sqlite_identifier(backup_table)

    db.execute_sql(f"CREATE TABLE {quoted_backup_table} AS SELECT * FROM {quoted_table}")
    logger.info(f"已创建备份表 '{backup_table}'")

    original_count = db.execute_sql(f"SELECT COUNT(*) FROM {quoted_backup_table}").fetchone()[0]
    logger.info(f"备份表 '{backup_table}' 包含 {original_count} 行数据")

    db.execute_sql(f"DROP TABLE {quoted_table}")
    logger.info(f"已删除原表 '{table_name}'")

    db.create_tables([model])
    logger.info(f"已重新创建表 '{table_name}' 使用新的约束")

    fields = [
        (field_name, field_obj, _field_column_name(field_name, field_obj))
        for field_name, field_obj in model._meta.fields.items()
    ]
    fields_str = ", ".join(_quote_sqlite_identifier(column_name) for _, _, column_name in fields)
    null_to_notnull_fields = {
        constraint["field_name"] for constraint in constraints_to_fix if constraint["action"] == "disallow_null"
    }
    null_to_notnull_columns = {
        constraint.get("column_name")
        for constraint in constraints_to_fix
        if constraint["action"] == "disallow_null" and constraint.get("column_name")
    }

    if null_to_notnull_fields:
        logger.warning(f"字段 {null_to_notnull_fields} 将从允许NULL改为不允许NULL，需要处理现有的NULL值")
        select_fields = []
        for field_name, field_obj, column_name in fields:
            quoted_column = _quote_sqlite_identifier(column_name)
            if field_name in null_to_notnull_fields or column_name in null_to_notnull_columns:
                default_value = _constraint_null_default(field_obj)
                select_fields.append(f"COALESCE({quoted_column}, {default_value}) AS {quoted_column}")
            else:
                select_fields.append(quoted_column)
        select_str = ", ".join(select_fields)
    else:
        select_str = fields_str

    db.execute_sql(f"INSERT INTO {quoted_table} ({fields_str}) SELECT {select_str} FROM {quoted_backup_table}")
    logger.info(f"已从备份表恢复数据到 '{table_name}'")

    new_count = db.execute_sql(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0]
    if original_count != new_count:
        raise RuntimeError(f"数据完整性验证失败: 原始 {original_count} 行，新表 {new_count} 行")

    logger.info(f"数据完整性验证通过: {original_count} 行数据")
    db.execute_sql(f"DROP TABLE {quoted_backup_table}")
    logger.info(f"已删除备份表 '{backup_table}'")

    for constraint in constraints_to_fix:
        logger.info(
            f"已修复字段 '{constraint['field_name']}': "
            f"{constraint['current_constraint']} -> {constraint['target_constraint']}"
        )


def _fix_table_constraints(table_name, model, constraints_to_fix):
    """在事务中重建 SQLite 表并同步字段约束。"""
    backup_table = f"{table_name}_backup_{time.time_ns()}"
    logger.info(f"开始修复表 '{table_name}' 的字段约束...")

    try:
        atomic = getattr(db, "atomic", None)
        if callable(atomic):
            with atomic():
                _rebuild_table_constraints(table_name, model, constraints_to_fix, backup_table)
        else:
            _rebuild_table_constraints(table_name, model, constraints_to_fix, backup_table)
    except Exception as e:
        logger.exception(f"修复表 '{table_name}' 约束时出错: {e}")
        try:
            if db.table_exists(backup_table):
                logger.info(f"尝试从备份表 '{backup_table}' 恢复...")
                db.execute_sql(f"DROP TABLE IF EXISTS {_quote_sqlite_identifier(table_name)}")
                db.execute_sql(
                    f"ALTER TABLE {_quote_sqlite_identifier(backup_table)} "
                    f"RENAME TO {_quote_sqlite_identifier(table_name)}"
                )
                logger.info(f"已从备份恢复表 '{table_name}'")
        except Exception as restore_error:
            logger.exception(f"恢复表失败: {restore_error}")


def check_field_constraints():
    """
    检查但不修复字段约束，返回不一致的字段信息。
    用于在修复前预览需要修复的内容。
    """

    inconsistencies = {}

    try:
        with db:
            for model in MODELS:
                table_name = model._meta.table_name
                if not db.table_exists(model):
                    continue

                # 获取当前表结构信息
                cursor = db.execute_sql(_table_info_sql(table_name))
                current_schema = {
                    row[1]: {"type": row[2], "notnull": bool(row[3]), "default": row[4]} for row in cursor.fetchall()
                }

                table_inconsistencies = []

                # 检查每个模型字段的约束
                for field_name, field_obj in model._meta.fields.items():
                    column_name = _field_column_name(field_name, field_obj)
                    if column_name not in current_schema:
                        continue

                    current_notnull = current_schema[column_name]["notnull"]
                    model_allows_null = field_obj.null

                    if model_allows_null and current_notnull:
                        table_inconsistencies.append(
                            {
                                "field_name": field_name,
                                "issue": "model_allows_null_but_db_not_null",
                                "model_constraint": "NULL",
                                "db_constraint": "NOT NULL",
                                "recommended_action": "allow_null",
                            }
                        )
                    elif not model_allows_null and not current_notnull:
                        table_inconsistencies.append(
                            {
                                "field_name": field_name,
                                "issue": "model_not_null_but_db_allows_null",
                                "model_constraint": "NOT NULL",
                                "db_constraint": "NULL",
                                "recommended_action": "disallow_null",
                            }
                        )

                if table_inconsistencies:
                    inconsistencies[table_name] = table_inconsistencies

    except Exception as e:
        logger.exception(f"检查字段约束时出错: {e}")

    return inconsistencies


def fix_image_id():
    """
    修复表情包的 image_id 字段
    """
    import uuid

    try:
        with db:
            for img in Images.select():
                if not img.image_id:
                    img.image_id = str(uuid.uuid4())
                    img.save()
                    logger.info(f"已为表情包 {img.id} 生成新的 image_id: {img.image_id}")
    except Exception as e:
        logger.exception(f"修复 image_id 时出错: {e}")


# 模块加载时调用初始化函数
initialize_database(sync_constraints=True)
fix_image_id()
