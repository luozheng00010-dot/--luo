"""密钥管理（计划 3.2 / P1-05）。

密钥存系统钥匙串（macOS Keychain），数据库只存引用键名；
日志脱敏由 app/logging_setup.py 统一处理。
"""

from __future__ import annotations

import keyring
from keyring.errors import KeyringError

KEYRING_SERVICE = "autoeditor"

# 测试与无钥匙串环境的后备存储
_FALLBACK: dict[str, str] = {}
_use_fallback = False


def enable_fallback() -> None:
    """显式启用内存后备（仅测试）。"""
    global _use_fallback
    _use_fallback = True


def _backend_available() -> bool:
    if _use_fallback:
        return False
    try:
        return keyring.get_keyring() is not None
    except Exception:  # noqa: BLE001
        return False


def set_secret(ref: str, value: str) -> None:
    """保存密钥，ref 是数据库/设置中保存的引用名。"""
    if _use_fallback:
        _FALLBACK[ref] = value
        return
    keyring.set_password(KEYRING_SERVICE, ref, value)


def get_secret(ref: str) -> str | None:
    if _use_fallback:
        return _FALLBACK.get(ref)
    try:
        return keyring.get_password(KEYRING_SERVICE, ref)
    except KeyringError:
        return None


def delete_secret(ref: str) -> None:
    if _use_fallback:
        _FALLBACK.pop(ref, None)
        return
    try:
        keyring.delete_password(KEYRING_SERVICE, ref)
    except KeyringError:
        pass


def list_refs() -> list[str]:
    """列出已知引用名（后备存储可枚举；钥匙串环境由 settings 表中的引用记录补充）。"""
    if _use_fallback:
        return sorted(_FALLBACK.keys())
    return []
