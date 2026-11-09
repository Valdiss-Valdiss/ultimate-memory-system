"""Path validation and security layer.

Single-writer policy enforcement: writes are allowed only inside the INBOX_PREFIX
subdirectory of the vault. Reads are allowed anywhere inside the vault.
"""
from pathlib import Path

from .config import INBOX_PREFIX, VAULT_PATH


class PathSecurityError(Exception):
    """Raised when path validation fails."""


def _resolve_under_vault(path: str) -> Path:
    if not path:
        raise PathSecurityError("Empty path")

    candidate = path.lstrip("/")
    full_path = (VAULT_PATH / candidate).resolve()

    try:
        full_path.relative_to(VAULT_PATH)
    except ValueError:
        raise PathSecurityError(f"Path traversal detected: {path!r}")

    return full_path


def validate_read_path(path: str) -> Path:
    """Resolve and validate a path for READ operations.

    The resolved path must lie inside VAULT_PATH.
    """
    return _resolve_under_vault(path)


def validate_write_path(path: str) -> Path:
    """Resolve and validate a path for WRITE operations.

    The resolved path must lie inside VAULT_PATH AND under INBOX_PREFIX.
    """
    full_path = _resolve_under_vault(path)

    rel_path = full_path.relative_to(VAULT_PATH).as_posix()
    if not rel_path.startswith(INBOX_PREFIX):
        raise PathSecurityError(
            f"Write operations only allowed in '{INBOX_PREFIX}*', got: {rel_path!r}"
        )

    return full_path
