import json
import os
import tempfile
import uuid

from src.common.logger import get_logger

LOCAL_STORE_FILE_PATH = "data/local_store.json"

logger = get_logger("local_storage")


class LocalStoreManager:
    file_path: str
    """本地存储路径"""

    store: dict[str, str | list | dict | int | float | bool]
    """本地存储数据"""

    def __init__(self, local_store_path: str | None = None):
        self.file_path = local_store_path or LOCAL_STORE_FILE_PATH
        self.store = {}
        self.load_local_store()

    def __getitem__(self, item: str) -> str | list | dict | int | float | bool | None:
        """获取本地存储数据"""
        return self.store.get(item)

    def __setitem__(self, key: str, value: str | list | dict | int | float | bool):
        """设置本地存储数据"""
        previous = self.store.copy()
        self.store[key] = value
        try:
            self.save_local_store()
        except Exception:
            self.store = previous
            raise

    def __delitem__(self, key: str):
        """删除本地存储数据"""
        if key in self.store:
            previous = self.store.copy()
            del self.store[key]
            try:
                self.save_local_store()
            except Exception:
                self.store = previous
                raise
        else:
            logger.warning("本地存储键不存在，删除已跳过", event_code="local_store.delete_missing_key", key=key)

    def __contains__(self, item: str) -> bool:
        """检查本地存储数据是否存在"""
        return item in self.store

    def load_local_store(self):
        """加载本地存储数据"""
        if os.path.exists(self.file_path):
            # 存在本地存储文件，加载数据
            logger.info("本地存储开始加载", event_code="local_store.load_started", path=self.file_path)
            logger.debug("本地存储文件读取", event_code="local_store.file_read", path=self.file_path)
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    self.store = json.load(f)
                    if not isinstance(self.store, dict):
                        raise ValueError("本地存储根节点必须为对象")
                    logger.info("本地存储加载完成", event_code="local_store.loaded", path=self.file_path)
            except (json.JSONDecodeError, ValueError):
                backup_path = f"{self.file_path}.corrupt.{uuid.uuid4().hex}"
                os.replace(self.file_path, backup_path)
                logger.warning(
                    "本地存储 JSON 无效，已保留损坏文件并重建",
                    event_code="local_store.invalid_json_rebuild",
                    path=self.file_path,
                    backup_path=backup_path,
                )
                self.store = {}
                self.save_local_store()
                logger.info("本地存储重建完成", event_code="local_store.rebuilt", path=self.file_path)
        else:
            # 不存在本地存储文件，创建新的目录和文件
            logger.warning("本地存储文件不存在，开始创建", event_code="local_store.missing_create", path=self.file_path)
            store_dir = os.path.dirname(self.file_path)
            if store_dir:
                os.makedirs(store_dir, exist_ok=True)
            self.save_local_store()
            logger.info("本地存储文件创建完成", event_code="local_store.created", path=self.file_path)

    def save_local_store(self):
        """保存本地存储数据"""
        logger.debug("本地存储开始保存", event_code="local_store.save_started", path=self.file_path)
        output = json.dumps(self.store, ensure_ascii=False, indent=4)
        directory = os.path.dirname(os.path.abspath(self.file_path))
        descriptor, temporary_path = tempfile.mkstemp(dir=directory, prefix=".local-store-", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(output)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.file_path)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)


local_storage = LocalStoreManager("data/local_store.json")  # 全局单例化
