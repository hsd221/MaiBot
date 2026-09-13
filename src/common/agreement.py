"""EULA 和隐私条款确认状态管理。"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from src.common.logger import get_logger

logger = get_logger("agreement")

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class AgreementDocumentStatus:
    """单个协议文件的状态。"""

    key: str
    title: str
    file_name: str
    confirmation_file_name: str
    env_var: str
    hash: str
    confirmed: bool
    file_confirmed: bool
    environment_confirmed: bool
    content: str = ""


AGREEMENT_FILES = {
    "eula": {
        "title": "最终用户许可协议",
        "file_name": "EULA.md",
        "confirmation_file_name": "eula.confirmed",
        "env_var": "EULA_AGREE",
    },
    "privacy": {
        "title": "隐私条款",
        "file_name": "PRIVACY.md",
        "confirmation_file_name": "privacy.confirmed",
        "env_var": "PRIVACY_AGREE",
    },
}


def _read_text(path: Path, file_type: str) -> str:
    if not path.exists():
        logger.error("协议文件不存在", event_code="agreement.file_missing", file_type=file_type, path=str(path))
        raise FileNotFoundError(f"{file_type} 文件不存在")
    return path.read_text(encoding="utf-8")


def calculate_file_hash(file_path: Path, file_type: str) -> str:
    """计算协议文件的 MD5 哈希值。"""
    content = _read_text(file_path, file_type)
    return hashlib.md5(content.encode("utf-8"), usedforsecurity=False).hexdigest()


def _check_confirmation(file_hash: str, confirm_file: Path, env_var: str) -> tuple[bool, bool, bool]:
    environment_confirmed = file_hash == os.getenv(env_var)
    file_confirmed = False

    if confirm_file.exists():
        confirmed_content = confirm_file.read_text(encoding="utf-8").strip()
        file_confirmed = file_hash == confirmed_content

    return environment_confirmed or file_confirmed, file_confirmed, environment_confirmed


def get_agreement_status(include_content: bool = False) -> dict[str, AgreementDocumentStatus]:
    """获取 EULA 和隐私条款的确认状态。"""
    status: dict[str, AgreementDocumentStatus] = {}

    for key, info in AGREEMENT_FILES.items():
        file_name = info["file_name"]
        file_path = PROJECT_ROOT / file_name
        content = _read_text(file_path, file_name)
        file_hash = hashlib.md5(content.encode("utf-8"), usedforsecurity=False).hexdigest()
        confirmed, file_confirmed, environment_confirmed = _check_confirmation(
            file_hash,
            PROJECT_ROOT / info["confirmation_file_name"],
            info["env_var"],
        )

        status[key] = AgreementDocumentStatus(
            key=key,
            title=info["title"],
            file_name=file_name,
            confirmation_file_name=info["confirmation_file_name"],
            env_var=info["env_var"],
            hash=file_hash,
            confirmed=confirmed,
            file_confirmed=file_confirmed,
            environment_confirmed=environment_confirmed,
            content=content if include_content else "",
        )

    return status


def are_agreements_confirmed() -> bool:
    """检查 EULA 和隐私条款是否都已确认。"""
    return all(document.confirmed for document in get_agreement_status(include_content=False).values())


def confirm_agreements(eula_hash: str, privacy_hash: str) -> dict[str, AgreementDocumentStatus]:
    """写入当前 EULA 和隐私条款的确认文件。"""
    status = get_agreement_status(include_content=False)
    expected_hashes = {
        "eula": eula_hash,
        "privacy": privacy_hash,
    }

    for key, expected_hash in expected_hashes.items():
        current_hash = status[key].hash
        if expected_hash != current_hash:
            logger.warning(
                "协议确认哈希不匹配",
                event_code="agreement.hash_mismatch",
                agreement=key,
                expected=current_hash,
                received=expected_hash,
            )
            raise ValueError("协议内容已更新，请刷新页面后重新确认")

    for document in status.values():
        confirm_path = PROJECT_ROOT / document.confirmation_file_name
        descriptor, temporary_path = tempfile.mkstemp(dir=PROJECT_ROOT, prefix=".agreement-", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(document.hash)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, confirm_path)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)
        logger.info(
            "协议确认文件已更新",
            event_code="agreement.confirmation_updated",
            agreement=document.key,
            path=str(confirm_path),
            hash=document.hash,
        )

    return get_agreement_status(include_content=True)
