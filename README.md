<div align="center">
  <h1>RiyaBot <sub><small>璃夜Bot</small></sub></h1>
  <p>一个面向 QQ 群聊的拟生命体聊天机器人，基于大语言模型、长期记忆、行为规划和插件系统构建。</p>

  <p>
    <img src="https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
    <img src="https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=white" alt="React 19">
    <img src="https://img.shields.io/badge/FastAPI-WebUI-009688?logo=fastapi&logoColor=white" alt="FastAPI WebUI">
    <img src="https://img.shields.io/badge/License-GPL--3.0-blue" alt="GPL-3.0">
  </p>

  <p><a href="https://hsd221.github.io/riyabot/">在线文档</a> · <a href="https://github.com/hsd221/riyabot/blob/dev/changelogs/changelog.md">更新日志</a></p>
</div>

## 介绍

RiyaBot 不是一个只等命令的工具型 bot。它更像一个会长期停留在群聊里的虚拟角色：观察上下文、决定什么时候说话、学习群友表达、使用表情包和工具，并在持续互动中形成自己的记忆与行为习惯。

这个仓库基于 MaiBot/MaiCore fork 后继续改造，当前目标是把它整理成一个有独立命名、独立文档和更清晰维护边界的项目。

## 核心能力

- **群聊行为规划**：根据聊天上下文决定回复、等待、使用动作（Action）或调用工具（Tool），支持主动发起话题和使用表情包。
- **分层长期记忆**：SQLite 为主存储、可选 Qdrant/FAISS 向量索引与 BM25 检索的组合，覆盖归档、摘要、编码、检索与遗忘，并维护用户画像、人物关系和知识片段。
- **表达与行为学习**：内置学习器持续从聊天历史中提炼表达方式、群体黑话和行为习惯，用于生成更自然的拟人回复。
- **多模型接入**：统一的模型客户端支持 OpenAI 兼容接口与 Google Gemini，可按任务分配模型，并附带请求追踪与嵌入配置。
- **插件系统**：支持 Action、Command、Tool、Event 等组件类型，提供注册、配置、权限等完整插件 API；内置表情包、知识库、插件管理、TTS 等插件。
- **Web 管理面板**：React 19 + FastAPI 的本地管理界面，覆盖首次配置向导、模型管理、聊天记录导入、人物关系、表情包、日志与插件市场。
- **协议适配**：默认面向 QQ/NapCat + MaiBot 适配器部署场景，Compose 一次拉起核心、适配器与 NapCat。

## 快速开始

### 环境要求

- Python 3.10+（推荐使用 [uv](https://github.com/astral-sh/uv) 管理依赖）
- [Bun](https://bun.sh/)（仅开发 WebUI 前端或使用 `dev` 分支时需要）
- 一个支持 OpenAI 兼容接口或 Gemini 的模型 API Key

### 安装与启动

```bash
git clone https://github.com/hsd221/riyabot.git
cd riyabot
uv sync
python bot.py
```

- 首次启动会根据 `src/config/` 中的 Python 配置定义自动生成 `config/bot_config.toml` 和 `config/model_config.toml`，无需手工复制 TOML 模板。
- 首次启动需要确认 EULA 和隐私协议（交互确认，或通过 `EULA_AGREE` / `PRIVACY_AGREE` 环境变量传入哈希，Docker 部署常用后者）。
- 首次配置完成前只会启动 WebUI；请在管理面板的配置向导中填写 bot 信息并完成模型管理与任务分配。
- `config/`、`data/`、`logs/` 属于运行时目录，不应提交到仓库。

### WebUI 访问与前端开发

后端会从 `webui/dist/` 托管前端静态文件并默认监听 `8001` 端口。`main` 分支的 `webui/dist/` 已由 CI 构建并提交到仓库，克隆后开箱即用；使用 `dev` 分支或修改前端时需要手动构建：

```bash
cd webui
bun install
bun run build   # 或 bun run dev 进行前端热更新开发
```

## Docker 部署

官方镜像发布在 GHCR（`main` 分支与版本标签构建正式镜像，`dev` 分支每日构建开发镜像）：

```bash
docker pull ghcr.io/hsd221/riyabot:latest
```

也可以在本地构建（Dockerfile 会在独立 Bun 阶段生成 WebUI 静态资源，无需宿主机前端构建）：

```bash
docker build -t riyabot .
docker compose up -d
```

Compose 默认包含四个服务：`core`（核心）、`adapters`（MaiBot 适配器）、`napcat`（QQ 协议端），以及可选的 `sqlite-web` 调试服务（`docker compose --profile debug up` 启用）。WebUI 默认只绑定本机 `18001` 端口，如需远程访问请自行配置反向代理。

### 旧版消息服务器的跨容器认证

核心与适配器之间的旧版消息 WebSocket 默认拒绝远程匿名监听。若适配器版本支持 `MAIBOT_LEGACY_SERVER_TOKEN`，请先为核心与适配器设置同一个强随机令牌：

```bash
export MAIBOT_LEGACY_SERVER_TOKEN="$(openssl rand -hex 32)"
docker compose up -d
```

若迁移中的旧适配器镜像尚不支持该变量，可暂时保持令牌为空并设置
`MAIBOT_ALLOW_UNAUTHENTICATED_LEGACY_SERVER=1`。此兼容方式仅适用于受信的私有 Compose 网络，且绝不能发布
核心的 `8000` 端口；完成适配器升级后应立即改用共享令牌并关闭兼容开关。

```bash
export MAIBOT_LEGACY_SERVER_TOKEN=
export MAIBOT_ALLOW_UNAUTHENTICATED_LEGACY_SERVER=1
docker compose up -d
```

注意：当前 Compose 使用容器内 `/RiyaBot` 和宿主机 `data/RiyaBot` / `docker-config` 作为持久化路径。若你从旧部署迁移，需要手动把旧数据目录复制到新路径。

## 安全与环境变量

`template/template.env` 列出了全部可用的启动环境变量，包括 WebUI 监听地址、遥测端点、以及一组默认关闭的兼容开关（明文模型 API、私有 Git 仓库、内网媒体 URL、注入端点等）。这些开关用于在受信环境中放宽默认的 SSRF/认证防护，除非明确了解后果，建议保持默认值。

## 程序更新

WebUI 的“系统设置 > 关于”可以检查并切换更新频道：

- `正式版` 选择 `main` 分支可达的最新正式 SemVer 标签，不包含预发布标签。
- `开发版` 跟踪远端 `dev` 分支的完整提交 SHA。

切换频道只保存跟踪偏好，不会立即更新。Git 源码安装在当前提交可识别、受跟踪文件无修改、目标为快进提交且
`git`、`uv`、`bun` 均可用时，可以由 Runner 执行一键更新并重启。目标落后、分支已分叉或检查结果变化时，
更新会被拒绝，不会执行强制重置或降级。

代码快进后如果依赖同步或 WebUI 构建失败，Runner 会记录失败阶段并尝试启动当前工作区；WebUI 会读取该结果，
不会仅因服务恢复就把本次更新显示为成功。此时目标代码可能已经检出，需要根据 Runner 日志修复依赖或构建问题。

Docker 镜像和压缩包安装支持在线检查，但不会在运行中的容器或安装目录内替换自身。Docker 部署发现新版本后，
请拉取对应的 GHCR 镜像并重新创建容器；无需也不应向核心容器挂载 Docker socket。

## 项目结构

```text
.
├── bot.py                  # Runner/Worker 入口：Runner 监督并重启 Worker 进程
├── src/                    # Python 后端
│   ├── main.py             # MainSystem：核心服务组装与连接
│   ├── chat/               # 群聊/私聊编排、行为规划（heart_flow / brain_chat）、回复器、表情系统
│   ├── memory/             # 分层记忆：归档/摘要/编码/检索、向量索引、图存储、遗忘
│   ├── bw_learner/         # 表达与行为学习：表达学习、黑话挖掘、历史导入
│   ├── llm_models/         # 模型客户端、请求追踪、嵌入配置
│   ├── plugin_system/      # 插件 SDK 与公开 API（apis/ 门面）
│   ├── plugins/built_in/   # 内置插件：表情包、知识库、插件管理、TTS
│   ├── update_system/      # 更新频道检查与 Git 快进更新
│   ├── common/             # 日志、数据库、Prompt 管理等基础设施
│   ├── config/             # TOML 配置定义、生成与升级
│   └── webui/              # FastAPI WebUI 后端
├── webui/                  # React 19 + TypeScript + Vite 管理面板（Bun 构建）
├── prompts/                # 外部 Prompt 模板
├── plugins/                # 外部插件目录（如 OneBot 适配器、表情包同步）
├── template/               # 启动环境变量模板 template.env
├── tests/                  # unittest 单元测试、消息流模拟器与 E2E 工具
├── scripts/                # 维护、迁移、评估与压测脚本
├── docs-src/               # VitePress 文档源文件
├── changelogs/             # 更新日志
├── docker-config/          # Compose 持久化的宿主机配置边界
├── Dockerfile / docker-compose.yml
└── EULA.md / PRIVACY.md
```

## 开发与测试

代码风格由 Ruff 强制执行（行宽 120，启用 `E` / `F` / `B` 规则）：

```bash
ruff check --fix .
ruff format .
```

单元测试使用标准库 `unittest`：

```bash
uv run python -m unittest discover -s tests -p 'test_*.py'
```

消息流模拟与 E2E：

```bash
MAIBOT_WORKER_PROCESS=1 uv run python tests/simulator.py --file tests/data/chat_exports/chat_histories_1.json
uv run python tests/run_e2e.py --quick
```

文档站点使用 VitePress 构建：

```bash
cd docs-src && bun install --frozen-lockfile && bun run docs:build
```

## 贡献

`dev` 是集成分支，`main` 是稳定发布分支；普通改动请先进入 `dev`（PR 或明确授权的直接提交），经验证后再通过 PR 晋升到 `main`。提交信息请使用 Conventional Commit 前缀（`feat:`、`fix:`、`refactor:`、`chore:`）。

提交 PR 前请说明：

- 这次修改解决的问题或行为变化
- 是否影响配置、数据目录、插件 API 或部署方式
- 已经运行过的验证命令
- WebUI 相关修改的截图或录屏

新增功能建议先通过 Issue 讨论，避免和现有架构方向冲突。

## 来源与许可

RiyaBot fork 自 MaiBot/MaiCore，并继续遵循原项目的 GPL-3.0 开源许可。原项目作者、维护者和贡献者的工作构成了这个项目的基础。

使用前请阅读 [EULA](EULA.md) 和 [隐私协议](PRIVACY.md)。QQ bot、AI 生成内容和第三方模型服务都有各自的使用风险，请按平台规则和当地法律法规谨慎部署。
