# RiyaBot 插件开发文档

> 聊天层已统一使用 Tool Call。新插件优先使用原生 `BaseTool`；`BaseAction` 仍作为兼容组件保留。

## 新手入门

- [📖 快速开始指南](quick-start.md) — 创建你的第一个插件
- [插件市场使用指南](../guide/plugin-market.md) — 安装、更新、回退和识别插件来源

## 组件功能详解

- [🔧 Tool 组件详解](tool-components.md) — 使用原生结构化 Tool Call 扩展模型能力
- [💻 Command 组件详解](command-components.md) — 学习直接响应命令的组件
- [⚙️ 配置文件系统指南](configuration-guide.md) — 学会使用自动生成的插件配置文件
- [📄 Manifest 系统指南](manifest-guide.md) — 了解插件元数据管理和配置架构
- [📦 依赖管理](dependency-management.md) — 声明插件和 Python 包依赖

## 组件选择指南

1. 使用 `BaseTool` 的场景

- ✅ 需要模型按结构化参数调用能力
- ✅ 需要把执行结果作为 Tool Result 返回聊天流程
- ✅ 查询信息或执行明确的外部操作

2. 使用 `BaseAction` 的场景

- ✅ 维护已有 Action 插件
- ✅ 需要 `ALWAYS`、`RANDOM`、`KEYWORD` 等激活机制
- ✅ 需要 `associated_types` 等聊天上下文筛选
- ✅ 扩展表情、语音等已有聊天动作

3. 使用 `BaseCommand` 的场景

- ✅ 用户需要明确输入命令
- ✅ 需要确定性匹配和执行
- ✅ 管理、查询或系统维护命令

`BaseAction` 和 `BaseTool` 都会在聊天层以 Tool Call 运行，但它们的插件基类、注册信息和执行器不同。不要因为两者都出现在模型工具列表中，就把 `BaseAction` 的代码直接改写成 `BaseTool`。

## API 参考

### 消息发送与处理 API

- [📤 发送 API](api/send-api.md) — 各种类型消息发送接口
- [💬 消息 API](api/message-api.md) — 消息获取、构建与查询接口
- [🗨️ 聊天流 API](api/chat-api.md) — 聊天流管理和查询接口

### AI 与生成 API

- [🧠 LLM API](api/llm-api.md) — 大语言模型交互接口，可以使用内置 LLM 生成内容
- [✨ 回复生成器 API](api/generator-api.md) — 智能回复生成接口，可以使用内置风格化生成器

### 表情包 API

- [😊 表情包 API](api/emoji-api.md) — 表情包选择和管理接口

### 关系系统 API

- [👤 人物信息 API](api/person-api.md) — 用户信息，处理璃夜认识的人和关系的接口

### 数据与配置 API

- [🗄️ 数据库 API](api/database-api.md) — 数据库操作接口
- [⚙️ 配置 API](api/config-api.md) — 配置读取和用户信息接口

### 插件和组件管理 API

- [🔌 插件 API](api/plugin-manage-api.md) — 插件加载和管理接口
- [🧩 组件 API](api/component-manage-api.md) — 组件注册和管理接口

### 日志与工具 API

- [📜 日志 API](api/logging-api.md) — logger 实例获取接口
- [🔧 工具 API](api/tool-api.md) — tool 获取接口

## 导入约定

`src/plugin_system/__init__.py` 中定义了 `__all__`，列出了所有公共导出的名称（`BasePlugin`、`register_plugin`、`ComponentInfo`、各 API 门面等）。两种导入方式都受支持：

```python
from src.plugin_system import BasePlugin, register_plugin, ComponentInfo
```

需要导入全部公共名称时，也可以使用通配导入。推荐使用显式导入，便于 IDE 提示和代码审查。

## 继续阅读

- [贡献指南](../CONTRIBUTE.md) — 提交插件或文档改动前的检查项
- [架构概览](../guide/architecture.md) — 了解插件系统在聊天运行时中的位置

## 支持

> 如果您在文档中发现错误或需要补充，请：

1. 检查最新的文档版本
2. 查看相关示例代码
3. 参考其他类似插件
4. 提交文档仓库 issue
