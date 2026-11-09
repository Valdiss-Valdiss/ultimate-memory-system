"""Configuration loaded from environment variables."""
import os
from pathlib import Path

VAULT_PATH: Path = Path(os.getenv("VAULT_PATH", "/vault")).resolve()
INBOX_PREFIX: str = os.getenv("INBOX_PREFIX", "Inbox/AI/")
TRASH_PREFIX: str = INBOX_PREFIX + "Trash/"
AUDIT_LOG: Path = Path(os.getenv("AUDIT_LOG", "/var/log/memory-mcp/audit.log"))
