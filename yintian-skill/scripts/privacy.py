"""隐填 · 本地保险箱路径边界。"""
from __future__ import annotations

from pathlib import Path

import config


class PrivacyViolation(RuntimeError):
    """Raised when private material crosses a forbidden boundary."""


def assert_in_vault(path: str, vault_dir: str | None = None) -> str:
    """Confirm a private source path is inside YINTIAN_VAULT_DIR when configured."""
    vault_dir = config.VAULT_DIR if vault_dir is None else vault_dir
    if not vault_dir:
        return path

    target = Path(path).expanduser().resolve()
    vault = Path(vault_dir).expanduser().resolve()
    if target != vault and vault not in target.parents:
        raise PrivacyViolation("路径在配置的隐填保险箱之外")
    return path
