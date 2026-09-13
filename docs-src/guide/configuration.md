# 配置指南

RiyaBot 的配置分为两个文件，均由程序在首次启动时自动生成于 `config/` 目录：

- `bot_config.toml` — bot 身份、人格、聊天行为、记忆、表达学习等；
- `model_config.toml` — 模型供应商、模型定义与任务分配。

> **不必手动编辑配置文件。** 绝大多数配置项都能在 WebUI 管理面板中可视化修改并即时保存，首次配置也由 WebUI 的配置向导引导完成。本文用于说明各配置项的含义，方便你在面板中理解每个字段；需要时也可以直接编辑 TOML。

配置类定义在 `src/config/official_configs.py`（bot 配置）和 `src/config/api_ada_configs.py`（模型配置）。以下按配置段（TOML 中的 `[section]`）说明常用字段，字段默认值以源码为准。

## bot_config.toml

### `[bot]` 身份

| 字段 | 类型 | 说明 |
|---|---|---|
| `platform` | str | 平台标识 |
| `qq_account` | str | QQ 账号 |
| `nickname` | str | 昵称 |
| `platforms` | list | 其他平台列表 |
| `alias_names` | list | 别名列表，默认 `["Riya", "小璃"]` |

### `[personality]` 人格

| 字段 | 类型 | 说明 |
|---|---|---|
| `personality` | str | 人格描述 |
| `reply_style` | str | 默认表达风格 |

### `[chat]` 聊天行为

常用字段：

| 字段 | 默认 | 说明 |
|---|---|---|
| `group_message_buffer_seconds` | 3.0 | 群聊首条消息进入 TurnGate 后的固定等待时长，0 关闭 |
| `private_message_buffer_seconds` | 1.5 | 私聊首条消息的等待时长 |
| `max_context_size` | 30 | 上下文长度 |
| `mentioned_bot_reply` | true | 是否启用提及必回复 |
| `at_bot_inevitable_reply` | 1 | @bot 必然回复，1 为 100%，0 为不额外增幅 |
| `planner_smooth` | 3 | 规划器平滑，增大可减小 planner 负荷、略降反应速度，推荐 2-5，0 关闭 |
| `talk_value` | 1 | 思考频率 |
| `enable_talk_value_rules` | true | 是否启用动态发言频率规则 |
| `talk_value_rules` | list | 按聊天流/时段配置的发言频率规则 |
| `llm_quote` | false | 是否允许 LLM 在 reply 中控制引用 |

`talk_value_rules` 规则格式为 `{ target="platform:id:type" 或 "", time="HH:MM-HH:MM", value=0.5 }`。匹配时先匹配指定聊天流规则，再匹配全局规则（`target=""`），时间区间支持跨夜（如 `23:00-02:00`）。

### `[message_receive]` 消息过滤

| 字段 | 说明 |
|---|---|
| `ban_words` | 过滤词集合 |
| `ban_msgs_regex` | 过滤正则集合 |

### `[memory]` 记忆

| 字段 | 默认 | 说明 |
|---|---|---|
| `max_agent_iterations` | 5 | 记忆 Agent 最多迭代轮数（最低 1） |
| `agent_timeout_seconds` | 180.0 | Agent 超时时间（秒） |
| `global_memory` | false | 是否允许记忆检索跨聊天流全局查询 |
| `global_memory_blacklist` | [] | 全局记忆黑名单，格式 `["platform:id:type", ...]` |
| `planner_question` | true | 是否用 Planner 的 question 作为记忆检索问题 |
| `sqlite_path` | data/memory.db | 记忆 SQLite 路径 |
| `qdrant_url` | "" | Qdrant 服务器 URL，为空时用本地嵌入模式 |
| `qdrant_local_path` | data/qdrant | Qdrant 本地模式数据目录 |
| `embedding_dimension` | 1024 | 嵌入向量维度，须与 embedding 模型输出维度一致 |

### `[expression]` 表达学习

| 字段 | 默认 | 说明 |
|---|---|---|
| `learning_list` | 见源码 | 按聊天流配置：`[chat_id, 使用表达, 学习表达, jargon学习]` |
| `expression_groups` | 见源码 | 表达学习互通组 |
| `vector_selection_enabled` | true | 是否用向量召回筛选表达候选 |
| `expression_self_reflect` | true | 是否启用自动表达优化 |

### `[behavior]` 行为学习

| 字段 | 说明 |
|---|---|
| `learning_list` | 按聊天流配置：`[chat_id, 使用行为, 学习行为]` |
| `behavior_groups` | 行为学习互通组 |

### `[emoji]` 表情包

| 字段 | 默认 | 说明 |
|---|---|---|
| `emoji_chance` | 0.4 | 发送表情包的基础概率 |
| `max_reg_num` | 100 | 表情包最大注册数量 |
| `steal_emoji` | true | 是否偷取（保存）群里的表情包并可再发送 |
| `content_filtration` | false | 是否开启表情包过滤 |
| `vector_selection_enabled` | true | 是否用向量召回筛选表情候选 |
| `usage_scene_enabled` | true | 是否学习真人发送表情的场景 |

### `[webui]` 管理面板

监听地址已移至环境变量 `WEBUI_HOST` / `WEBUI_PORT`，本段只保留行为配置：

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 是否启用 WebUI |
| `mode` | production | 运行模式 development / production |
| `anti_crawler_mode` | loose | 防爬模式 false / strict / loose / basic |
| `allowed_ips` | 127.0.0.1 | IP 白名单，支持精确 IP、CIDR、通配符 |
| `trust_xff` | false | 是否解析 X-Forwarded-For |
| `secure_cookie` | false | 是否启用仅 HTTPS 的安全 Cookie |
| `plugin_registry_url` | RiyaBot 官方 Registry | 插件市场使用的 HTTPS Registry 地址；浏览器请求不能覆盖 |

### `[log]` 日志

| 字段 | 默认 | 说明 |
|---|---|---|
| `log_level` | INFO | 全局日志级别 |
| `console_log_level` | INFO | 控制台日志级别 |
| `file_log_level` | DEBUG | 文件日志级别 |
| `debug_plaintext_logging` | false | DEBUG 级别是否记录截断后的明文 |

### `[maim_message]` 消息服务与 API Server

| 字段 | 默认 | 说明 |
|---|---|---|
| `auth_token` | [] | 旧版 API 认证令牌，为空则不启用验证 |
| `enable_api_server` | false | 是否启用新版 API Server |
| `api_server_host` | 127.0.0.1 | 新版 API Server 主机，默认仅本机 |
| `api_server_port` | 8090 | 新版 API Server 端口 |
| `api_server_allowed_api_keys` | [] | 允许的 API Key；为空时仅本机允许匿名连接 |

### `[update]` 更新频道

| 字段 | 默认 | 说明 |
|---|---|---|
| `channel` | stable | `stable` 检查正式版本标签，`dev` 跟踪开发分支提交 |

## model_config.toml

模型配置分三部分：供应商（`APIProvider`）、模型（`ModelInfo`）、任务分配（`ModelTaskConfig`）。

### 供应商 `[[providers]]`

| 字段 | 默认 | 说明 |
|---|---|---|
| `api_key` | "" | API 密钥 |
| `client_type` | openai | 客户端类型 |
| `max_retry` | 2 | 最大重试次数 |
| `timeout` | 60 | SDK 请求/读取超时（秒），具体阶段由供应商客户端决定 |
| `retry_interval` | 10 | 重试间隔（秒） |

已有配置中显式填写的 `timeout` 不会被新缺省值覆盖。配置仍在重启后生效，见[审计修复兼容说明](audit-remediation.md)。

### 模型 `[[models]]`

| 字段 | 默认 | 说明 |
|---|---|---|
| `price_in` / `price_out` | 0.0 | 输入/输出计价 |
| `temperature` | None | 采样温度 |
| `max_tokens` | None | 最大生成 token |
| `force_stream_mode` | false | 是否强制流式 |
| `extra_params` | {} | 透传给供应商的额外参数 |

### 任务分配 `[model_task_config]`

每个任务是一个 `TaskConfig`（含 `model_list`、`max_tokens`、`temperature`、`slow_threshold`、`selection_strategy`）。可配置的任务：

| 任务 | 用途 |
|---|---|
| `utils` | 组件通用模型 |
| `replyer` | 首要回复模型 |
| `vlm` | 视觉语言模型 |
| `voice` | 语音识别模型 |
| `tool_use` | 工具调用模型 |
| `planner` | 规划模型 |
| `embedding` | 嵌入模型 |
| `memory_encoder` | 记忆编码模型 |
| `memory_weaver` | 梦境编织模型 |

> 说明：配置文件由程序自动生成和升级，不建议手动新增源码中不存在的字段。完整字段与默认值请查阅 `src/config/official_configs.py` 与 `src/config/api_ada_configs.py`。
