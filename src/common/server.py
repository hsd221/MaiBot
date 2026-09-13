import asyncio
import ipaddress
import os
import secrets
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Body, FastAPI, Header, HTTPException
from rich.traceback import install
from uvicorn import Config, Server as UvicornServer

from src.common.background_tasks import spawn_background_task

install(extra_lines=3)


# ---------------------------------------------------------------------------
# 消息注入端点 — 供 E2E 测试模拟器使用，必须通过环境变量显式启用
# ---------------------------------------------------------------------------


def _is_loopback_host(host: str) -> bool:
    """仅将明确的回环监听地址视为本机可访问。"""
    normalized_host = host.strip().lower().rstrip(".")
    if normalized_host == "localhost":
        return True

    if normalized_host.startswith("[") and normalized_host.endswith("]"):
        normalized_host = normalized_host[1:-1]
    normalized_host = normalized_host.split("%", maxsplit=1)[0]

    try:
        return ipaddress.ip_address(normalized_host).is_loopback
    except ValueError:
        return False


def _register_inject_endpoint(app: FastAPI, required_token: Optional[str] = None) -> None:
    """向 FastAPI 实例注册 POST /message/inject 端点。"""
    if any(getattr(route, "path", None) == "/message/inject" for route in app.routes):
        return

    @app.post("/message/inject")
    async def inject_message(
        message: Annotated[dict[str, Any], Body()],
        inject_token: Annotated[Optional[str], Header(alias="X-MaiBot-Inject-Token")] = None,
    ):
        """接收模拟器发送的消息 JSON，注入 bot 消息处理管线。"""
        if required_token is not None and (
            inject_token is None
            or not secrets.compare_digest(inject_token.encode("utf-8"), required_token.encode("utf-8"))
        ):
            raise HTTPException(status_code=401, detail="消息注入凭据无效")

        if message.get("_probe") is True:
            return {"status": "ok"}

        from src.chat.message_receive.bot import chat_bot

        spawn_background_task(chat_bot.message_process(message), name="inject-message-process")
        return {"status": "accepted"}


# ---------------------------------------------------------------------------


class Server:
    def __init__(self, host: Optional[str] = None, port: Optional[int] = None, app_name: str = "MaiMCore"):
        self.app = FastAPI(title=app_name, openapi_url=None, docs_url=None, redoc_url=None)
        self._host: str = "127.0.0.1"
        self._port: int = 8080
        self._server: Optional[UvicornServer] = None
        self.set_address(host, port)

        if os.environ.get("MAIBOT_ENABLE_INJECT_ENDPOINT") == "1":
            inject_token = os.environ.get("MAIBOT_INJECT_TOKEN")
            if inject_token is not None and not inject_token.strip():
                inject_token = None
            if not _is_loopback_host(self._host) and inject_token is None:
                raise RuntimeError("非回环地址启用消息注入端点时必须设置 MAIBOT_INJECT_TOKEN")
            _register_inject_endpoint(self.app, required_token=inject_token)

    def register_router(self, router: APIRouter, prefix: str = ""):
        """注册路由

        APIRouter 用于对相关的路由端点进行分组和模块化管理：
        1. 可以将相关的端点组织在一起，便于管理
        2. 支持添加统一的路由前缀
        3. 可以为一组路由添加共同的依赖项、标签等

        示例:
            router = APIRouter()

            @router.get("/users")
            def get_users():
                return {"users": [...]}

            @router.post("/users")
            def create_user():
                return {"msg": "user created"}

            # 注册路由，添加前缀 "/api/v1"
            server.register_router(router, prefix="/api/v1")
        """
        self.app.include_router(router, prefix=prefix)

    def set_address(self, host: Optional[str] = None, port: Optional[int] = None):
        """设置服务器地址和端口"""
        if host:
            self._host = host
        if port:
            self._port = port

    async def run(self):
        """启动服务器"""
        # 禁用 uvicorn 默认日志和访问日志
        # 设置 ws_max_size 为 100MB，支持大消息（如包含多张图片的转发消息）
        config = Config(
            app=self.app,
            host=self._host,
            port=self._port,
            log_config=None,
            access_log=False,
            ws_max_size=104_857_600,  # 100MB
        )
        self._server = UvicornServer(config=config)
        try:
            await self._server.serve()
        except KeyboardInterrupt:
            await self.shutdown()
            raise
        except Exception as e:
            await self.shutdown()
            raise RuntimeError(f"服务器运行错误: {str(e)}") from e
        finally:
            await self.shutdown()

    async def shutdown(self):
        """安全关闭服务器"""
        if self._server:
            self._server.should_exit = True
            try:
                # 添加 3 秒超时，避免 shutdown 永久挂起
                await asyncio.wait_for(self._server.shutdown(), timeout=3.0)
            except asyncio.TimeoutError:
                # 超时就强制标记为 None，让垃圾回收处理
                pass
            except Exception:
                # 忽略其他异常
                pass
            finally:
                self._server = None

    def get_app(self) -> FastAPI:
        """获取 FastAPI 实例"""
        return self.app


global_server = None


def get_server_address() -> tuple[str, int]:
    """解析内部服务器地址，缺省值与 template.env 一致。"""
    host = os.getenv("HOST", "127.0.0.1").strip()
    if not host:
        raise ValueError("HOST 不能为空，请检查 .env 或服务环境变量")
    try:
        port = int(os.getenv("PORT", "8000"))
    except ValueError as exc:
        raise ValueError("PORT 必须为 1 到 65535 的整数，请检查 .env 或服务环境变量") from exc
    if not 1 <= port <= 65535:
        raise ValueError("PORT 必须为 1 到 65535 的整数，请检查 .env 或服务环境变量")
    return host, port


def get_global_server() -> Server:
    """获取全局服务器实例"""
    global global_server
    if global_server is None:
        host, port = get_server_address()
        global_server = Server(host=host, port=port)
    return global_server
