import asyncio
import time
from maim_message import MessageServer

from src.common.remote import TelemetryHeartBeatTask
from src.manager.async_task_manager import async_task_manager
from src.chat.utils.statistic import OnlineTimeRecordTask

# from src.chat.utils.token_statistics import TokenStatisticsTask
from src.chat.emoji_system.emoji_manager import get_emoji_manager
from src.chat.message_receive.chat_stream import get_chat_manager
from src.config.config import CONFIG_DIR, global_config, get_created_config_files, model_config
from src.chat.message_receive.bot import chat_bot
from src.common.background_tasks import spawn_background_task
from src.common.logger import get_logger
from src.common.agreement import are_agreements_confirmed
from src.common.prompt_manager import prompt_manager
from src.common.server import get_global_server, Server
from rich.traceback import install

# from src.api.main import start_api_server

# 导入新的插件管理器
from src.plugin_system.core.plugin_manager import plugin_manager

# 导入消息API和traceback模块
from src.common.message import get_global_api
from src.bw_learner.expression_auto_check_task import ExpressionAutoCheckTask

# 插件系统现在使用统一的插件加载器

install(extra_lines=3)

logger = get_logger("main")


class MainSystem:
    def __init__(self):
        # 使用消息API替代直接的FastAPI实例
        self.app: MessageServer = get_global_api()
        self.server: Server = get_global_server()
        self.webui_server = None  # 独立的 WebUI 服务器
        self.setup_required = self._is_setup_required()

        # 设置独立的 WebUI 服务器
        self._setup_webui_server()

    def _is_setup_required(self) -> bool:
        """判断是否需要先进入 WebUI 首次配置向导。"""
        try:
            from src.webui.token_manager import get_token_manager

            created_config_files = get_created_config_files()
            agreements_pending = not are_agreements_confirmed()
            first_setup = get_token_manager().is_first_setup()
            model_config_error = model_config.get_runtime_readiness_error()
            setup_required = agreements_pending or first_setup or bool(created_config_files) or bool(model_config_error)

            if setup_required:
                logger.warning(
                    "首次配置未完成，将仅启动 WebUI 向导",
                    event_code="system.setup.required",
                    agreements_pending=agreements_pending,
                    first_setup=first_setup,
                    created_config_files=created_config_files,
                    model_config_error=model_config_error,
                )

            return setup_required
        except Exception:
            logger.exception("首次配置状态检测失败，将仅启动 WebUI", event_code="system.setup.check_failed")
            return True

    def _setup_webui_server(self):
        """设置独立的 WebUI 服务器"""
        from src.config.config import global_config

        if not global_config.webui.enabled and not self.setup_required:
            logger.info("WebUI 已禁用", event_code="webui.disabled")
            return
        if not global_config.webui.enabled and self.setup_required:
            logger.warning(
                "首次配置未完成，已临时忽略 webui.enabled=false 并启动 WebUI",
                event_code="webui.setup.force_enabled",
            )

        try:
            from src.webui.webui_server import get_webui_server

            self.webui_server = get_webui_server()

        except Exception:
            logger.exception("WebUI 服务器初始化失败", event_code="webui.init_failed")

    async def initialize(self):
        """初始化系统组件"""
        if self.setup_required:
            logger.info("跳过主系统组件初始化，等待 WebUI 首次配置完成", event_code="system.setup.init_skipped")
            return

        logger.info("系统初始化开始", event_code="system.initialize.started", bot_name=global_config.bot.nickname)

        # 其他初始化任务
        await self._init_components()

        logger.info(
            "系统初始化完成",
            event_code="system.initialize.completed",
            bot_name=global_config.bot.nickname,
            docs_url="https://hsd221.github.io/riyabot/",
            plugin_docs_url="https://hsd221.github.io/riyabot/plugins/index",
            statistics_database="data/RiyaBot.db",
            statistics_page="/statistics",
        )

    async def _init_components(self):
        """初始化其他组件"""
        init_start_time = time.time()

        prompt_manager.load_prompts()
        logger.info("外部提示词模板已加载", event_code="prompt.templates.loaded", count=prompt_manager.prompt_count)

        # 添加在线时间统计任务
        await async_task_manager.add_task(OnlineTimeRecordTask())

        # 添加遥测心跳任务
        await async_task_manager.add_task(TelemetryHeartBeatTask())

        # 添加表达方式自动检查任务
        await async_task_manager.add_task(ExpressionAutoCheckTask())

        # 启动API服务器
        # start_api_server()
        # logger.info("API服务器启动成功")

        # 加载所有actions，包括默认的和插件的
        plugin_manager.load_all_plugins()

        # 初始化表情管理器
        get_emoji_manager().initialize()
        logger.info("表情包管理器初始化完成", event_code="emoji.manager.initialized")

        # 初始化聊天管理器
        await get_chat_manager().initialize()
        spawn_background_task(get_chat_manager().auto_save(), name="chat-manager-auto-save")

        logger.info("聊天管理器初始化完成", event_code="chat.manager.initialized")

        # 初始化记忆存储
        try:
            from src.memory import MemoryStore, MemoryStoreConfig
            from src.llm_models.embedding_profile import activate_embedding_runtime
            from src.services.embedding_profile_monitor import EmbeddingProfileMonitorTask

            mc = global_config.memory
            embedding_runtime = activate_embedding_runtime(model_config, mc.embedding_dimension)
            embedding_profile = embedding_runtime.profile
            memory_config = MemoryStoreConfig(
                sqlite_path=mc.sqlite_path,
                qdrant_url=mc.qdrant_url,
                qdrant_api_key=mc.qdrant_api_key or None,
                qdrant_local_path=mc.qdrant_local_path,
                embedding_dimension=mc.embedding_dimension,
                embedding_signature=embedding_profile.signature,
                embedding_model_name=embedding_profile.model_name,
                collection_name_atoms=mc.collection_name_atoms,
                collection_name_graph=mc.collection_name_graph,
                vector_batch_size=mc.vector_batch_size,
            )
            store = MemoryStore(memory_config)
            await store.initialize()
            logger.info("记忆存储初始化完成", event_code="memory.store.initialized")
            forgetting_manager = None

            await async_task_manager.add_task(
                EmbeddingProfileMonitorTask(
                    store,
                    config_dir=CONFIG_DIR,
                    task_manager=async_task_manager,
                )
            )
            logger.info(
                "embedding profile 运行时检测任务已注册",
                event_code="embedding.profile.monitor_registered",
                interval_seconds=15,
            )

            # 启动记忆遗忘定期扫描
            try:
                from src.memory.forgetting import ForgettingManager, ForgettingSweepTask

                forgetting_manager = ForgettingManager(store)
                await async_task_manager.add_task(ForgettingSweepTask(forgetting_manager))
                logger.info(
                    "记忆遗忘扫描任务已注册", event_code="memory.forgetting.task_registered", interval_seconds=3600
                )
            except Exception:
                logger.warning(
                    "记忆遗忘扫描任务注册失败", event_code="memory.forgetting.task_register_failed", exc_info=True
                )

            # 创建写操作日志记录器（供编码管线和一致性协调任务共享）
            memory_op_logger = None
            try:
                from src.memory.write_ops import WriteOpLogger

                memory_op_logger = WriteOpLogger(
                    db_path=memory_config.sqlite_path,
                    max_entries=5000,
                )
                logger.info("记忆写操作日志记录器初始化完成", event_code="memory.write_op_logger.initialized")
            except Exception:
                logger.warning(
                    "记忆写操作日志记录器初始化失败", event_code="memory.write_op_logger.init_failed", exc_info=True
                )

            # 启动时先重放上次异常退出留下的 pending/in_progress/failed 写操作。
            if memory_op_logger is not None:
                try:
                    recovered_ops = await memory_op_logger.replay_failed_ops(store)
                    logger.info(
                        "记忆写操作恢复完成",
                        event_code="memory.write_ops.replay_completed",
                        recovered_count=len(recovered_ops),
                    )
                except Exception:
                    logger.warning(
                        "记忆写操作恢复失败",
                        event_code="memory.write_ops.replay_failed",
                        exc_info=True,
                    )

            # 启动编码管线（连接 Layer 2 → Layer 3）
            try:
                from src.memory.encoding_pipeline import EncodingPipeline, EncodingTask

                pipeline = EncodingPipeline(store, op_logger=memory_op_logger)
                await async_task_manager.add_task(EncodingTask(pipeline, interval=300))
                logger.info("编码管线任务已注册", event_code="memory.encoding.task_registered", interval_seconds=300)
            except Exception:
                logger.warning("编码管线任务注册失败", event_code="memory.encoding.task_register_failed", exc_info=True)

            # 启动梦境维护任务
            try:
                from src.memory.dream_agent import DreamTask
                from src.memory.dream_weaver import DreamWeaver
                from src.memory.graph_store import GraphStore

                graph_store = GraphStore()
                dream_weaver = DreamWeaver(store=store)
                dream_task = DreamTask(
                    store=store,
                    forgetting_manager=forgetting_manager,
                    graph_store=graph_store,
                    dream_weaver=dream_weaver,
                )
                await async_task_manager.add_task(dream_task)
                logger.info(
                    "梦境维护任务已注册",
                    event_code="memory.dream.task_registered",
                    interval_seconds=dream_task.run_interval,
                )
            except Exception:
                logger.warning("梦境维护任务注册失败", event_code="memory.dream.task_register_failed", exc_info=True)

            # 启动双写一致性协调任务
            try:
                from src.memory.reconciliation import ReconciliationTask

                recon_task = ReconciliationTask(
                    store=store,
                    op_logger=memory_op_logger,
                    interval=120,
                )
                await async_task_manager.add_task(recon_task)
                logger.info(
                    "双写一致性协调任务已注册",
                    event_code="memory.reconciliation.task_registered",
                    interval_seconds=120,
                    write_op_log_available=memory_op_logger is not None,
                )
            except Exception:
                logger.warning(
                    "双写一致性协调任务注册失败",
                    event_code="memory.reconciliation.task_register_failed",
                    exc_info=True,
                )
        except Exception:
            logger.warning("记忆存储初始化失败，主系统继续启动", event_code="memory.store.init_failed", exc_info=True)

        # await asyncio.sleep(0.5) #防止logger输出飞了

        # 将bot.py中的chat_bot.message_process消息处理函数注册到api.py的消息处理基类中
        self.app.register_message_handler(chat_bot.message_process)
        self.app.register_custom_message_handler("message_id_echo", chat_bot.echo_message_process)

        # 触发 ON_START 事件
        from src.plugin_system.core.events_manager import events_manager
        from src.plugin_system.base.component_types import EventType

        await events_manager.handle_mai_events(event_type=EventType.ON_START)
        # logger.info("已触发 ON_START 事件")
        try:
            init_time = int(1000 * (time.time() - init_start_time))
            logger.info("组件初始化完成", event_code="system.components.initialized", duration_ms=init_time)
        except Exception:
            logger.exception("系统组件初始化失败", event_code="system.components.init_failed")
            raise

    async def schedule_tasks(self):
        """调度定时任务"""
        try:
            if self.setup_required:
                if self.webui_server:
                    logger.info("首次配置模式下启动 WebUI", event_code="system.setup.webui_only")
                    await self.webui_server.start()
                else:
                    logger.error(
                        "首次配置未完成且 WebUI 未启用，无法继续",
                        event_code="system.setup.webui_unavailable",
                    )
                return

            tasks = [
                get_emoji_manager().start_periodic_check_register(),
                self.app.run(),
                self.server.run(),
            ]

            # 如果 WebUI 服务器已初始化，添加到任务列表
            if self.webui_server:
                tasks.append(self.webui_server.start())

            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("调度任务已取消", event_code="system.schedule.cancelled")
            raise

    # async def forget_memory_task(self):
    #     """记忆遗忘任务"""
    #     while True:
    #         await asyncio.sleep(global_config.memory.forget_memory_interval)
    #         logger.info("[记忆遗忘] 开始遗忘记忆...")
    #         await self.hippocampus_manager.forget_memory(percentage=global_config.memory.memory_forget_percentage)  # type: ignore
    #         logger.info("[记忆遗忘] 记忆遗忘完成")


async def main():
    """主函数"""
    system = MainSystem()
    await system.initialize()
    await system.schedule_tasks()


if __name__ == "__main__":
    asyncio.run(main())
