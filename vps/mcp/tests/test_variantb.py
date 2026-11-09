"""Tests for Variant B tools: edit_note, move_note, delete_note + Trash filtering.

Calls tool functions directly (FastMCP decorator returns the original function).
Zone bindings are monkeypatched in src.config, src.security and src.server
because all three bind config values at import time.

Tool functions CATCH PathSecurityError and return {"error": ...} dicts, so
security denials are asserted as error responses, not raised exceptions.
"""
from pathlib import Path

import pytest

from src import server as srv

ZONE = "Inbox/AI/"
TRASH = "Inbox/AI/Trash/"


@pytest.fixture
def fake_vault(tmp_path, monkeypatch):
    """Fake vault: human folders + agent zone; config patched in all modules."""
    vault = tmp_path / "vault"
    (vault / "Public").mkdir(parents=True)
    (vault / "Business").mkdir(parents=True)
    (vault / ZONE).mkdir(parents=True)
    (vault / "Public" / "hello.md").write_text("# hello", encoding="utf-8")

    monkeypatch.setattr("src.config.VAULT_PATH", vault.resolve())
    monkeypatch.setattr("src.config.INBOX_PREFIX", ZONE)
    monkeypatch.setattr("src.config.TRASH_PREFIX", TRASH)
    monkeypatch.setattr("src.security.VAULT_PATH", vault.resolve())
    monkeypatch.setattr("src.security.INBOX_PREFIX", ZONE)
    monkeypatch.setattr("src.server.VAULT_PATH", vault.resolve())
    monkeypatch.setattr("src.server.INBOX_PREFIX", ZONE)
    monkeypatch.setattr("src.server.TRASH_PREFIX", TRASH)
    return vault


def _create(vault, rel, content):
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


class TestEditNote:
    def test_append_adds_at_end(self, fake_vault):
        _create(fake_vault, "Inbox/AI/n.md", "line1\n")
        r = srv.edit_note("Inbox/AI/n.md", "append", content="line2")
        assert r["status"] == "edited"
        assert (fake_vault / "Inbox/AI/n.md").read_text(encoding="utf-8") == "line1\nline2"

    def test_append_without_trailing_newline(self, fake_vault):
        _create(fake_vault, "Inbox/AI/n.md", "line1")
        srv.edit_note("Inbox/AI/n.md", "append", content="line2")
        assert (fake_vault / "Inbox/AI/n.md").read_text(encoding="utf-8") == "line1\nline2"

    def test_prepend_adds_at_start(self, fake_vault):
        _create(fake_vault, "Inbox/AI/n.md", "line1\n")
        r = srv.edit_note("Inbox/AI/n.md", "prepend", content="top\n")
        assert r["status"] == "edited"
        assert (fake_vault / "Inbox/AI/n.md").read_text(encoding="utf-8") == "top\nline1\n"

    def test_prepend_content_without_newline(self, fake_vault):
        _create(fake_vault, "Inbox/AI/n.md", "line1\n")
        srv.edit_note("Inbox/AI/n.md", "prepend", content="top")
        assert (fake_vault / "Inbox/AI/n.md").read_text(encoding="utf-8") == "top\nline1\n"

    def test_append_empty_content_rejected(self, fake_vault):
        _create(fake_vault, "Inbox/AI/n.md", "x")
        r = srv.edit_note("Inbox/AI/n.md", "append", content="")
        assert "error" in r

    def test_find_replace_all_occurrences(self, fake_vault):
        _create(fake_vault, "Inbox/AI/n.md", "foo and foo")
        r = srv.edit_note("Inbox/AI/n.md", "find_replace", content="bar", old_text="foo")
        assert r["replaced_count"] == 2
        assert (fake_vault / "Inbox/AI/n.md").read_text(encoding="utf-8") == "bar and bar"

    def test_find_replace_not_found_no_change(self, fake_vault):
        _create(fake_vault, "Inbox/AI/n.md", "stable content")
        r = srv.edit_note("Inbox/AI/n.md", "find_replace", content="x", old_text="absent")
        assert "error" in r and "not found" in r["error"]
        assert (fake_vault / "Inbox/AI/n.md").read_text(encoding="utf-8") == "stable content"

    def test_find_replace_empty_old_text_rejected(self, fake_vault):
        _create(fake_vault, "Inbox/AI/n.md", "x")
        r = srv.edit_note("Inbox/AI/n.md", "find_replace", content="x", old_text="")
        assert "error" in r

    def test_replace_section_replaces_body_keeps_heading(self, fake_vault):
        _create(
            fake_vault,
            "Inbox/AI/n.md",
            "# Title\n\n## Status\ntext A\ntext B\n\n## Notes\nkeep me\n",
        )
        r = srv.edit_note("Inbox/AI/n.md", "replace_section", content="new body", section="## Status")
        assert r["status"] == "edited"
        out = (fake_vault / "Inbox/AI/n.md").read_text(encoding="utf-8")
        assert out == "# Title\n\n## Status\nnew body\n\n## Notes\nkeep me\n"

    def test_replace_section_stops_at_same_level(self, fake_vault):
        _create(fake_vault, "Inbox/AI/n.md", "## One\na\n## Two\nb\n")
        srv.edit_note("Inbox/AI/n.md", "replace_section", content="X", section="One")
        out = (fake_vault / "Inbox/AI/n.md").read_text(encoding="utf-8")
        assert out == "## One\nX\n\n## Two\nb\n"

    def test_replace_section_includes_subsections(self, fake_vault):
        # Body extends until the next heading of same/higher level:
        # subsections are part of the body and are replaced with it.
        _create(fake_vault, "Inbox/AI/n.md", "## One\na\n### Sub\ns\n")
        srv.edit_note("Inbox/AI/n.md", "replace_section", content="X", section="One")
        out = (fake_vault / "Inbox/AI/n.md").read_text(encoding="utf-8")
        assert out == "## One\nX\n"

    def test_replace_section_empty_body_clears_section(self, fake_vault):
        _create(fake_vault, "Inbox/AI/n.md", "## One\na\n## Two\nb\n")
        srv.edit_note("Inbox/AI/n.md", "replace_section", content="", section="One")
        out = (fake_vault / "Inbox/AI/n.md").read_text(encoding="utf-8")
        assert out == "## One\n## Two\nb\n"

    def test_replace_section_not_found(self, fake_vault):
        _create(fake_vault, "Inbox/AI/n.md", "## A\nx\n")
        r = srv.edit_note("Inbox/AI/n.md", "replace_section", content="y", section="Missing")
        assert "error" in r and "not found" in r["error"]

    def test_unknown_operation_rejected(self, fake_vault):
        _create(fake_vault, "Inbox/AI/n.md", "x")
        r = srv.edit_note("Inbox/AI/n.md", "destroy", content="x")
        assert "error" in r and "Unknown operation" in r["error"]

    def test_missing_file(self, fake_vault):
        r = srv.edit_note("Inbox/AI/absent.md", "append", content="x")
        assert "error" in r

    def test_outside_zone_denied(self, fake_vault):
        _create(fake_vault, "Public/hello.md", "# hello")
        r = srv.edit_note("Public/hello.md", "append", content="x")
        assert "error" in r and "Write operations only allowed" in r["error"]
        assert (fake_vault / "Public/hello.md").read_text(encoding="utf-8") == "# hello"

    def test_zone_substring_escape_denied(self, fake_vault):
        r = srv.edit_note("Inbox/AI-other/n.md", "append", content="x")
        assert "error" in r and "Write operations only allowed" in r["error"]

    def test_traversal_denied(self, fake_vault):
        r = srv.edit_note("Inbox/AI/../../../etc/passwd", "append", content="x")
        assert "error" in r and "traversal" in r["error"].lower()


class TestMoveNote:
    def test_move_inside_zone(self, fake_vault):
        _create(fake_vault, "Inbox/AI/a.md", "data")
        r = srv.move_note("Inbox/AI/a.md", "Inbox/AI/b.md")
        assert r["status"] == "moved"
        assert not (fake_vault / "Inbox/AI/a.md").exists()
        assert (fake_vault / "Inbox/AI/b.md").read_text(encoding="utf-8") == "data"

    def test_move_creates_parent_dirs(self, fake_vault):
        _create(fake_vault, "Inbox/AI/a.md", "data")
        r = srv.move_note("Inbox/AI/a.md", "Inbox/AI/deep/nested/b.md")
        assert r["status"] == "moved"
        assert (fake_vault / "Inbox/AI/deep/nested/b.md").exists()

    def test_move_out_of_zone_denied(self, fake_vault):
        _create(fake_vault, "Inbox/AI/a.md", "data")
        r = srv.move_note("Inbox/AI/a.md", "Public/a.md")
        assert "error" in r and "Write operations only allowed" in r["error"]
        assert (fake_vault / "Inbox/AI/a.md").read_text(encoding="utf-8") == "data"

    def test_move_from_out_of_zone_denied(self, fake_vault):
        r = srv.move_note("Public/hello.md", "Inbox/AI/hello.md")
        assert "error" in r and "Write operations only allowed" in r["error"]
        assert (fake_vault / "Public/hello.md").exists()

    def test_move_destination_exists_denied(self, fake_vault):
        _create(fake_vault, "Inbox/AI/a.md", "data")
        _create(fake_vault, "Inbox/AI/b.md", "other")
        r = srv.move_note("Inbox/AI/a.md", "Inbox/AI/b.md")
        assert "error" in r and "already exists" in r["error"]
        assert (fake_vault / "Inbox/AI/b.md").read_text(encoding="utf-8") == "other"

    def test_move_non_md_denied(self, fake_vault):
        _create(fake_vault, "Inbox/AI/data.txt", "x")
        r = srv.move_note("Inbox/AI/data.txt", "Inbox/AI/data2.txt")
        assert "error" in r and ".md" in r["error"]

    def test_move_missing_source(self, fake_vault):
        r = srv.move_note("Inbox/AI/absent.md", "Inbox/AI/x.md")
        assert "error" in r


class TestDeleteNote:
    def test_delete_requires_confirm(self, fake_vault):
        _create(fake_vault, "Inbox/AI/a.md", "data")
        r = srv.delete_note("Inbox/AI/a.md", confirm=False)
        assert "confirm" in r["error"]
        assert (fake_vault / "Inbox/AI/a.md").exists()

    def test_delete_moves_to_trash_keeps_structure(self, fake_vault):
        _create(fake_vault, "Inbox/AI/sub/a.md", "data")
        r = srv.delete_note("Inbox/AI/sub/a.md", confirm=True)
        assert r["status"] == "deleted"
        assert r["trash_path"] == "Inbox/AI/Trash/sub/a.md"
        assert r["previous_size"] == 4
        assert not (fake_vault / "Inbox/AI/sub/a.md").exists()
        assert (fake_vault / "Inbox/AI/Trash/sub/a.md").read_text(encoding="utf-8") == "data"

    def test_delete_collision_gets_timestamp_suffix(self, fake_vault):
        _create(fake_vault, "Inbox/AI/a.md", "first")
        srv.delete_note("Inbox/AI/a.md", confirm=True)
        _create(fake_vault, "Inbox/AI/a.md", "second")
        r = srv.delete_note("Inbox/AI/a.md", confirm=True)
        assert r["status"] == "deleted"
        assert r["trash_path"].endswith(".md")
        assert r["trash_path"] != "Inbox/AI/Trash/a.md"
        assert (fake_vault / r["trash_path"]).read_text(encoding="utf-8") == "second"

    def test_delete_inside_trash_blocked(self, fake_vault):
        _create(fake_vault, "Inbox/AI/Trash/x.md", "data")
        r = srv.delete_note("Inbox/AI/Trash/x.md", confirm=True)
        assert "error" in r and "human-only" in r["error"]
        assert (fake_vault / "Inbox/AI/Trash/x.md").read_text(encoding="utf-8") == "data"

    def test_delete_non_md_denied(self, fake_vault):
        _create(fake_vault, "Inbox/AI/data.txt", "x")
        r = srv.delete_note("Inbox/AI/data.txt", confirm=True)
        assert "error" in r and ".md" in r["error"]

    def test_delete_outside_zone_denied(self, fake_vault):
        _create(fake_vault, "Public/hello.md", "# hello")
        r = srv.delete_note("Public/hello.md", confirm=True)
        assert "error" in r and "Write operations only allowed" in r["error"]
        assert (fake_vault / "Public/hello.md").read_text(encoding="utf-8") == "# hello"

    def test_restore_from_trash_via_move(self, fake_vault):
        _create(fake_vault, "Inbox/AI/a.md", "data")
        r = srv.delete_note("Inbox/AI/a.md", confirm=True)
        trash_rel = r["trash_path"]
        r2 = srv.move_note(trash_rel, "Inbox/AI/a.md")
        assert r2["status"] == "moved"
        assert (fake_vault / "Inbox/AI/a.md").read_text(encoding="utf-8") == "data"


class TestTrashFiltering:
    def test_md_files_excludes_trash(self, fake_vault):
        _create(fake_vault, "Inbox/AI/live.md", "visible")
        _create(fake_vault, "Inbox/AI/Trash/dead.md", "deleted marker")
        files = srv._md_files()
        rels = [p.relative_to(fake_vault).as_posix() for p in files]
        assert "Inbox/AI/live.md" in rels
        assert all(not rel.startswith(TRASH) for rel in rels)

    def test_list_notes_excludes_trash(self, fake_vault):
        _create(fake_vault, "Inbox/AI/live.md", "visible")
        _create(fake_vault, "Inbox/AI/Trash/dead.md", "deleted")
        r = srv.list_notes(folder="Inbox/AI/", limit=100)
        assert r["notes"] == ["Inbox/AI/live.md"]

    def test_search_excludes_trash(self, fake_vault):
        _create(fake_vault, "Inbox/AI/live.md", "unique-xyz visible")
        _create(fake_vault, "Inbox/AI/Trash/dead.md", "unique-xyz deleted")
        r = srv.search_notes("unique-xyz", limit=20)
        assert r["matches"] == ["Inbox/AI/live.md"]

    def test_stats_excludes_trash(self, fake_vault):
        _create(fake_vault, "Inbox/AI/live.md", "visible")
        before = srv.get_vault_stats()["file_count"]
        _create(fake_vault, "Inbox/AI/Trash/dead.md", "deleted")
        after = srv.get_vault_stats()["file_count"]
        assert after == before

    def test_read_can_still_read_trashed(self, fake_vault):
        _create(fake_vault, "Inbox/AI/Trash/dead.md", "content")
        r = srv.read_note("Inbox/AI/Trash/dead.md")
        assert r["content"] == "content"
