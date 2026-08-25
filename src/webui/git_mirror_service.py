"""Git 镜像源服务 - 支持多镜像源、错误重试、Git 克隆和 Raw 文件获取"""

import asyncio
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from src.common.logger import get_logger, hash_id
from src.webui.error_utils import log_exception_type

logger = get_logger("webui.git_mirror")

MAX_RAW_FILE_BYTES = 5 * 1024 * 1024
"""Raw 文件最大响应体（5 MiB）。"""

_MAX_URL_LENGTH = 4096
_TRUE_VALUES = {"1", "true", "yes"}
_PROXY_ENV_VARS = {
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
}
_NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPATIBLE_PREFIX = ipaddress.ip_network("::/96")
_PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_BUILT_IN_PUBLIC_GIT_HOSTS = frozenset(
    {
        "github.com",
        "www.github.com",
        "raw.githubusercontent.com",
        "gh-proxy.org",
        "hk.gh-proxy.org",
        "cdn.gh-proxy.org",
        "edgeone.gh-proxy.org",
        "meyzh.github.io",
    }
)
_SCP_LIKE_URL = re.compile(
    r"^(?:(?P<user>[A-Za-z0-9._-]+)@)?(?P<host>\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+):(?P<path>[^\s\x00]+)$"
)
_GIT_OBJECT_ID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class RawFileTooLargeError(ValueError):
    """Raw 文件超过允许大小。"""


def _env_enabled(name: str) -> bool:
    return os.getenv(name, "").lower() in _TRUE_VALUES


def _validate_url_text(url: str) -> str:
    if not isinstance(url, str) or not url:
        raise ValueError("URL 不能为空")
    if url != url.strip() or len(url) > _MAX_URL_LENGTH:
        raise ValueError("URL 格式无效")
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError("URL 包含控制字符")
    return url


def _validate_network_url(url: str, *, clone: bool) -> str:
    url = _validate_url_text(url)
    purpose = "克隆" if clone else "Raw 文件"
    allowed_schemes = {"https", "ssh"} if clone else {"https"}
    if _env_enabled("MAIBOT_ALLOW_INSECURE_GIT_URLS"):
        allowed_schemes.update({"http", "git"} if clone else {"http"})

    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        _ = parsed.port  # 访问属性以触发非法或越界端口校验
    except ValueError as exc:
        raise ValueError(f"{purpose} URL 格式无效") from exc

    if scheme not in allowed_schemes:
        schemes = "、".join(sorted(allowed_schemes))
        raise ValueError(f"{purpose} URL 协议不安全，仅允许 {schemes}")
    if not hostname:
        raise ValueError(f"{purpose} URL 缺少主机名")
    _ascii_hostname(hostname)
    if parsed.fragment:
        raise ValueError(f"{purpose} URL 不允许包含片段")
    if scheme in {"http", "https"} and (parsed.username is not None or parsed.password is not None):
        raise ValueError(f"{purpose} URL 不允许内嵌认证凭据，请使用 Git 凭据管理器")
    if scheme == "ssh" and parsed.password is not None:
        raise ValueError("SSH URL 不允许内嵌密码，请使用 SSH 密钥")
    if clone and (not parsed.path or parsed.path == "/"):
        raise ValueError("克隆 URL 缺少仓库路径")
    return url


def validate_raw_url(url: str) -> str:
    """验证 Raw 文件 URL，只允许安全的 HTTP(S) 形式。"""
    return _validate_network_url(url, clone=False)


def validate_clone_url(url: str) -> str:
    """验证 Git 克隆 URL，拒绝 file/ext 等可执行本地协议。"""
    url = _validate_url_text(url)
    if "://" not in url:
        match = _SCP_LIKE_URL.fullmatch(url)
        if not match or not match.group("host") or not match.group("path"):
            raise ValueError("克隆 URL 格式无效")
        if any(delimiter in match.group("path") for delimiter in {"?", "#"}):
            raise ValueError("克隆 URL 格式无效")
        _ascii_hostname(match.group("host").strip("[]"))
        return url
    return _validate_network_url(url, clone=True)


def _host_and_port(url: str, *, clone: bool) -> tuple[str, int]:
    if clone and "://" not in url:
        match = _SCP_LIKE_URL.fullmatch(url)
        if not match:
            raise ValueError("克隆 URL 格式无效")
        return match.group("host").strip("[]"), 22

    parsed = urlsplit(url)
    default_port = 443 if parsed.scheme.lower() == "https" else 22 if parsed.scheme.lower() == "ssh" else 80
    return parsed.hostname or "", parsed.port or default_port


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判断地址是否适合对外下载，并识别常见 IPv4 嵌入式绕过。"""
    if not address.is_global:
        return False

    if isinstance(address, ipaddress.IPv6Address):
        embedded_addresses = [
            candidate for candidate in (address.ipv4_mapped, address.sixtofour) if candidate is not None
        ]
        if address.teredo is not None:
            embedded_addresses.extend(address.teredo)
        if address in _NAT64_WELL_KNOWN_PREFIX or address in _IPV4_COMPATIBLE_PREFIX:
            embedded_addresses.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
        if any(not embedded.is_global for embedded in embedded_addresses):
            return False
    return True


def _is_allowed_outbound_address(
    host: str,
    port: int,
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    if _is_public_address(address):
        return True
    normalized_host = _ascii_hostname(host).rstrip(".")
    return (
        port == 443
        and normalized_host in _BUILT_IN_PUBLIC_GIT_HOSTS
        and isinstance(address, ipaddress.IPv4Address)
        and address in _PROXY_FAKE_IP_NETWORK
    )


async def validate_outbound_host(host: str, port: int) -> tuple[str, ...]:
    """解析并校验出站地址，返回供请求固定使用的 IP，避免 DNS rebinding。"""
    allow_private = _env_enabled("MAIBOT_ALLOW_PRIVATE_GIT_URLS")

    try:
        literal_address = ipaddress.ip_address(host)
        addresses = [literal_address]
    except ValueError:
        try:
            address_info = await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                port,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise ValueError("无法解析 Git 服务主机名") from exc

        addresses = []
        for item in address_info:
            address = ipaddress.ip_address(item[4][0])
            if address not in addresses:
                addresses.append(address)

    if not addresses:
        raise ValueError("无法解析 Git 服务主机名")
    if not allow_private and any(not _is_allowed_outbound_address(host, port, address) for address in addresses):
        raise ValueError("Git URL 指向私有或本地地址；如需自托管服务，请显式设置 MAIBOT_ALLOW_PRIVATE_GIT_URLS=1")
    return tuple(str(address) for address in addresses)


def _ascii_hostname(host: str) -> str:
    """将主机名规范化为网络协议使用的 ASCII 形式。"""
    if not host or host.startswith("-") or "%" in host:
        raise ValueError("Git 服务主机名格式无效")

    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass

    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("Git 服务主机名格式无效") from exc

    host_without_root_dot = ascii_host[:-1] if ascii_host.endswith(".") else ascii_host
    labels = host_without_root_dot.split(".")
    if (
        not host_without_root_dot
        or len(ascii_host) > 253
        or any(len(label) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) for label in labels)
    ):
        raise ValueError("Git 服务主机名格式无效")
    return ascii_host


def _format_ip_for_url(address: str) -> str:
    return f"[{address}]" if ":" in address else address


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _build_pinned_http_request(url: str, address: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """将 HTTP(S) 请求改为直连已校验 IP，同时保留 Host 与 TLS SNI。"""
    parsed = urlsplit(url)
    hostname = _ascii_hostname(parsed.hostname or "")
    pinned_netloc = _format_ip_for_url(address)
    host_header = f"[{hostname}]" if ":" in hostname else hostname
    if parsed.port is not None:
        pinned_netloc = f"{pinned_netloc}:{parsed.port}"
        host_header = f"{host_header}:{parsed.port}"

    pinned_url = urlunsplit((parsed.scheme, pinned_netloc, parsed.path, parsed.query, ""))
    extensions = {"sni_hostname": hostname} if parsed.scheme.lower() == "https" else {}
    return pinned_url, {"Host": host_header}, extensions


def _git_curl_resolve(host: str, port: int, addresses: tuple[str, ...]) -> str:
    """构造 Git/libcurl 的固定 DNS 解析配置。"""
    formatted_addresses = ",".join(_format_ip_for_url(address) for address in addresses)
    return f"{_ascii_hostname(host)}:{port}:{formatted_addresses}"


def _pinned_ssh_command(host: str, address: str) -> str:
    """构造固定目标 IP 的 SSH 命令，并保留原主机名用于主机密钥校验。"""
    return shlex.join(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"HostName={address}",
            "-o",
            f"HostKeyAlias={_ascii_hostname(host)}",
            "-o",
            "CheckHostIP=no",
        ]
    )


def _remove_clone_target(target_path: Path) -> None:
    """清理失败或重试中的克隆目录，包括损坏的符号链接。"""
    if target_path.is_symlink() or target_path.is_file():
        target_path.unlink(missing_ok=True)
    elif target_path.exists():
        shutil.rmtree(target_path, ignore_errors=True)


def redact_url(url: str) -> str:
    """生成可安全记录和返回的 URL，移除凭据、查询参数和片段。"""
    if "://" not in url:
        return url
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        hostname = f"[{hostname}]" if ":" in hostname else hostname
        netloc = hostname
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        if parsed.scheme.lower() == "ssh" and parsed.username:
            netloc = f"{parsed.username}@{netloc}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except ValueError:
        return "<redacted-url>"


def parse_repository_url(url: str) -> tuple[str, str, bool, str]:
    """解析仓库 URL，返回 owner、repo、是否 GitHub 以及规范化 URL。"""
    normalized = validate_clone_url(url.rstrip("/"))
    if "://" not in normalized:
        match = _SCP_LIKE_URL.fullmatch(normalized)
        if not match:
            raise ValueError("仓库 URL 格式无效")
        hostname = match.group("host").strip("[]")
        path = match.group("path")
    else:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname or ""
        path = parsed.path

    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2 or any(part in {".", ".."} for part in parts[-2:]):
        raise ValueError("仓库 URL 缺少 owner/repo 路径")

    owner, repo = parts[-2], parts[-1]
    is_github = hostname.lower().rstrip(".") in {"github.com", "www.github.com"}
    return owner, repo, is_github, normalized


# 导入进度更新函数（避免循环导入）
_update_progress = None


def set_update_progress_callback(callback):
    """设置进度更新回调函数"""
    global _update_progress
    _update_progress = callback


class MirrorType(str, Enum):
    """镜像源类型"""

    GH_PROXY = "gh-proxy"  # gh-proxy 主节点
    HK_GH_PROXY = "hk-gh-proxy"  # gh-proxy 香港节点
    CDN_GH_PROXY = "cdn-gh-proxy"  # gh-proxy CDN 节点
    EDGEONE_GH_PROXY = "edgeone-gh-proxy"  # gh-proxy EdgeOne 节点
    MEYZH_GITHUB = "meyzh-github"  # Meyzh GitHub 镜像
    GITHUB = "github"  # GitHub 官方源（兜底）
    CUSTOM = "custom"  # 自定义镜像源


class GitMirrorConfig:
    """Git 镜像源配置管理"""

    # 配置文件路径
    CONFIG_FILE = Path("data/webui.json")

    # 默认镜像源配置
    DEFAULT_MIRRORS = [
        {
            "id": "gh-proxy",
            "name": "gh-proxy 镜像",
            "raw_prefix": "https://gh-proxy.org/https://raw.githubusercontent.com",
            "clone_prefix": "https://gh-proxy.org/https://github.com",
            "enabled": True,
            "priority": 1,
            "created_at": None,
        },
        {
            "id": "hk-gh-proxy",
            "name": "gh-proxy 香港节点",
            "raw_prefix": "https://hk.gh-proxy.org/https://raw.githubusercontent.com",
            "clone_prefix": "https://hk.gh-proxy.org/https://github.com",
            "enabled": True,
            "priority": 2,
            "created_at": None,
        },
        {
            "id": "cdn-gh-proxy",
            "name": "gh-proxy CDN 节点",
            "raw_prefix": "https://cdn.gh-proxy.org/https://raw.githubusercontent.com",
            "clone_prefix": "https://cdn.gh-proxy.org/https://github.com",
            "enabled": True,
            "priority": 3,
            "created_at": None,
        },
        {
            "id": "edgeone-gh-proxy",
            "name": "gh-proxy EdgeOne 节点",
            "raw_prefix": "https://edgeone.gh-proxy.org/https://raw.githubusercontent.com",
            "clone_prefix": "https://edgeone.gh-proxy.org/https://github.com",
            "enabled": True,
            "priority": 4,
            "created_at": None,
        },
        {
            "id": "meyzh-github",
            "name": "Meyzh GitHub 镜像",
            "raw_prefix": "https://meyzh.github.io/https://raw.githubusercontent.com",
            "clone_prefix": "https://meyzh.github.io/https://github.com",
            "enabled": True,
            "priority": 5,
            "created_at": None,
        },
        {
            "id": "github",
            "name": "GitHub 官方源（兜底）",
            "raw_prefix": "https://raw.githubusercontent.com",
            "clone_prefix": "https://github.com",
            "enabled": True,
            "priority": 999,
            "created_at": None,
        },
    ]

    def __init__(self):
        """初始化配置管理器"""
        self.config_file = self.CONFIG_FILE
        self.mirrors: List[Dict[str, Any]] = []
        self._load_config()

    def _load_config(self) -> None:
        """加载配置文件"""
        try:
            if self.config_file.exists():
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # 检查是否有镜像源配置
                if "git_mirrors" not in data or not data["git_mirrors"]:
                    logger.info("配置文件中未找到镜像源配置，使用默认配置")
                    self._init_default_mirrors()
                else:
                    self.mirrors = data["git_mirrors"]
                    logger.info("已加载镜像源配置", mirror_count=len(self.mirrors))
            else:
                logger.info("配置文件不存在，创建默认配置")
                self._init_default_mirrors()
        except Exception as e:
            log_exception_type(logger, "加载 Git 镜像配置失败", e)
            self._init_default_mirrors()

    def _init_default_mirrors(self) -> None:
        """初始化默认镜像源"""
        current_time = datetime.now().isoformat()
        self.mirrors = []

        for mirror in self.DEFAULT_MIRRORS:
            mirror_copy = mirror.copy()
            mirror_copy["created_at"] = current_time
            self.mirrors.append(mirror_copy)

        self._save_config()
        logger.info("已初始化默认镜像源", mirror_count=len(self.mirrors))

    def _save_config(self) -> None:
        """保存配置到文件"""
        try:
            # 确保目录存在
            self.config_file.parent.mkdir(parents=True, exist_ok=True)

            # 读取现有配置
            existing_data = {}
            if self.config_file.exists():
                with open(self.config_file, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)

            # 更新镜像源配置
            existing_data["git_mirrors"] = self.mirrors

            # 写入文件
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(existing_data, f, indent=2, ensure_ascii=False)

            logger.debug("Git 镜像配置已保存")
        except Exception as e:
            log_exception_type(logger, "保存 Git 镜像配置失败", e)

    def get_all_mirrors(self) -> List[Dict[str, Any]]:
        """获取所有镜像源"""
        return self.mirrors.copy()

    def get_enabled_mirrors(self) -> List[Dict[str, Any]]:
        """获取所有启用的镜像源，按优先级排序"""
        enabled = [m for m in self.mirrors if m.get("enabled", False)]
        return sorted(enabled, key=lambda x: x.get("priority", 999))

    def get_mirror_by_id(self, mirror_id: str) -> Optional[Dict[str, Any]]:
        """根据 ID 获取镜像源"""
        for mirror in self.mirrors:
            if mirror.get("id") == mirror_id:
                return mirror.copy()
        return None

    def add_mirror(
        self,
        mirror_id: str,
        name: str,
        raw_prefix: str,
        clone_prefix: str,
        enabled: bool = True,
        priority: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        添加新的镜像源

        Returns:
            添加的镜像源配置

        Raises:
            ValueError: 如果镜像源 ID 已存在
        """
        # 检查 ID 是否已存在
        if self.get_mirror_by_id(mirror_id):
            raise ValueError("镜像源 ID 已存在")

        # 如果未指定优先级，使用最大优先级 + 1
        if priority is None:
            max_priority = max((m.get("priority", 0) for m in self.mirrors), default=0)
            priority = max_priority + 1

        new_mirror = {
            "id": mirror_id,
            "name": name,
            "raw_prefix": raw_prefix,
            "clone_prefix": clone_prefix,
            "enabled": enabled,
            "priority": priority,
            "created_at": datetime.now().isoformat(),
        }

        self.mirrors.append(new_mirror)
        self._save_config()

        logger.info("已添加镜像源", mirror_id_hash=hash_id(mirror_id))
        return new_mirror.copy()

    def update_mirror(
        self,
        mirror_id: str,
        name: Optional[str] = None,
        raw_prefix: Optional[str] = None,
        clone_prefix: Optional[str] = None,
        enabled: Optional[bool] = None,
        priority: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        更新镜像源配置

        Returns:
            更新后的镜像源配置，如果不存在则返回 None
        """
        for mirror in self.mirrors:
            if mirror.get("id") == mirror_id:
                if name is not None:
                    mirror["name"] = name
                if raw_prefix is not None:
                    mirror["raw_prefix"] = raw_prefix
                if clone_prefix is not None:
                    mirror["clone_prefix"] = clone_prefix
                if enabled is not None:
                    mirror["enabled"] = enabled
                if priority is not None:
                    mirror["priority"] = priority

                mirror["updated_at"] = datetime.now().isoformat()
                self._save_config()

                logger.info("已更新镜像源", mirror_id_hash=hash_id(mirror_id))
                return mirror.copy()

        return None

    def delete_mirror(self, mirror_id: str) -> bool:
        """
        删除镜像源

        Returns:
            True 如果删除成功，False 如果镜像源不存在
        """
        for i, mirror in enumerate(self.mirrors):
            if mirror.get("id") == mirror_id:
                self.mirrors.pop(i)
                self._save_config()
                logger.info("已删除镜像源", mirror_id_hash=hash_id(mirror_id))
                return True

        return False

    def get_default_priority_list(self) -> List[str]:
        """获取默认优先级列表（仅启用的镜像源 ID）"""
        enabled = self.get_enabled_mirrors()
        return [m["id"] for m in enabled]


class GitMirrorService:
    """Git 镜像源服务"""

    def __init__(self, max_retries: int = 3, timeout: int = 30, config: Optional[GitMirrorConfig] = None):
        """
        初始化 Git 镜像源服务

        Args:
            max_retries: 最大重试次数
            timeout: 请求超时时间（秒）
            config: 镜像源配置管理器（可选，默认创建新实例）
        """
        self.max_retries = max_retries
        self.timeout = timeout
        self.config = config or GitMirrorConfig()
        logger.info("Git 镜像源服务初始化完成", enabled_mirror_count=len(self.config.get_enabled_mirrors()))

    def get_mirror_config(self) -> GitMirrorConfig:
        """获取镜像源配置管理器"""
        return self.config

    @staticmethod
    def check_git_installed() -> Dict[str, Any]:
        """
        检查本机是否安装了 Git

        Returns:
            Dict 包含:
                - installed: bool - 是否已安装 Git
                - version: str - Git 版本号（如果已安装）
                - path: str - Git 可执行文件路径（如果已安装）
                - error: str - 错误信息（如果未安装或检测失败）
        """
        import subprocess
        import shutil

        try:
            # 查找 git 可执行文件路径
            git_path = shutil.which("git")

            if not git_path:
                logger.warning("未找到 Git 可执行文件")
                return {"installed": False, "error": "系统中未找到 Git，请先安装 Git"}

            # 获取 Git 版本
            result = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=5)

            if result.returncode == 0:
                version = result.stdout.strip()
                logger.info("检测到 Git")
                return {"installed": True, "version": version, "path": git_path}
            else:
                logger.warning("Git 命令执行失败", return_code=result.returncode)
                return {"installed": False, "error": "Git 命令执行失败"}

        except subprocess.TimeoutExpired:
            logger.error("Git 版本检测超时")
            return {"installed": False, "error": "Git 版本检测超时"}
        except Exception as e:
            log_exception_type(logger, "检测 Git 时发生错误", e)
            return {"installed": False, "error": "检测 Git 时发生错误"}

    async def fetch_raw_file(
        self,
        owner: str,
        repo: str,
        branch: str,
        file_path: str,
        mirror_id: Optional[str] = None,
        custom_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        获取 GitHub 仓库的 Raw 文件内容

        Args:
            owner: 仓库所有者
            repo: 仓库名称
            branch: 分支名称
            file_path: 文件路径
            mirror_id: 指定的镜像源 ID
            custom_url: 自定义完整 URL（如果提供，将忽略其他参数）

        Returns:
            Dict 包含:
                - success: bool - 是否成功
                - data: str - 文件内容（成功时）
                - error: str - 错误信息（失败时）
                - mirror_used: str - 使用的镜像源
                - attempts: int - 尝试次数
        """
        logger.info(
            "开始获取 Raw 文件",
            repository_hash=hash_id(f"{owner}/{repo}"),
            branch_hash=hash_id(branch),
            file_path_hash=hash_id(file_path),
        )

        if custom_url:
            # 使用自定义 URL
            return await self._fetch_with_url(custom_url, "custom")

        # 确定要使用的镜像源列表
        if mirror_id:
            # 使用指定的镜像源
            mirror = self.config.get_mirror_by_id(mirror_id)
            if not mirror:
                return {"success": False, "error": "未找到指定镜像源", "mirror_used": None, "attempts": 0}
            mirrors_to_try = [mirror]
        else:
            # 使用所有启用的镜像源
            mirrors_to_try = self.config.get_enabled_mirrors()

        total_mirrors = len(mirrors_to_try)

        # 依次尝试每个镜像源
        for index, mirror in enumerate(mirrors_to_try, 1):
            # 推送进度：正在尝试第 N 个镜像源
            if _update_progress:
                try:
                    progress = 30 + int((index - 1) / total_mirrors * 40)  # 30% - 70%
                    await _update_progress(
                        stage="loading",
                        progress=progress,
                        message=f"正在尝试镜像源 {index}/{total_mirrors}",
                        total_plugins=0,
                        loaded_plugins=0,
                    )
                except Exception as e:
                    log_exception_type(logger, "推送 Git 镜像进度失败", e, level="warning")

            result = await self._fetch_raw_from_mirror(owner, repo, branch, file_path, mirror)

            if result["success"]:
                # 成功，推送进度
                if _update_progress:
                    try:
                        await _update_progress(
                            stage="loading",
                            progress=70,
                            message="成功获取数据",
                            total_plugins=0,
                            loaded_plugins=0,
                        )
                    except Exception as e:
                        log_exception_type(logger, "推送 Git 镜像进度失败", e, level="warning")
                return result

            # 失败，记录日志并推送失败信息
            logger.warning("镜像源获取 Raw 文件失败", mirror_id_hash=hash_id(mirror.get("id")))

            if _update_progress and index < total_mirrors:
                try:
                    await _update_progress(
                        stage="loading",
                        progress=30 + int(index / total_mirrors * 40),
                        message="当前镜像源失败，尝试下一个...",
                        total_plugins=0,
                        loaded_plugins=0,
                    )
                except Exception as e:
                    log_exception_type(logger, "推送 Git 镜像进度失败", e, level="warning")

        # 所有镜像源都失败
        return {"success": False, "error": "所有镜像源均失败", "mirror_used": None, "attempts": len(mirrors_to_try)}

    async def _fetch_raw_from_mirror(
        self, owner: str, repo: str, branch: str, file_path: str, mirror: Dict[str, Any]
    ) -> Dict[str, Any]:
        """从指定镜像源获取文件"""
        # 构建 URL
        raw_prefix = mirror["raw_prefix"]
        url = f"{raw_prefix}/{owner}/{repo}/{branch}/{file_path}"

        return await self._fetch_with_url(url, mirror["id"])

    async def _fetch_with_url(self, url: str, mirror_type: str) -> Dict[str, Any]:
        """使用指定 URL 获取文件，支持重试"""
        safe_url = "<redacted-url>"
        try:
            url = validate_raw_url(url)
            safe_url = redact_url(url)
            host, port = _host_and_port(url, clone=False)
            addresses = await validate_outbound_host(host, port)
        except ValueError as e:
            log_exception_type(logger, "拒绝不安全的 Raw 文件 URL", e, level="warning")
            return {
                "success": False,
                "error": "Raw 文件 URL 不安全或无效",
                "mirror_used": mirror_type,
                "attempts": 0,
                "url": safe_url,
            }

        attempts = 0
        last_error = None

        for attempt in range(self.max_retries):
            attempts += 1
            try:
                logger.debug(
                    "尝试获取 Raw 文件",
                    attempt=attempt + 1,
                    max_retries=self.max_retries,
                    url_hash=hash_id(safe_url),
                )
                pinned_url, request_headers, request_extensions = _build_pinned_http_request(
                    url, addresses[attempt % len(addresses)]
                )
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    follow_redirects=False,
                    trust_env=_env_enabled("MAIBOT_ALLOW_GIT_PROXY"),
                ) as client:
                    async with client.stream(
                        "GET",
                        pinned_url,
                        headers=request_headers,
                        extensions=request_extensions,
                    ) as response:
                        response.raise_for_status()

                        content_length = response.headers.get("content-length")
                        if content_length:
                            try:
                                if int(content_length) > MAX_RAW_FILE_BYTES:
                                    raise RawFileTooLargeError("Raw 文件过大，最大允许 5 MiB")
                            except ValueError as e:
                                if isinstance(e, RawFileTooLargeError):
                                    raise

                        chunks = []
                        total_bytes = 0
                        async for chunk in response.aiter_bytes():
                            total_bytes += len(chunk)
                            if total_bytes > MAX_RAW_FILE_BYTES:
                                raise RawFileTooLargeError("Raw 文件过大，最大允许 5 MiB")
                            chunks.append(chunk)

                        encoding = response.encoding or "utf-8"
                        data = b"".join(chunks).decode(encoding, errors="replace")

                    logger.info("成功获取 Raw 文件", url_hash=hash_id(safe_url), attempts=attempts)
                    return {
                        "success": True,
                        "data": data,
                        "mirror_used": mirror_type,
                        "attempts": attempts,
                        "url": safe_url,
                    }
            except RawFileTooLargeError:
                last_error = "Raw 文件响应超过大小限制"
                logger.warning(
                    "Raw 文件响应超过大小限制",
                    url_hash=hash_id(safe_url),
                    max_bytes=MAX_RAW_FILE_BYTES,
                )
                break
            except httpx.HTTPStatusError as e:
                last_error = f"HTTP {e.response.status_code}"
                logger.warning(
                    "Raw 文件 HTTP 请求失败",
                    attempt=attempt + 1,
                    max_retries=self.max_retries,
                    status_code=e.response.status_code,
                )
            except httpx.TimeoutException:
                last_error = "请求超时"
                logger.warning("Raw 文件请求超时", attempt=attempt + 1, max_retries=self.max_retries)
            except httpx.RequestError as e:
                last_error = f"网络请求失败: {type(e).__name__}"
                logger.warning(
                    "Raw 文件网络请求失败",
                    attempt=attempt + 1,
                    max_retries=self.max_retries,
                    error_type=type(e).__name__,
                )
            except Exception as e:
                last_error = f"未知错误: {type(e).__name__}"
                log_exception_type(
                    logger,
                    "Raw 文件获取失败",
                    e,
                    attempt=attempt + 1,
                    max_retries=self.max_retries,
                )

        return {
            "success": False,
            "error": last_error,
            "mirror_used": mirror_type,
            "attempts": attempts,
            "url": safe_url,
        }

    async def clone_repository(
        self,
        owner: str,
        repo: str,
        target_path: Path,
        branch: Optional[str] = None,
        mirror_id: Optional[str] = None,
        custom_url: Optional[str] = None,
        depth: Optional[int] = None,
        expected_commit: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        克隆 GitHub 仓库

        Args:
            owner: 仓库所有者
            repo: 仓库名称
            target_path: 目标路径
            branch: 分支名称（可选）
            mirror_id: 指定的镜像源 ID
            custom_url: 自定义克隆 URL
            depth: 克隆深度（浅克隆）

        Returns:
            Dict 包含:
                - success: bool - 是否成功
                - path: str - 克隆路径（成功时）
                - error: str - 错误信息（失败时）
                - mirror_used: str - 使用的镜像源
                - attempts: int - 尝试次数
        """
        logger.info(
            "开始克隆仓库",
            repository_hash=hash_id(f"{owner}/{repo}"),
            target_path_hash=hash_id(target_path),
        )

        if custom_url:
            # 使用自定义 URL
            return await self._clone_with_url(
                custom_url,
                target_path,
                branch,
                depth,
                "custom",
                expected_commit=expected_commit,
            )

        # 确定要使用的镜像源列表
        if mirror_id:
            # 使用指定的镜像源
            mirror = self.config.get_mirror_by_id(mirror_id)
            if not mirror:
                return {"success": False, "error": "未找到指定镜像源", "mirror_used": None, "attempts": 0}
            mirrors_to_try = [mirror]
        else:
            # 使用所有启用的镜像源
            mirrors_to_try = self.config.get_enabled_mirrors()

        # 依次尝试每个镜像源
        for mirror in mirrors_to_try:
            result = await self._clone_from_mirror(
                owner,
                repo,
                target_path,
                branch,
                depth,
                mirror,
                expected_commit=expected_commit,
            )
            if result["success"]:
                return result
            logger.warning("镜像源克隆仓库失败", mirror_id_hash=hash_id(mirror.get("id")))

        # 所有镜像源都失败
        return {"success": False, "error": "所有镜像源克隆均失败", "mirror_used": None, "attempts": len(mirrors_to_try)}

    async def _clone_from_mirror(
        self,
        owner: str,
        repo: str,
        target_path: Path,
        branch: Optional[str],
        depth: Optional[int],
        mirror: Dict[str, Any],
        expected_commit: Optional[str] = None,
    ) -> Dict[str, Any]:
        """从指定镜像源克隆仓库"""
        # 构建克隆 URL
        clone_prefix = mirror["clone_prefix"]
        url = f"{clone_prefix}/{owner}/{repo}.git"

        return await self._clone_with_url(
            url,
            target_path,
            branch,
            depth,
            mirror["id"],
            expected_commit=expected_commit,
        )

    async def _clone_with_url(
        self,
        url: str,
        target_path: Path,
        branch: Optional[str],
        depth: Optional[int],
        mirror_type: str,
        *,
        expected_commit: Optional[str] = None,
    ) -> Dict[str, Any]:
        """使用指定 URL 克隆仓库，支持重试"""
        safe_url = "<redacted-url>"
        try:
            url = validate_clone_url(url)
            safe_url = redact_url(url)
            host, port = _host_and_port(url, clone=True)
            addresses = await validate_outbound_host(host, port)
            if branch and (
                len(branch) > 255
                or branch.startswith("-")
                or any(ord(char) < 32 or ord(char) == 127 for char in branch)
            ):
                raise ValueError("分支名称格式无效")
            if depth is not None and not 1 <= depth <= 1000:
                raise ValueError("克隆深度必须在 1 到 1000 之间")
            if expected_commit is not None and not _GIT_OBJECT_ID_RE.fullmatch(expected_commit):
                raise ValueError("预期提交格式无效")
        except ValueError as e:
            log_exception_type(logger, "拒绝不安全的 Git 克隆请求", e, level="warning")
            return {
                "success": False,
                "error": "Git 克隆 URL 不安全或无效",
                "mirror_used": mirror_type,
                "attempts": 0,
                "url": safe_url,
            }

        attempts = 0
        last_error = None

        for attempt in range(self.max_retries):
            attempts += 1

            try:
                # 确保目标路径不存在
                if target_path.exists():
                    logger.warning("克隆目标路径已存在，准备清理", target_path_hash=hash_id(target_path))
                    _remove_clone_target(target_path)

                # 构建 git clone 命令
                cmd = [
                    "git",
                    "-c",
                    "protocol.file.allow=never",
                    "-c",
                    "protocol.ext.allow=never",
                ]

                is_scp_like = "://" not in url
                scheme = "ssh" if is_scp_like else urlsplit(url).scheme.lower()
                allow_git_proxy = _env_enabled("MAIBOT_ALLOW_GIT_PROXY")
                if scheme in {"http", "https"}:
                    cmd.extend(["-c", "http.followRedirects=false"])
                    if not allow_git_proxy:
                        cmd.extend(["-c", "http.proxy="])
                    if not _is_ip_literal(host):
                        cmd.extend(
                            [
                                "-c",
                                "http.curloptResolve=",
                                "-c",
                                f"http.curloptResolve={_git_curl_resolve(host, port, addresses)}",
                            ]
                        )

                cmd.append("clone")

                # 添加分支参数
                if branch:
                    cmd.extend(["-b", branch])

                # 添加深度参数（浅克隆）
                if depth:
                    cmd.extend(["--depth", str(depth)])

                # 添加 URL 和目标路径
                cmd.extend(["--", url, str(target_path)])

                logger.info(
                    "尝试克隆仓库",
                    attempt=attempt + 1,
                    max_retries=self.max_retries,
                    scheme=scheme,
                    url_hash=hash_id(safe_url),
                    target_path_hash=hash_id(target_path),
                )

                # 推送进度
                if _update_progress:
                    try:
                        await _update_progress(
                            stage="loading",
                            progress=20 + attempt * 10,
                            message=f"正在克隆仓库 (尝试 {attempt + 1}/{self.max_retries})...",
                            operation="install",
                        )
                    except Exception as e:
                        log_exception_type(logger, "推送 Git 克隆进度失败", e, level="warning")

                # 执行 git clone（在线程池中运行以避免阻塞）
                loop = asyncio.get_event_loop()

                pinned_address = addresses[attempt % len(addresses)]

                def run_git_clone(
                    clone_cmd=cmd,
                    clone_scheme=scheme,
                    clone_host=host,
                    ssh_address=pinned_address,
                    proxy_allowed=allow_git_proxy,
                ):
                    clone_env = os.environ.copy()
                    clone_env["GIT_TERMINAL_PROMPT"] = "0"
                    if not proxy_allowed:
                        for variable in _PROXY_ENV_VARS:
                            clone_env.pop(variable, None)
                        clone_env["NO_PROXY"] = "*"
                        clone_env["no_proxy"] = "*"
                    if clone_scheme == "ssh":
                        clone_env["GIT_SSH_COMMAND"] = _pinned_ssh_command(clone_host, ssh_address)
                    return subprocess.run(
                        clone_cmd,
                        capture_output=True,
                        text=True,
                        timeout=300,  # 5分钟超时
                        env=clone_env,
                    )

                process = await loop.run_in_executor(None, run_git_clone)

                if process.returncode == 0:

                    def resolve_git_head():
                        return subprocess.run(
                            ["git", "-C", str(target_path), "rev-parse", "--verify", "HEAD"],
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )

                    head_process = await loop.run_in_executor(None, resolve_git_head)
                    head_commit = head_process.stdout.strip().lower() if head_process.returncode == 0 else ""
                    if not _GIT_OBJECT_ID_RE.fullmatch(head_commit):
                        last_error = "无法确认仓库提交"
                        logger.warning(
                            "无法确认克隆仓库的 HEAD",
                            url_hash=hash_id(safe_url),
                            return_code=head_process.returncode,
                        )
                        _remove_clone_target(target_path)
                        continue
                    if expected_commit is not None and head_commit != expected_commit:
                        last_error = "仓库提交与审核记录不一致"
                        logger.warning(
                            "克隆仓库提交与审核记录不一致",
                            url_hash=hash_id(safe_url),
                            expected_commit_hash=hash_id(expected_commit),
                            actual_commit_hash=hash_id(head_commit),
                        )
                        _remove_clone_target(target_path)
                        break
                    logger.info(
                        "成功克隆仓库",
                        url_hash=hash_id(safe_url),
                        target_path_hash=hash_id(target_path),
                        attempts=attempts,
                    )
                    return {
                        "success": True,
                        "path": str(target_path),
                        "mirror_used": mirror_type,
                        "attempts": attempts,
                        "url": safe_url,
                        "branch": branch or "default",
                        "commit": head_commit,
                    }
                else:
                    last_error = "Git 克隆失败"
                    logger.warning(
                        "Git 克隆命令执行失败",
                        attempt=attempt + 1,
                        max_retries=self.max_retries,
                        return_code=process.returncode,
                    )
                    _remove_clone_target(target_path)

            except subprocess.TimeoutExpired:
                last_error = "克隆超时（超过 5 分钟）"
                logger.warning("Git 克隆超时", attempt=attempt + 1, max_retries=self.max_retries)

                # 清理可能的部分克隆
                _remove_clone_target(target_path)

            except FileNotFoundError:
                last_error = "Git 未安装或不在 PATH 中"
                logger.error("Git 未安装或不在 PATH 中")
                break  # Git 不存在，不需要重试

            except Exception as e:
                last_error = f"未知错误: {type(e).__name__}"
                log_exception_type(
                    logger,
                    "Git 仓库克隆失败",
                    e,
                    attempt=attempt + 1,
                    max_retries=self.max_retries,
                )

                # 清理可能的部分克隆
                _remove_clone_target(target_path)

        _remove_clone_target(target_path)
        return {
            "success": False,
            "error": last_error,
            "mirror_used": mirror_type,
            "attempts": attempts,
            "url": safe_url,
        }


# 全局服务实例
_git_mirror_service: Optional[GitMirrorService] = None


def get_git_mirror_service() -> GitMirrorService:
    """获取 Git 镜像源服务实例（单例）"""
    global _git_mirror_service
    if _git_mirror_service is None:
        _git_mirror_service = GitMirrorService()
    return _git_mirror_service
