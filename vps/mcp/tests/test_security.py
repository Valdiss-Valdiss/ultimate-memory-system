"""Tests for security.py path validation.

Strategy: create a temp vault with Inbox/AI/, exercise every guard,
verify expected pass/fail behaviour.

Note: security.py binds VAULT_PATH/INBOX_PREFIX at import time, so the
fixture patches bindings in BOTH src.config and src.security.
"""
from pathlib import Path

import pytest

from src.security import (
    PathSecurityError,
    validate_read_path,
    validate_write_path,
)


@pytest.fixture
def fake_vault(tmp_path, monkeypatch):
    """Set up a fake vault at tmp_path with Inbox/AI/, override config."""
    vault = tmp_path / "vault"
    (vault / "Public").mkdir(parents=True)
    (vault / "Business").mkdir(parents=True)
    (vault / "RAW").mkdir(parents=True)
    (vault / "Inbox" / "AI").mkdir(parents=True)
    (vault / "Public" / "hello.md").write_text("# hello", encoding="utf-8")

    monkeypatch.setattr("src.config.VAULT_PATH", vault.resolve())
    monkeypatch.setattr("src.config.INBOX_PREFIX", "Inbox/AI/")
    monkeypatch.setattr("src.security.VAULT_PATH", vault.resolve())
    monkeypatch.setattr("src.security.INBOX_PREFIX", "Inbox/AI/")
    return vault


class TestReadValidation:
    def test_simple_relative_path(self, fake_vault):
        p = validate_read_path("Public/hello.md")
        assert p == (fake_vault / "Public" / "hello.md").resolve()

    def test_leading_slash_stripped(self, fake_vault):
        p = validate_read_path("/Public/hello.md")
        assert p == (fake_vault / "Public" / "hello.md").resolve()

    def test_traversal_blocked(self, fake_vault):
        with pytest.raises(PathSecurityError):
            validate_read_path("../../../etc/passwd")

    def test_traversal_with_valid_prefix_blocked(self, fake_vault):
        with pytest.raises(PathSecurityError):
            validate_read_path("Public/../../../etc/passwd")

    def test_absolute_path_treated_as_vault_relative(self, fake_vault, tmp_path):
        # Semantics: leading slashes are stripped, so any input is interpreted
        # as a vault-relative path. There is no escape: the joined+resolved
        # result is always checked to stay inside the vault. An absolute
        # filesystem path therefore maps to a (non-existent) path INSIDE the
        # vault - it must never resolve outside VAULT_PATH.
        outside = tmp_path / "outside.md"
        outside.write_text("x", encoding="utf-8")
        p = validate_read_path(str(outside))
        p.relative_to(fake_vault.resolve())  # must not raise

    def test_empty_path_blocked(self, fake_vault):
        with pytest.raises(PathSecurityError):
            validate_read_path("")

    def test_deep_nested_relative_path_allowed(self, fake_vault):
        (fake_vault / "Business" / "Client1").mkdir(parents=True)
        (fake_vault / "Business" / "Client1" / "ICP.md").write_text("icp", encoding="utf-8")
        p = validate_read_path("Business/Client1/ICP.md")
        assert p.name == "ICP.md"


class TestWriteValidation:
    def test_inbox_path_allowed(self, fake_vault):
        p = validate_write_path("Inbox/AI/notes.md")
        assert p == (fake_vault / "Inbox" / "AI" / "notes.md").resolve()

    def test_inbox_nested_allowed(self, fake_vault):
        p = validate_write_path("Inbox/AI/research/competitors.md")
        assert p.name == "competitors.md"

    def test_public_write_blocked(self, fake_vault):
        with pytest.raises(PathSecurityError, match="Write operations only allowed"):
            validate_write_path("Public/new.md")

    def test_business_write_blocked(self, fake_vault):
        with pytest.raises(PathSecurityError, match="Write operations only allowed"):
            validate_write_path("Business/secret.md")

    def test_raw_write_blocked(self, fake_vault):
        with pytest.raises(PathSecurityError, match="Write operations only allowed"):
            validate_write_path("RAW/raw.md")

    def test_inbox_prefix_substring_blocked(self, fake_vault):
        # "Inbox/AI-other/" is NOT under "Inbox/AI/"
        with pytest.raises(PathSecurityError, match="Write operations only allowed"):
            validate_write_path("Inbox/AI-other/notes.md")

    def test_traversal_in_write_blocked(self, fake_vault):
        with pytest.raises(PathSecurityError):
            validate_write_path("Inbox/AI/../../../etc/passwd")

    def test_inboxai_no_slash_blocked(self, fake_vault):
        # Just "InboxAI" without slash is not under "Inbox/AI/"
        with pytest.raises(PathSecurityError, match="Write operations only allowed"):
            validate_write_path("InboxAI/notes.md")

    def test_trash_path_allowed_for_write(self, fake_vault):
        # Trash lives inside the zone: delete_note must be able to move files there.
        p = validate_write_path("Inbox/AI/Trash/sub/note.md")
        assert p == (fake_vault / "Inbox" / "AI" / "Trash" / "sub" / "note.md").resolve()
