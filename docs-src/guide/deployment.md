# 部署指南

本文说明 RiyaBot 的部署方式、组件连接和运行时目录。安装前请先阅读[源码安装](installation.md)或[Docker 安装](docker-installation.md)。

## 部署方式

| 方式 | 适合 | 说明 |
|---|---|---|
| [源码安装](installation.md) | 开发、调试、开发插件 | 在宿主机安装 Python，`main` 分支已内置构建好的 WebUI |
| [Docker 安装](docker-installation.md) | 长期运行、快速部署 | 镜像自带 WebUI，用 Compose 编排核心、适配器和 NapCat |

## 组件构成

一个完整的部署包含三部分：

- **核心（core）**：RiyaBot 本体，负责聊天行为、记忆、插件与 WebUI；
- **适配器（adapters）**：把消息平台的消息协议转换成 RiyaBot 使用的旧版消息协议；
- **协议端（如 NapCat）**：真正登录消息平台账号、收发底层消息的客户端。

核心与适配器之间通过**旧版消息 WebSocket**通信。它默认只监听回环地址（`127.0.0.1:8000`），跨主机连接时需要显式配置监听地址和共享令牌。

## 连接适配器

核心与适配器可以部署在同一台机器（回环通信，无需额外配置），也可以跨主机部署。跨主机时需要两步：

1. **生成强随机令牌**，并把同一个值配置给核心与适配器：

```bash
export MAIBOT_LEGACY_SERVER_TOKEN="$(openssl rand -hex 32)"
```

2. **让核心监听非回环地址**：把 `.env` 中的 `HOST` 改为 `0.0.0.0`（Docker 部署按 [Docker 安装](docker-installation.md) 操作）。

旧版适配器镜像如果不支持令牌变量，可以暂时开启匿名兼容模式：

```bash
export MAIBOT_LEGACY_SERVER_TOKEN=
export MAIBOT_ALLOW_UNAUTHENTICATED_LEGACY_SERVER=1
```

这个兼容开关只适用于受信的私有网络，**不要把旧版消息端口发布到公网**；适配器升级后，应改回共享令牌并关闭兼容开关。

## 端口

| 服务 | 默认端口 | 说明 |
|---|---|---|
| 旧版消息 WebSocket | `8000` | 核心与适配器通信；**不要发布到公网** |
| WebUI | `8001` | 管理面板，默认只监听本机 |
| Docker WebUI（宿主机） | `18001` | Docker 部署时映射到宿主机的 WebUI 端口 |

需要公网访问 WebUI 时，只开放 WebUI 端口并放在受控的 HTTPS 入口后面。不要同时发布 `8000`，也不要为了调试把旧版消息服务改成匿名公网监听。

## 环境变量

以下是最常用的环境变量（完整列表见 `template/template.env`）：

进程已有环境变量优先于项目 `.env`，包括 systemd、Compose 或 `docker run -e` 注入的值；显式注入的空字符串也不会被 `.env` 覆盖。修改部署配置后需要重启。

| 变量 | 默认值 | 用途 |
|---|---|---|
| `HOST` | `127.0.0.1` | 旧版消息服务器监听地址；跨主机连接时改为 `0.0.0.0` |
| `PORT` | `8000` | 核心与适配器使用的旧版消息端口 |
| `WEBUI_HOST` | `127.0.0.1` | WebUI 服务监听地址 |
| `WEBUI_PORT` | `8001` | WebUI 服务端口 |
| `MAIBOT_LEGACY_SERVER_TOKEN` | 空 | 核心与适配器共享的连接令牌 |
| `MAIBOT_ALLOW_UNAUTHENTICATED_LEGACY_SERVER` | `0` | 是否允许旧版消息服务器远程匿名监听 |

Docker 部署额外使用以下宿主机映射变量（只在 Docker 部署时生效）：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `RIYABOT_WEBUI_BIND_ADDRESS` | `127.0.0.1` | Docker WebUI 端口的宿主机监听地址 |
| `RIYABOT_WEBUI_PORT` | `18001` | Docker WebUI 的宿主机端口 |
| `NAPCAT_BIND_ADDRESS` | `127.0.0.1` | NapCat 管理端口的宿主机监听地址 |
| `NAPCAT_PORT` | `6099` | NapCat 管理端口的宿主机端口 |

源码安装首次启动时会根据 `template/template.env` 自动生成 `.env`，多数情况不需要手动改；Docker 部署的端口映射通过 Compose 环境变量配置，见 [Docker 安装](docker-installation.md)。

## 运行时目录

源码安装使用项目根目录的 `config/`、`data/` 和 `logs/` 保存运行时状态。Docker 安装把对应内容持久化到宿主机的 `docker-config/` 和 `data/RiyaBot/`，具体映射见 [Docker 安装](docker-installation.md)。

这些目录不是源码，不应提交到仓库。升级或迁移前，请先备份配置、数据库、插件和日志中需要保留的内容。

## 程序更新

WebUI 的「系统设置 > 关于」可以检查并切换更新频道：

- **正式版**：跟踪 `main` 分支可达的最新正式 SemVer 标签，不含预发布版本；
- **开发版**：跟踪远端 `dev` 分支的完整提交 SHA。

源码安装由 Runner 执行一键更新并重启，前提是：当前提交可识别、受跟踪文件无修改、目标为快进提交，且 `git`、`uv`、`bun` 均可用。目标落后、分支分叉或检查结果变化时，更新会被拒绝，不会强制重置或降级。

Docker 不会在运行中的容器内替换自身。更新 Docker 部署时，请按照 [Docker 安装](docker-installation.md) 中的流程拉取新源码、备份持久化目录并重新构建容器。

## 相关文档

- [配置说明](configuration.md) — 了解 WebUI 配置向导和生成的 TOML 文件。
- [架构概览](architecture.md) — 了解 Runner、Worker、核心服务与适配器的关系。
