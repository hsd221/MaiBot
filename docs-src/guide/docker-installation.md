# Docker 安装

Docker 安装适合希望用容器运行 RiyaBot 的场景。镜像会在独立的 Bun 构建阶段生成 WebUI 静态资源，宿主机不需要安装 Python、uv 或 Bun。仓库提供的 Compose 文件还会编排核心、适配器和 NapCat。

如果你需要修改源码或开发 WebUI，请改看[源码安装](installation.md)。

## 1. 准备环境

安装以下工具：

- Docker Engine；
- Docker Compose v2；
- Git；
- OpenSSL，用于生成跨容器连接令牌。

获取仓库并进入项目目录：

```bash
git clone https://github.com/hsd221/riyabot.git
cd riyabot
```

## 2. 准备 Compose 配置

仓库的 Compose 文件会把 `docker-config/mmc/.env` 和 `docker-config/adapters/config.toml` 作为文件挂载。首次使用前需要先创建这些路径；否则 Docker 可能把缺失的文件创建成目录，导致容器无法启动。

```bash
mkdir -p docker-config/mmc docker-config/adapters docker-config/napcat
cp template/template.env docker-config/mmc/.env
touch docker-config/adapters/config.toml
```

> 如果 `docker-config/mmc/.env` 已存在，不要用 `cp` 覆盖它，请直接修改现有文件。

核心与适配器通过旧版消息 WebSocket 通信。容器之间需要使用同一个强随机令牌，并且核心容器内的监听地址必须允许 Compose 网络访问。将令牌写入核心挂载的 `.env`，不能只写在当前 shell 的环境变量中：

```bash
export MAIBOT_LEGACY_SERVER_TOKEN="$(openssl rand -hex 32)"
sed -i \
  -e 's/^HOST=.*/HOST=0.0.0.0/' \
  -e 's/^WEBUI_HOST=.*/WEBUI_HOST=0.0.0.0/' \
  -e "s/^MAIBOT_LEGACY_SERVER_TOKEN=.*/MAIBOT_LEGACY_SERVER_TOKEN=${MAIBOT_LEGACY_SERVER_TOKEN}/" \
  docker-config/mmc/.env
chmod 600 docker-config/mmc/.env
```

`HOST=0.0.0.0` 让适配器能够访问核心的 `8000`；`WEBUI_HOST=0.0.0.0` 让宿主机映射的 WebUI 端口能够访问容器内的 `8001`。这两个值只适用于容器内的监听边界，不表示必须把端口公开到公网。

核心会优先使用容器进程已有的环境变量，其次读取挂载的 `.env`。当前 shell 的变量只有经 Compose `environment` 或 `docker run -e` 传入后才是容器变量；显式传入的空值同样优先。使用裸 `docker run` 时，缺省仍监听 `127.0.0.1`；跨容器连接需要显式传入 `HOST=0.0.0.0` 和与适配器一致的令牌。宿主机端口映射继续按私有访问范围配置。

旧版适配器镜像如果不支持令牌变量，可以暂时使用兼容模式。此时要同时修改核心挂载的 `.env` 和 Compose 环境变量：

```bash
export MAIBOT_LEGACY_SERVER_TOKEN=
export MAIBOT_ALLOW_UNAUTHENTICATED_LEGACY_SERVER=1
sed -i \
  -e 's/^MAIBOT_LEGACY_SERVER_TOKEN=.*/MAIBOT_LEGACY_SERVER_TOKEN=/' \
  -e 's/^MAIBOT_ALLOW_UNAUTHENTICATED_LEGACY_SERVER=.*/MAIBOT_ALLOW_UNAUTHENTICATED_LEGACY_SERVER=1/' \
  docker-config/mmc/.env
```

兼容模式只适用于受信的私有 Compose 网络。它不应暴露给公网；升级适配器后，应改回共享令牌并关闭兼容开关。

## 3. 构建并启动

在项目根目录执行：

```bash
docker compose up -d --build
```

Compose 会根据仓库根目录的 `Dockerfile` 构建核心镜像，并启动以下服务：

- **core**：RiyaBot 核心与 WebUI；
- **adapters**：消息协议适配器；
- **napcat**：登录消息平台账号、收发消息的客户端。

查看服务状态和日志：

```bash
docker compose ps
docker compose logs -f core
```

## 4. 完成首次配置

默认情况下，核心的 WebUI 映射到宿主机 `127.0.0.1:18001`。打开 http://127.0.0.1:18001，按配置向导确认协议并填写 bot 与模型信息。

首次配置生成的 TOML 配置和运行数据会写入宿主机目录；前一步准备的 `.env` 也会作为持久化配置保留，重建容器不会清空这些内容。配置项含义见[配置说明](configuration.md)。

## 5. 了解端口与持久化目录

Compose 默认使用以下边界：

| 用途 | 宿主机默认值 | 容器内位置或说明 |
|---|---:|---|
| WebUI | `127.0.0.1:18001` | 容器 `8001`；可用 `RIYABOT_WEBUI_BIND_ADDRESS` / `RIYABOT_WEBUI_PORT` 修改 |
| 旧版消息 WebSocket | 不发布 | 容器 `8000`；跨容器通信不需要宿主机端口映射 |
| NapCat 管理端口 | `127.0.0.1:6099` | 容器 `6099`；可用 `NAPCAT_BIND_ADDRESS` / `NAPCAT_PORT` 修改 |
| RiyaBot 配置 | `./docker-config/mmc/` | `/RiyaBot/config/` |
| RiyaBot 数据 | `./data/RiyaBot/` | `/RiyaBot/data/`，包含插件与日志目录 |
| 适配器配置 | `./docker-config/adapters/` | `/RiyaBot/adapters-config/` |
| NapCat 配置 | `./docker-config/napcat/` | `/app/napcat/config/` |

不要发布核心的 `8000` 端口。跨主机连接时应使用共享令牌，并在[部署指南](deployment.md)中确认监听地址和反向代理边界。

## 6. 使用调试面板

需要临时查看 SQLite 数据库时，可以启用 `debug` profile：

```bash
docker compose --profile debug up -d
```

`sqlite-web` 只读挂载 `./data/RiyaBot/`，默认映射到宿主机 `127.0.0.1:8120`。不需要时不要启用这个服务。

## 7. 更新与停止

拉取新的源码后重新构建核心镜像：

```bash
git pull
docker compose up -d --build
```

升级前请备份 `docker-config/` 与 `data/RiyaBot/`。停止服务但保留持久化数据：

```bash
docker compose down
```

## 下一步

- [配置说明](configuration.md) — 了解首次配置和 TOML 字段。
- [部署指南](deployment.md) — 配置跨主机连接、端口安全和运行时目录。
- [架构概览](architecture.md) — 了解核心、适配器与 WebUI 的关系。
