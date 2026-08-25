# 插件 Manifest 指南

每个 RiyaBot 插件目录都必须包含 `_manifest.json`。Manifest 描述插件身份、版本、作者、兼容范围和组件等静态元数据；用户可修改的运行参数仍由插件自己的 `config.toml` 管理。

RiyaBot 支持两种 Manifest：

| 版本 | 用途 | 状态 |
| --- | --- | --- |
| v1 | 已有插件、本地插件和普通 Git 安装 | 继续兼容 |
| v2 | 新插件和 Registry 市场插件 | 市场安装强制要求 |

v2 是严格契约。它不会让插件获得沙箱权限，而是为稳定身份、来源校验和兼容性检查提供可验证的数据。

## 文件边界

插件最小目录如下：

```text
my-plugin/
├── plugin.py
└── _manifest.json
```

- `_manifest.json`：静态身份、版本、作者、许可证、URL、兼容范围和能力声明；
- `config.toml`：启用状态、功能参数和其他运行时配置；
- `plugin.py`：插件入口。市场插件的 v2 入口固定为该文件。

不要把密码、Token、Cookie 或用户配置写入 Manifest。它可能被提交到公开仓库、Registry 和审核日志中。

## Manifest v1

v1 继续用于兼容已有插件。最小结构为：

```json
{
  "manifest_version": 1,
  "name": "插件显示名称",
  "version": "1.0.0",
  "description": "插件功能描述",
  "author": {
    "name": "作者名称"
  }
}
```

常用可选字段：

```json
{
  "id": "github.alice.my-plugin",
  "license": "MIT",
  "host_application": {
    "min_version": "0.14.0",
    "max_version": "0.99.99"
  },
  "homepage_url": "https://github.com/alice/my-plugin",
  "repository_url": "https://github.com/alice/my-plugin",
  "keywords": ["utility"],
  "categories": ["Utility"],
  "default_locale": "zh-CN",
  "locales_path": "_locales",
  "plugin_info": {
    "is_built_in": false,
    "plugin_type": "general",
    "components": [
      {
        "type": "tool",
        "name": "lookup",
        "description": "查询信息"
      }
    ]
  }
}
```

v1 校验会拒绝缺少必填字段、作者格式错误和不兼容的主程序版本。缺少 `license`、`keywords` 或 `categories` 只会产生警告。

v1 可以继续加载，但不能作为新的市场版本发布。市场需要稳定 ID、严格 SemVer、SDK 范围和完整来源信息，因此必须使用 v2。

## Manifest v2

完整示例：

```json
{
  "manifest_version": 2,
  "id": "github.alice.weather",
  "name": "天气插件",
  "version": "1.2.3",
  "description": "查询天气信息",
  "author": {
    "name": "Alice",
    "url": "https://github.com/alice"
  },
  "license": "MIT",
  "urls": {
    "repository": "https://github.com/alice/riyabot-weather",
    "homepage": "https://github.com/alice/riyabot-weather#readme",
    "documentation": "https://github.com/alice/riyabot-weather#readme",
    "issues": "https://github.com/alice/riyabot-weather/issues"
  },
  "host_application": {
    "min_version": "0.14.0",
    "max_version": "0.99.99"
  },
  "sdk": {
    "min_version": "1.0.0",
    "max_version": "1.99.99"
  },
  "entrypoint": "plugin.py",
  "capabilities": [
    "network",
    "messages.send",
    "filesystem.plugin_data"
  ],
  "i18n": {
    "default_locale": "zh-CN",
    "supported_locales": ["zh-CN"]
  },
  "keywords": ["weather"],
  "categories": ["Utility"],
  "plugin_info": {
    "is_built_in": false,
    "plugin_type": "general",
    "components": []
  }
}
```

### 身份和版本

| 字段 | 约束 |
| --- | --- |
| `manifest_version` | 固定为 `2` |
| `id` | 小写、至少三段命名空间，例如 `github.alice.weather` |
| `name` | 非空显示名称 |
| `version` | 严格遵循 SemVer 2.0.0，例如 `1.2.3` 或 `1.3.0-beta.1` |
| `description` | 非空描述 |

推荐使用“托管平台.作者.插件名”作为 ID。每段只能包含小写字母、数字和内部连字符，不能使用下划线。ID 一旦进入 Registry 就代表插件的长期身份，不应随仓库改名、显示名称或版本变化。

### 作者、许可证和 URL

| 字段 | 约束 |
| --- | --- |
| `author.name` | 必填 |
| `author.url` | 可选 HTTPS URL，公开发布时建议填写 |
| `license` | 必填 |
| `urls.repository` | 必填 HTTPS 仓库 URL；不允许凭据、查询参数或片段 |
| `urls.homepage` | 可选 HTTPS URL |
| `urls.documentation` | 可选 HTTPS URL |
| `urls.issues` | 可选 HTTPS URL |

Registry 记录中的仓库必须与每个审核版本的 `urls.repository` 完全一致。市场安装不会根据前端提交的新 URL 改变来源。

### 兼容范围

`host_application` 描述主程序范围，`sdk` 描述插件 SDK 范围。两者都必须提供 `min_version`，`max_version` 可省略：

```json
{
  "host_application": {
    "min_version": "0.14.0"
  },
  "sdk": {
    "min_version": "1.0.0",
    "max_version": "1.99.99"
  }
}
```

版本必须使用严格 SemVer，且最低版本不能高于最高版本。插件加载和市场安装都会根据当前 RiyaBot 与 SDK 版本检查这些范围。

### 入口、能力和国际化

- `entrypoint`：当前固定为 `plugin.py`；
- `capabilities`：唯一的小写能力名，可使用点号分段，例如 `messages.send`；
- `i18n.default_locale`：默认语言；
- `i18n.supported_locales`：支持语言列表，必须包含默认语言；
- `keywords`、`categories`：可选展示标签；
- `plugin_info`：可选组件摘要，字段结构与 v1 相同。

`capabilities` 是供用户查看和审核流程使用的声明，不是权限控制。插件仍运行在 RiyaBot 主进程内，可以使用该进程拥有的文件、网络、环境变量和系统权限。

### 严格解析

v2 不接受未声明的额外字段。市场安装还会拒绝重复 JSON 字段，并要求候选插件中的 Manifest 与 Registry 审核快照完全一致。以下任一情况都会拒绝安装：

- Registry ID、Manifest ID 或请求 ID 不一致；
- Registry 版本与 Manifest 版本不一致；
- 仓库、Tag 或真实 commit 与审核记录不一致；
- `plugin.py` 或 `_manifest.json` 不是普通文件；
- 兼容范围不包含当前主程序或 SDK；
- 插件目录包含符号链接、特殊文件，或超过市场大小限制。

## 从 v1 迁移到 v2

已有 v1 插件可以继续使用，不需要为了升级 RiyaBot 立即迁移。准备提交市场时按以下顺序迁移：

1. 选择稳定且唯一的三段小写 `id`，发布后不要再更改；
2. 把 `version` 改为严格 SemVer；
3. 将 `homepage_url`、`repository_url` 等移动到 `urls` 对象；
4. 补齐 `license`、`host_application`、`sdk`、`entrypoint` 和 `i18n`；
5. 根据实际行为声明 `capabilities`；
6. 确认仓库根目录包含普通文件 `plugin.py` 和 `_manifest.json`；
7. 为目标版本创建不可变 Tag，并确保 Manifest 版本与 Tag 对应的 Registry 记录一致。

若旧插件曾与其他插件共用 ID，应先为每个插件分配独立 ID。原先在主仓库维护的两个外部插件现已拆到独立仓库，且没有迁入内置插件目录：

- [OneBot/NapCat 适配器](https://github.com/hsd221/riyabot-plugin-onebot-adapter)：`github.hsd221.onebot-adapter`
- [QQ 收藏表情同步](https://github.com/hsd221/riyabot-plugin-qq-emoji-sync)：`github.hsd221.qq-emoji-sync`

## 校验和排错

RiyaBot 加载插件时会通过 `ManifestValidator` 校验 `_manifest.json`。错误会阻止加载，警告只提示建议补充的 v1 字段。

常见错误包括：

```text
- 缺少必需字段: name
- 作者信息缺少name字段或为空
- 主程序兼容性检查失败
- id: 市场插件 ID 必须是小写的至少三段命名空间
- version: 版本必须符合 SemVer 2.0.0
```

市场安装比普通本地加载多一层 Registry、Tag、commit 和仓库校验。能在本机加载的 v1 插件，不代表它已经满足市场发布要求。

## 相关文档

- [插件市场使用指南](../guide/plugin-market.md)
- [插件快速开始](./quick-start.md)
- [插件配置指南](./configuration-guide.md)
- [依赖管理](./dependency-management.md)
