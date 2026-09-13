import asyncio
import os
import time
import platform
import shutil
import sys
import subprocess
from dotenv import load_dotenv
from pathlib import Path
from rich.traceback import install
from src.common.logger import initialize_logging, get_logger, shutdown_logging
from src.update_system.runner import UPDATE_EXIT_CODE, PendingUpdateStore, apply_pending_update

script_dir = os.path.dirname(os.path.abspath(__file__))
logger = get_logger("main")
confirm_logger = get_logger("confirm")
RESTART_EXIT_CODE = 42


def initialize_runtime():
    """仅在进程入口加载环境；服务注入的变量优先于 .env。"""
    os.chdir(script_dir)
    env_path = Path(__file__).parent / ".env"
    template_env_path = Path(__file__).parent / "template" / "template.env"
    if not env_path.exists():
        if not template_env_path.exists():
            raise FileNotFoundError(".env 文件不存在，请创建并配置所需的环境变量")
        shutil.copyfile(template_env_path, env_path)
        print("未找到.env，已从 template/template.env 自动创建", flush=True)
    load_dotenv(str(env_path), override=False)
    initialize_logging(verbose=os.environ.get("MAIBOT_WORKER_PROCESS") == "1")
    install(extra_lines=3)
    logger.info("工作目录已设置", event_code="app.workdir.set", workdir=script_dir)


def run_runner_process():
    """
    Runner 进程逻辑：作为守护进程运行，负责启动和监控 Worker 进程。
    处理重启请求 (退出码 42) 和 Ctrl+C 信号。
    """
    script_file = os.path.join(script_dir, "bot.py")
    python_executable = sys.executable

    # 设置环境变量，标记子进程为 Worker 进程
    env = os.environ.copy()
    env["MAIBOT_WORKER_PROCESS"] = "1"

    crash_delay = 1
    while True:
        # 启动子进程 (Worker)
        # 使用 sys.executable 确保使用相同的 Python 解释器
        cmd = [python_executable, script_file] + sys.argv[1:]
        logger.info(
            "Worker 进程启动",
            event_code="runner.worker.start",
            script_file=script_file,
            python_executable=python_executable,
            argv_count=len(sys.argv),
        )

        started_at = time.monotonic()
        process = subprocess.Popen(cmd, env=env)

        try:
            # 等待子进程结束
            return_code = process.wait()

            if return_code == RESTART_EXIT_CODE:
                logger.info("Worker 请求重启", event_code="runner.worker.restart_requested", exit_code=return_code)
                time.sleep(1)  # 稍作等待
                continue
            elif return_code == UPDATE_EXIT_CODE:
                logger.info("Worker 请求执行更新", event_code="runner.worker.update_requested", exit_code=return_code)
                try:
                    update_result = apply_pending_update(
                        project_root=Path(script_dir),
                        store=PendingUpdateStore(Path(script_dir) / "data" / "update"),
                    )
                except Exception:
                    logger.exception("Runner 更新执行异常，将尝试启动当前工作区", event_code="runner.update.crashed")
                    time.sleep(1)
                    continue
                if update_result.success:
                    logger.info(
                        "Runner 更新执行成功，重新载入主程序",
                        event_code="runner.update.completed",
                        target_revision=update_result.target_revision,
                    )
                    try:
                        shutdown_logging()
                        os.execv(python_executable, cmd)
                    except OSError:
                        logger.exception(
                            "Runner 重新执行失败，将直接启动 Worker", event_code="runner.update.reexec_failed"
                        )
                else:
                    logger.error(
                        "Runner 更新执行失败，将尝试启动当前工作区",
                        event_code="runner.update.failed",
                        result_code=update_result.code,
                    )
                time.sleep(1)
                continue
            elif return_code not in {0, -2, -15, 130, 143}:
                if time.monotonic() - started_at >= 60:
                    crash_delay = 1
                logger.error(
                    "Worker 异常退出，将重新启动",
                    event_code="runner.worker.crashed",
                    exit_code=return_code,
                    retry_delay_seconds=crash_delay,
                )
                time.sleep(crash_delay)
                crash_delay = min(crash_delay * 2, 60)
                continue
            else:
                logger.info("Worker 进程退出", event_code="runner.worker.exited", exit_code=return_code)
                sys.exit(return_code)

        except KeyboardInterrupt:
            # 向子进程发送终止信号
            if process.poll() is None:
                # 在 Windows 上，Ctrl+C 通常已经发送给了子进程（如果它们共享控制台）
                # 但为了保险，我们可以尝试 terminate
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning("Worker 进程停止超时，执行强制终止", event_code="runner.worker.kill_timeout")
                    process.kill()
            sys.exit(0)


def print_opensource_notice():
    """打印开源项目提示，防止倒卖"""
    from colorama import init, Fore, Style

    init()

    notice_lines = [
        "",
        f"{Fore.CYAN}{'═' * 70}{Style.RESET_ALL}",
        f"{Fore.GREEN}  ★ RiyaBot / 璃夜Bot - 开源 AI 聊天机器人 ★{Style.RESET_ALL}",
        f"{Fore.CYAN}{'─' * 70}{Style.RESET_ALL}",
        f"{Fore.YELLOW}  本项目是完全免费的开源软件，基于 GPL-3.0 协议发布{Style.RESET_ALL}",
        f"{Fore.WHITE}  如果有人向你「出售本软件」，你被骗了！{Style.RESET_ALL}",
        "",
        f"{Fore.WHITE}  官方仓库: {Fore.BLUE}https://github.com/hsd221/riyabot {Style.RESET_ALL}",
        f"{Fore.WHITE}  项目文档: {Fore.BLUE}https://github.com/hsd221/riyabot#readme {Style.RESET_ALL}",
        f"{Fore.CYAN}{'─' * 70}{Style.RESET_ALL}",
        f"{Fore.RED}  ⚠ 将本软件作为「商品」倒卖、隐瞒开源性质均违反协议！{Style.RESET_ALL}",
        f"{Fore.CYAN}{'═' * 70}{Style.RESET_ALL}",
        "",
    ]

    for line in notice_lines:
        print(line)


def easter_egg():
    # 彩蛋
    from colorama import init, Fore

    init()
    text = "多年以后，面对AI行刑队，张三将会回想起他2023年在会议上讨论人工智能的那个下午"
    rainbow_colors = [Fore.RED, Fore.YELLOW, Fore.GREEN, Fore.CYAN, Fore.BLUE, Fore.MAGENTA]
    rainbow_text = ""
    for i, char in enumerate(text):
        rainbow_text += rainbow_colors[i % len(rainbow_colors)] + char
    print(rainbow_text)


async def graceful_shutdown():  # sourcery skip: use-named-expression
    try:
        logger.info("应用开始关闭", event_code="app.shutdown.started")

        # 关闭 WebUI 服务器
        try:
            from src.webui.webui_server import get_webui_server

            webui_server = get_webui_server()
            if webui_server:
                await webui_server.shutdown()
        except Exception as e:
            logger.warning("WebUI 服务器关闭失败，继续关闭流程", event_code="app.shutdown.webui_failed", error=str(e))

        try:
            from src.plugin_system.core.events_manager import events_manager
            from src.plugin_system.base.component_types import EventType

            await events_manager.handle_mai_events(event_type=EventType.ON_STOP)
        except Exception:
            logger.exception("插件关闭事件失败，继续清理任务", event_code="app.shutdown.plugins_failed")

        try:
            from src.manager.async_task_manager import async_task_manager

            await async_task_manager.stop_and_wait_all_tasks()
        except Exception:
            logger.exception("异步任务管理器关闭失败，继续清理任务", event_code="app.shutdown.manager_failed")

        # 获取所有剩余任务，排除当前任务
        remaining_tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

        if remaining_tasks:
            logger.info("剩余异步任务开始取消", event_code="app.shutdown.cancel_tasks", count=len(remaining_tasks))

            # 取消所有剩余任务
            for task in remaining_tasks:
                if not task.done():
                    task.cancel()

            # 等待所有任务完成，设置超时
            try:
                await asyncio.wait_for(asyncio.gather(*remaining_tasks, return_exceptions=True), timeout=15.0)
                logger.info("剩余异步任务已取消", event_code="app.shutdown.tasks_cancelled", count=len(remaining_tasks))
            except asyncio.TimeoutError:
                logger.warning(
                    "等待异步任务取消超时", event_code="app.shutdown.task_cancel_timeout", timeout_seconds=15.0
                )
            except Exception:
                logger.exception("等待异步任务取消失败", event_code="app.shutdown.task_cancel_failed")

        logger.info("应用关闭完成", event_code="app.shutdown.completed")

    except Exception:
        logger.exception("应用关闭失败", event_code="app.shutdown.failed")


def check_eula() -> bool:
    """检查EULA和隐私条款确认状态"""
    from src.common.agreement import get_agreement_status

    agreement_status = get_agreement_status(include_content=False)
    pending_agreements = [document.title for document in agreement_status.values() if not document.confirmed]

    if not pending_agreements:
        return True

    confirm_logger.warning(
        "EULA或隐私条款尚未确认，已交由 WebUI 首次配置向导处理",
        event_code="agreement.webui_confirmation_required",
        pending=pending_agreements,
        eula_hash=agreement_status["eula"].hash,
        privacy_hash=agreement_status["privacy"].hash,
    )
    return False


def raw_main():
    # 利用 TZ 环境变量设定程序工作的时区
    if platform.system().lower() != "windows":
        time.tzset()  # type: ignore

    # 打印开源提示（防止倒卖）
    print_opensource_notice()

    agreements_confirmed = check_eula()
    logger.info("协议确认检查完成", event_code="agreement.check_completed", confirmed=agreements_confirmed)

    easter_egg()

    from src.main import MainSystem

    return MainSystem()


def run_worker_process() -> int:
    """运行 Worker，并在正常退出、异常和受控重启时清理已启动的组件。"""
    exit_code = 0
    loop = None
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        main_system = raw_main()
        from src.common.logger import initialize_ws_handler

        initialize_ws_handler(loop)
        loop.run_until_complete(main_system.initialize())
        loop.run_until_complete(main_system.schedule_tasks())
    except KeyboardInterrupt:
        logger.warning("收到中断信号，开始关闭流程", event_code="app.interrupt_received")
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
        if exit_code in {RESTART_EXIT_CODE, UPDATE_EXIT_CODE}:
            logger.info("收到受控退出码", event_code="app.controlled_exit_requested", exit_code=exit_code)
    except Exception:
        logger.exception("主程序异常退出", event_code="app.main_failed")
        exit_code = 1
    finally:
        if loop is not None and not loop.is_closed():
            try:
                loop.run_until_complete(graceful_shutdown())
                loop.run_until_complete(loop.shutdown_asyncgens())
            except (Exception, KeyboardInterrupt):
                logger.exception("关闭流程失败", event_code="app.shutdown.failed")
            finally:
                loop.close()
                asyncio.set_event_loop(None)
    return exit_code


if __name__ == "__main__":
    initialize_runtime()
    if os.environ.get("MAIBOT_WORKER_PROCESS") != "1":
        run_runner_process()
    else:
        exit_code = run_worker_process()
        shutdown_logging()
        # Worker 可能仍有第三方库线程；清理完成后保留硬退出以避免挂起。
        print("[主程序] 准备退出...", flush=True)
        sys.stderr.flush()
        os._exit(exit_code)
