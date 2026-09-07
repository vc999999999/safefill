"""隐填 · 本地保险箱路径边界。"""
from __future__ import annotations

from pathlib import Path

import config


class PrivacyViolation(RuntimeError):
    """Raised when private material crosses a forbidden boundary."""


def assert_in_vault(path: str) -> str:
    """Confirm a private source path is inside YINTIAN_VAULT_DIR when configured."""
    if not config.VAULT_DIR:
        return path

    target = Path(path).expanduser().resolve()
    vault = Path(config.VAULT_DIR).expanduser().resolve()
    if target != vault and vault not in target.parents:
        raise PrivacyViolation("路径在配置的隐填保险箱之外")
    return path
