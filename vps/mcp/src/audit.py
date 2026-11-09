"""Append-only audit log writer (JSON lines)."""
import json
from datetime import datetime, timezone

from .config import AUDIT_LOG


def log_operation(operation: str, path: str, result: str, details: dict | None = None) -> None:
    """Append a single audit entry.

    Args:
        operation: Tool name (e.g., "read_note", "create_inbox_note").
        path: Vault-relative path being operated on.
        result: "success" or "error".
        details: Optional dict with additional context (size, error string, etc.).
    """
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "path": path,
        "result": result,
        "details": details or {},
    }

    with AUDIT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
