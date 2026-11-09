"""Memory MCP server - main entry point.

Exposes the tools over SSE / Streamable HTTP:

    list_notes, read_note, search_notes, get_vault_stats,
    create_inbox_note, append_to_inbox_note,
    edit_note, move_note, delete_note

Single-writer policy: writes are allowed only inside the agent zone
(INBOX_PREFIX, e.g. Inbox/AI/). Reads are allowed anywhere in the vault.

Variant B (full CRUD inside the zone):
    - edit_note: append / prepend / find_replace / replace_section
    - move_note: rename/move strictly inside the zone (restore from Trash)
    - delete_note: SOFT delete -> moves file to <zone>/Trash/ (requires confirm);
      permanent cleanup of Trash is human-only.

Trash/<zone> contents are hidden from list/search/stats.

All operations are appended to the audit log.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .audit import log_operation
from .config import INBOX_PREFIX, TRASH_PREFIX, VAULT_PATH
from .security import PathSecurityError, validate_read_path, validate_write_path
from urllib.parse import quote as _quote

# --- Кликабельные ссылки на заметки (2026-11-07) ---------------------------------
# telegram_url — ссылка на исходный пост в архивном канале (из frontmatter `url`, не угадывается);
# obsidian_url — открыть заметку в Obsidian на ПК (obsidian://);
# obsidian_web — та же заметка через https-переходник (для приложений, которые режут obsidian://).
_OBS_VAULT = os.getenv("OBSIDIAN_VAULT_NAME", "Obsidian Vault")
_OBS_BRIDGE = os.getenv("OBSIDIAN_BRIDGE", "https://mcp.YOUR-DOMAIN/o?")


import re as _re_links
_DERIVED = _re_links.compile(r"^Inbox/[^/]+/(Search|Status|Trash)/")


def _is_derived(rel: str) -> bool:
    return bool(_DERIVED.match(rel))


def _links(rel: str) -> dict:
    out: dict = {}
    try:
        f = VAULT_PATH / rel
        head = f.read_text(encoding="utf-8", errors="replace")[:4000] if f.is_file() else ""
        if head.startswith("---"):
            end = head.find("\n---", 3)
            for line in (head[3:end] if end > 0 else "").splitlines():
                if line.startswith("url:"):
                    u = line[4:].strip().strip('"').strip("'")
                    if u.startswith("https://t.me/"):
                        out["telegram_url"] = u
                    elif u.startswith("http"):
                        out["source_url"] = u
                    break
    except Exception:  # noqa: BLE001
        pass
    enc = _quote(rel[:-3] if rel.endswith(".md") else rel, safe="")
    out["obsidian_url"] = f"obsidian://open?vault={_quote(_OBS_VAULT, safe='')}&file={enc}"
    out["obsidian_web"] = _OBS_BRIDGE + enc
    return out


def _make_mcp() -> FastMCP:
    """Construct FastMCP, falling back gracefully across SDK versions."""
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8000"))
    try:
        return FastMCP("memory-mcp", host=host, port=port)
    except TypeError:
        mcp = FastMCP("memory-mcp")
        try:
            mcp.settings.host = host  # type: ignore[attr-defined]
            mcp.settings.port = port  # type: ignore[attr-defined]
        except AttributeError:
            pass
        return mcp


mcp = _make_mcp()


def _md_files() -> list[Path]:
    """All markdown files in the vault, excluding the zone Trash directory."""
    return sorted(
        p
        for p in VAULT_PATH.rglob("*.md")
        if p.is_file()
        and not p.relative_to(VAULT_PATH).as_posix().startswith(TRASH_PREFIX)
    )


def _find_section(lines: list[str], section: str) -> tuple[int, int] | tuple[None, None]:
    """Locate a heading whose text matches `section` (exact, case-insensitive).

    Returns (line_index, heading_level) or (None, None). Inline '#' inside code
    fences is not guarded against in v1 - agent-written notes only.
    """
    target = section.strip().lstrip("#").strip().lower()
    if not target:
        return None, None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        level = len(stripped) - len(stripped.lstrip("#"))
        text = stripped.lstrip("#").strip().lower()
        if text == target:
            return i, level
    return None, None


def _section_end(lines: list[str], start: int, level: int) -> int:
    """Index of the next heading with level <= `level`, or end of file."""
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("#"):
            lvl = len(stripped) - len(stripped.lstrip("#"))
            if lvl <= level:
                return j
    return len(lines)


@mcp.tool()
def list_notes(folder: str = "", limit: int = 100) -> dict[str, Any]:
    """List markdown notes in the vault.

    Args:
        folder: Optional folder prefix to filter (e.g. "Business/").
        limit: Maximum number of notes to return (default 100).
    """
    try:
        search_root = VAULT_PATH
        if folder:
            search_root = (VAULT_PATH / folder).resolve()
            search_root.relative_to(VAULT_PATH)

        notes: list[str] = []
        for md in search_root.rglob("*.md"):
            if not md.is_file():
                continue
            if md.relative_to(VAULT_PATH).as_posix().startswith(TRASH_PREFIX):
                continue
            if len(notes) >= limit:
                break
            notes.append(md.relative_to(VAULT_PATH).as_posix())

        log_operation("list_notes", folder, "success", {"count": len(notes)})
        return {"count": len(notes), "notes": notes,
                "items": [{"path": n, **_links(n)} for n in notes]}
    except Exception as exc:  # noqa: BLE001
        log_operation("list_notes", folder, "error", {"error": str(exc)})
        return {"count": 0, "notes": [], "error": str(exc)}


@mcp.tool()
def read_note(path: str) -> dict[str, Any]:
    """Read a note by vault-relative path."""
    try:
        full_path = validate_read_path(path)
        if not full_path.is_file():
            log_operation("read_note", path, "error", {"error": "not_found"})
            return {"path": path, "error": "File not found"}

        content = full_path.read_text(encoding="utf-8")
        stat = full_path.stat()

        log_operation(
            "read_note",
            path,
            "success",
            {"size": stat.st_size},
        )
        return {
            "path": path,
            "content": content,
            "size": stat.st_size,
            "modified": stat.st_mtime,
            **_links(path),
        }
    except PathSecurityError as exc:
        log_operation("read_note", path, "error", {"error": str(exc)})
        return {"path": path, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log_operation("read_note", path, "error", {"error": str(exc)})
        return {"path": path, "error": str(exc)}


@mcp.tool()
def search_notes(query: str, limit: int = 20) -> dict[str, Any]:
    """Поиск ПО ТОЧНЫМ ПОДСТРОКАМ (case-insensitive). Только для точных слов,
    имён, тегов, id. Для смыслового поиска («найди про X», «что у меня по теме Y»,
    синонимы, перефразировки) используй первым инструмент semantic_search."""
    try:
        q = query.lower()
        matches: list[str] = []
        for md in _md_files():
            if len(matches) >= limit:
                break
            try:
                content = md.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if q in content.lower():
                _rel = md.relative_to(VAULT_PATH).as_posix()
                if not _is_derived(_rel):
                    matches.append(_rel)

        log_operation("search_notes", query, "success", {"count": len(matches)})
        result: dict[str, Any] = {"count": len(matches), "matches": matches,
                                  "items": [{"path": m, **_links(m)} for m in matches]}
        if len(matches) < 3:
            result["hint"] = ("Найдено мало: это поиск точных подстрок. "
                              "Для смыслового поиска вызови semantic_search(query='...').")
        return result
    except Exception as exc:  # noqa: BLE001
        log_operation("search_notes", query, "error", {"error": str(exc)})
        return {"count": 0, "matches": [], "error": str(exc)}


@mcp.tool()
def get_vault_stats() -> dict[str, Any]:
    """Get basic statistics about the vault."""
    try:
        files = _md_files()
        total_size = sum(f.stat().st_size for f in files)
        folders = sorted({
            f.relative_to(VAULT_PATH).parent.as_posix()
            for f in files
            if f.relative_to(VAULT_PATH).parent != Path(".")
        })

        log_operation(
            "get_vault_stats",
            "",
            "success",
            {"file_count": len(files), "total_size": total_size},
        )
        return {
            "file_count": len(files),
            "total_size": total_size,
            "folders": folders,
        }
    except Exception as exc:  # noqa: BLE001
        log_operation("get_vault_stats", "", "error", {"error": str(exc)})
        return {"error": str(exc)}


@mcp.tool()
def create_inbox_note(path: str, content: str) -> dict[str, Any]:
    """Create a new note inside the agent write zone (INBOX_PREFIX, e.g. Inbox/AI/).

    Args:
        path: Vault-relative path under the zone (e.g. "Inbox/AI/notes.md").
        content: Markdown content for the new note.
    """
    try:
        full_path = validate_write_path(path)
        if full_path.exists():
            log_operation("create_inbox_note", path, "error", {"error": "exists"})
            return {"path": path, "error": "File already exists. Use append_to_inbox_note or edit_note."}

        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")

        log_operation("create_inbox_note", path, "success", {"size": len(content)})
        return {"path": path, "status": "created", "size": len(content)}
    except PathSecurityError as exc:
        log_operation("create_inbox_note", path, "error", {"error": str(exc)})
        return {"path": path, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log_operation("create_inbox_note", path, "error", {"error": str(exc)})
        return {"path": path, "error": str(exc)}


@mcp.tool()
def append_to_inbox_note(path: str, content: str) -> dict[str, Any]:
    """Append content to an existing note inside the agent write zone."""
    try:
        full_path = validate_write_path(path)
        if not full_path.is_file():
            log_operation("append_to_inbox_note", path, "error", {"error": "not_found"})
            return {"path": path, "error": "File not found. Use create_inbox_note first."}

        existing = full_path.read_text(encoding="utf-8")
        new_content = (
            existing + content
            if existing.endswith("\n") or not existing
            else existing + "\n" + content
        )
        full_path.write_text(new_content, encoding="utf-8")

        log_operation(
            "append_to_inbox_note",
            path,
            "success",
            {"appended_size": len(content)},
        )
        return {
            "path": path,
            "status": "appended",
            "appended_size": len(content),
        }
    except PathSecurityError as exc:
        log_operation("append_to_inbox_note", path, "error", {"error": str(exc)})
        return {"path": path, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log_operation("append_to_inbox_note", path, "error", {"error": str(exc)})
        return {"path": path, "error": str(exc)}


@mcp.tool()
def edit_note(
    path: str,
    operation: str,
    content: str = "",
    old_text: str = "",
    section: str = "",
) -> dict[str, Any]:
    """Edit an existing note inside the agent write zone.

    Args:
        path: Vault-relative path under the zone (e.g. "Inbox/AI/notes.md").
        operation: One of "append", "prepend", "find_replace", "replace_section".
        content: New text (for append/prepend) or replacement text
            (for find_replace; for replace_section - new body of the section).
        old_text: Exact text to find (find_replace only; replaces ALL exact
            occurrences; no fuzzy matching).
        section: Heading text to locate (replace_section only), e.g. "## Status"
            or "Status". The heading line is preserved; the body until the next
            heading of the same or higher level (subsections included) is
            replaced. A blank line is kept before the following heading.

    Guards:
        - find_replace fails if old_text is empty or not found (no silent no-op).
        - replace_section fails if the heading is not found.
        - No fuzzy addressing: paths and old_text must match exactly.
    """
    try:
        full_path = validate_write_path(path)
        if not full_path.is_file():
            log_operation("edit_note", path, "error", {"error": "not_found"})
            return {"path": path, "error": "File not found. Use create_inbox_note first."}

        existing = full_path.read_text(encoding="utf-8")
        details: dict[str, Any] = {"operation": operation}

        if operation in ("append", "prepend"):
            if not content:
                log_operation("edit_note", path, "error", {"error": "empty_content"})
                return {"path": path, "error": f"content is required for {operation}"}
            if operation == "append":
                new_content = (
                    existing + content
                    if existing.endswith("\n") or not existing
                    else existing + "\n" + content
                )
            else:  # prepend
                sep = "" if (content.endswith("\n") or not existing) else "\n"
                new_content = content + sep + existing
            details["size_delta"] = len(new_content) - len(existing)

        elif operation == "find_replace":
            if not old_text:
                log_operation("edit_note", path, "error", {"error": "empty_old_text"})
                return {"path": path, "error": "old_text is required for find_replace"}
            count = existing.count(old_text)
            if count == 0:
                log_operation("edit_note", path, "error", {"error": "old_text_not_found"})
                return {"path": path, "error": "old_text not found in note (no changes made)"}
            new_content = existing.replace(old_text, content)
            details["replaced_count"] = count

        elif operation == "replace_section":
            if not section:
                log_operation("edit_note", path, "error", {"error": "empty_section"})
                return {"path": path, "error": "section is required for replace_section"}
            lines = existing.split("\n")
            start, level = _find_section(lines, section)
            if start is None:
                log_operation("edit_note", path, "error", {"error": "section_not_found"})
                return {"path": path, "error": f"Section heading not found: {section!r}"}
            end = _section_end(lines, start, level)
            body = content.split("\n") if content else []
            while body and body[-1] == "":
                body.pop()
            new_lines = lines[: start + 1] + body
            if end < len(lines) and lines[end].strip().startswith("#") and body:
                new_lines.append("")
            elif end >= len(lines) and existing.endswith("\n") and (not body or body[-1] != ""):
                new_lines.append("")
            new_lines += lines[end:]
            new_content = "\n".join(new_lines)
            details["section"] = section
            details["lines_removed"] = end - (start + 1)
            details["lines_added"] = len(body)
        else:
            log_operation("edit_note", path, "error", {"error": "unknown_operation"})
            return {
                "path": path,
                "error": f"Unknown operation {operation!r}. "
                "Use: append, prepend, find_replace, replace_section",
            }

        full_path.write_text(new_content, encoding="utf-8")

        log_operation("edit_note", path, "success", details)
        return {"path": path, "status": "edited", **details}
    except PathSecurityError as exc:
        log_operation("edit_note", path, "error", {"error": str(exc)})
        return {"path": path, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log_operation("edit_note", path, "error", {"error": str(exc)})
        return {"path": path, "error": str(exc)}


@mcp.tool()
def move_note(source: str, destination: str) -> dict[str, Any]:
    """Move or rename a note strictly inside the agent write zone.

    Both source and destination must resolve under INBOX_PREFIX. Moving out of
    the zone (including into human folders) is not allowed. Also used to
    restore a note from Trash (source inside Trash, destination back in zone).

    Args:
        source: Vault-relative path of the existing note (under the zone).
        destination: Vault-relative target path (must not exist; parent
            directories are created automatically).
    """
    try:
        src = validate_write_path(source)
        dst = validate_write_path(destination)

        if not src.is_file():
            log_operation("move_note", source, "error", {"error": "not_found"})
            return {"source": source, "error": "Source file not found"}
        if not source.strip().endswith(".md") or not destination.strip().endswith(".md"):
            log_operation("move_note", source, "error", {"error": "only_md"})
            return {"source": source, "error": "Only .md files can be moved"}
        if dst.exists():
            log_operation("move_note", source, "error", {"error": "destination_exists"})
            return {"source": source, "error": f"Destination already exists: {destination}"}

        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)

        details = {"destination": destination}
        log_operation("move_note", source, "success", details)
        return {"source": source, "destination": destination, "status": "moved"}
    except PathSecurityError as exc:
        log_operation("move_note", source, "error", {"error": str(exc)})
        return {"source": source, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log_operation("move_note", source, "error", {"error": str(exc)})
        return {"source": source, "error": str(exc)}


@mcp.tool()
def delete_note(path: str, confirm: bool = False) -> dict[str, Any]:
    """Soft-delete a note: move it to <zone>/Trash/ (recoverable).

    The note keeps its structure under Trash (e.g. Inbox/AI/sub/x.md becomes
    Inbox/AI/Trash/sub/x.md; on collision a timestamp suffix is added).
    Trashed notes are hidden from list/search/stats. Restore a note with
    move_note(trash_path, original_path). Permanent cleanup of Trash is a
    HUMAN-only operation - agents cannot empty the Trash.

    Args:
        path: Vault-relative path of the note to delete (under the zone).
        confirm: Must be explicitly true.
    """
    try:
        if not confirm:
            log_operation("delete_note", path, "error", {"error": "confirm_required"})
            return {
                "path": path,
                "error": "confirm must be true. Nothing was deleted. "
                "Show the note name to the user and ask for confirmation first.",
            }

        full_path = validate_write_path(path)
        if not full_path.is_file():
            log_operation("delete_note", path, "error", {"error": "not_found"})
            return {"path": path, "error": "File not found"}
        if not path.strip().endswith(".md"):
            log_operation("delete_note", path, "error", {"error": "only_md"})
            return {"path": path, "error": "Only .md notes can be deleted"}

        rel = full_path.relative_to(VAULT_PATH).as_posix()
        if rel.startswith(TRASH_PREFIX):
            log_operation("delete_note", path, "error", {"error": "already_in_trash"})
            return {
                "path": path,
                "error": "Note is already in Trash. Permanent deletion is a "
                "human-only operation (done via Obsidian/file system).",
            }

        previous_size = full_path.stat().st_size
        rel_under_zone = rel[len(INBOX_PREFIX):]
        trash_rel = TRASH_PREFIX + rel_under_zone
        trash_dst = (VAULT_PATH / trash_rel).resolve()

        if trash_dst.exists():
            stem, suffix = trash_dst.stem, trash_dst.suffix
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            trash_dst = trash_dst.with_name(f"{stem}-{ts}{suffix}")
            trash_rel = trash_dst.relative_to(VAULT_PATH).as_posix()

        trash_dst.parent.mkdir(parents=True, exist_ok=True)
        full_path.rename(trash_dst)

        log_operation(
            "delete_note",
            path,
            "success",
            {"trash_path": trash_rel, "previous_size": previous_size},
        )
        return {
            "path": path,
            "status": "deleted",
            "trash_path": trash_rel,
            "previous_size": previous_size,
            "restore_hint": f"move_note(source={trash_rel!r}, destination={rel!r})",
        }
    except PathSecurityError as exc:
        log_operation("delete_note", path, "error", {"error": str(exc)})
        return {"path": path, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log_operation("delete_note", path, "error", {"error": str(exc)})
        return {"path": path, "error": str(exc)}



# --- Semantic search (Этап 3): Qdrant + TEI, read-only ---------------------

QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
TEI_URL = os.getenv("TEI_URL", "http://memory_tei:80")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "vault_main")


def _qdrant_key():
    env = os.getenv("QDRANT_API_KEY", "")
    if env:
        return env
    p = os.getenv("QDRANT_KEY_FILE", "/qdrant-key")
    try:
        return Path(p).read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


def _embed_query(text):
    import json as _json
    import urllib.request as _ur

    body = _json.dumps({"inputs": ["query: " + text]}).encode()
    req = _ur.Request(TEI_URL + "/embed", data=body,
                      headers={"Content-Type": "application/json"}, method="POST")
    with _ur.urlopen(req, timeout=60) as r:
        return _json.loads(r.read().decode())[0]


def _qdrant_search(vector, flt, limit):
    import json as _json
    import urllib.request as _ur

    body: dict[str, Any] = {"vector": vector, "limit": limit, "with_payload": True}
    if flt:
        body["filter"] = flt
    req = _ur.Request(
        f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points/search",
        data=_json.dumps(body).encode(),
        headers={"api-key": _qdrant_key(), "Content-Type": "application/json"},
        method="POST",
    )
    with _ur.urlopen(req, timeout=60) as r:
        return _json.loads(r.read().decode()).get("result", [])


def _qdrant_post(path, body, timeout=60):
    import json as _json
    import urllib.request as _ur
    req = _ur.Request(f"{QDRANT_URL}{path}", data=_json.dumps(body).encode(),
                      headers={"api-key": _qdrant_key(), "Content-Type": "application/json"},
                      method="POST")
    with _ur.urlopen(req, timeout=timeout) as r:
        return _json.loads(r.read().decode()).get("result", [])


# --- Поиск v2 (2026-11-07, решения пользователя по ревизии поиска) -------------------
# Гибрид: смысловой (e5, коллекция QDRANT_COLLECTION) + словесный BM25 (коллекция
# QDRANT_BM25_COLLECTION, заметка = точка) по ДВУМ формулировкам: слова пользователя +
# расширение запроса одной дешёвой моделью на сервере (одинаково для всех агентов).
# Слияние списков по местам (RRF, k=60). Замер на 20 темах: nDCG@10 0,58 → 0,73.
QDRANT_BM25_COLLECTION = os.getenv("QDRANT_BM25_COLLECTION", "vault_bm25")
EXPAND_MODELS = [m for m in os.getenv("EXPAND_MODELS", "claude-haiku-5-5,glm-5.3-flash").split(",") if m]
EXPAND_URL = os.getenv("EXPAND_URL", "https://opencode.ai/zen/go/v1/chat/completions")
EXPAND_KEY_FILE = os.getenv("EXPAND_KEY_FILE", "/llm-key")
EXPAND_TIMEOUT = float(os.getenv("EXPAND_TIMEOUT", "8"))
SEARCH_MAX_NOTES = 100
_EXPAND_CACHE: dict[str, str] = {}
_EXPAND_SYS = (
    "Ты помогаешь искать в личной базе заметок пользователя. Заметки — сохранённые посты из его "
    "Telegram-каналов и ручные записи на темы: маркетинг, ИИ-инструменты, дизайн, разработка, бизнес, "
    "финансы, здоровье, контент (замените на 5–10 главных тем вашей базы — расширение станет точнее). "
    "Перепиши запрос в одну строку для поиска: сначала исходные слова, затем 6-12 синонимов, близких "
    "терминов, названий сервисов и английских эквивалентов, которые вероятно встречаются в таких заметках "
    "по этой теме. Только строка, без пояснений и кавычек.")


def _expand_query(query: str) -> str:
    """Расширенная формулировка запроса (кэш в памяти). Пусто — если модель недоступна."""
    import json as _json
    import urllib.request as _ur
    q = query.strip()
    if not q:
        return ""
    if q in _EXPAND_CACHE:
        return _EXPAND_CACHE[q]
    try:
        keys = [k.strip() for k in Path(EXPAND_KEY_FILE).read_text(encoding="utf-8-sig").splitlines() if k.strip()]
    except OSError:
        keys = []
    for model in EXPAND_MODELS:
        for key in keys:
            body = _json.dumps({"model": model, "max_tokens": 200, "temperature": 0,
                                "messages": [{"role": "system", "content": _EXPAND_SYS},
                                             {"role": "user", "content": q}]}).encode()
            req = _ur.Request(EXPAND_URL, data=body, method="POST", headers={
                "Authorization": "Bearer " + key, "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0", "x-opencode-session": "memory-system-search"})
            try:
                with _ur.urlopen(req, timeout=EXPAND_TIMEOUT) as r:
                    d = _json.loads(r.read().decode())
                txt = ((d["choices"][0]["message"].get("content") or "").strip().splitlines() or [""])[0]
                txt = txt.strip().strip('"«»').strip()[:400]
                if txt:
                    if len(_EXPAND_CACHE) > 2000:
                        _EXPAND_CACHE.clear()
                    _EXPAND_CACHE[q] = txt
                    return txt
            except Exception:  # noqa: BLE001
                continue
    return ""


def _flt(folder: str, channel: str):
    must = []
    if folder:
        must.append({"key": "folder", "match": {"value": folder}})
    if channel:
        must.append({"key": "channel", "match": {"value": channel}})
    return {"must": must} if must else None


def _skip(path: str) -> bool:
    return (not path) or _is_derived(path) or path.rsplit("/", 1)[-1].startswith("_index")


def _dense_notes(text: str, flt) -> tuple[list[str], dict]:
    vec = _embed_query(text)
    hits = _qdrant_search(vec, flt, 200)
    order, info = [], {}
    for h in hits:
        pl = h.get("payload") or {}
        p = pl.get("path", "")
        if _skip(p):
            continue
        if p not in info:
            order.append(p)
            info[p] = {"score": float(h.get("score", 0)), "payload": pl, "chunks": 1}
        else:
            info[p]["chunks"] += 1
    return order, info


def _bm25_notes(text: str, flt) -> tuple[list[str], dict]:
    from .bm25 import query_vector
    qv = query_vector(text)
    if not qv["indices"]:
        return [], {}
    body: dict[str, Any] = {"query": qv, "using": "bm25", "limit": 200,
                            "with_payload": True}
    if flt:
        body["filter"] = flt
    res = _qdrant_post(f"/collections/{QDRANT_BM25_COLLECTION}/points/query", body)
    pts = res.get("points", []) if isinstance(res, dict) else res
    order, info = [], {}
    for h in pts:
        pl = h.get("payload") or {}
        p = pl.get("path", "")
        if _skip(p) or p in info:
            continue
        order.append(p)
        info[p] = {"payload": pl}
    return order, info


def _search_v2(query: str, folder: str = "", channel: str = "", fresh: bool = False,
               expand: bool = True) -> dict[str, Any]:
    """Гибридный поиск: [слова пользователя, расширение] × [смысловой, BM25] → RRF → до 100 заметок."""
    q = query.strip()
    flt = _flt(folder, channel)
    exp = _expand_query(q) if expand else ""
    lists, meta, errors = [], {}, []
    for text in [q] + ([exp] if exp and exp.lower() != q.lower() else []):
        for fn, kind in ((_dense_notes, "dense"), (_bm25_notes, "bm25")):
            try:
                order, info = fn(text, flt)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{kind}: {type(exc).__name__}: {str(exc)[:80]}")
                continue
            lists.append(order)
            for p, v in info.items():
                m = meta.setdefault(p, {})
                if "payload" in v and "payload" not in m:
                    m["payload"] = v["payload"]
                if kind == "dense":
                    m["dense"] = max(m.get("dense", 0.0), v.get("score", 0.0))
                    m["payload"] = v["payload"]
    rrf: dict[str, float] = {}
    for lst in lists:
        for i, p in enumerate(lst):
            rrf[p] = rrf.get(p, 0.0) + 1.0 / (60 + i + 1)
    ranked = sorted(rrf, key=lambda p: -rrf[p])[:SEARCH_MAX_NOTES]
    top = rrf[ranked[0]] if ranked else 1.0
    results = []
    for p in ranked:
        pl = meta.get(p, {}).get("payload", {})
        results.append({
            "score": round(rrf[p] / top, 2),
            "path": p,
            "title": pl.get("title") or p.rsplit("/", 1)[-1][:-3],
            "date": pl.get("date", ""),
            "folder": pl.get("folder", ""),
            "channel": pl.get("channel", ""),
            "snippet": (pl.get("snippet") or "")[:300],
            **_links(p),
        })
    if fresh and results:
        head = sorted(results[:30], key=lambda r: r.get("date") or "", reverse=True)
        results = head + results[30:]
    return {"query": q, "expanded_query": exp, "results": results, "errors": errors,
            "lists": len(lists)}


def _write_search_note(query: str, results: list[dict], expanded: str = "",
                       fresh: bool = False) -> dict[str, Any]:
    """Заметка поиска (дизайн пользователя): таблица # | score | date | folder | title | path.
    Создаётся при КАЖДОМ поиске, даже при 0 результатах; повтор того же запроса в тот же день → -2, -3…"""
    import re as _re
    import time as _time
    from datetime import datetime as _dt
    out: dict[str, Any] = {}
    try:
        slug = _re.sub(r"[^\w\s-]", "", query, flags=_re.UNICODE)
        slug = _re.sub(r"[\s]+", "-", slug.strip())[:60].strip("-") or "search"
        base = f"{_dt.now().strftime('%Y-%m-%d')}-{slug}"
        folder = VAULT_PATH / INBOX_PREFIX / "Search"
        folder.mkdir(parents=True, exist_ok=True)
        fname, n = f"{base}.md", 1
        while (folder / fname).exists():
            n += 1
            fname = f"{base}-{n}.md"
        rows = ["| # | score | date | folder | title | path |", "|---|---|---|---|---|---|"]
        for i, x in enumerate(results[:SEARCH_MAX_NOTES], 1):
            rows.append(f"| {i} | {x['score']} | {x['date']} | "
                        f"{x['folder']} | {x['title']} | [[{x['path']}]] |")
        today = _time.strftime("%Y-%m-%d")
        if results:
            body = (f"Полный список результатов ({min(len(results), SEARCH_MAX_NOTES)}):\n\n"
                    + "\n".join(rows) + "\n")
        else:
            body = "0 результатов — ничего не найдено.\n"
        extra = ""
        if expanded:
            extra += f"expanded_query: \"{expanded[:300].replace(chr(34), '')}\"\n"
        if fresh:
            extra += "fresh: true\n"
        fm = (f"---\ntype: поиск\ncreated: {today}\nupdated: {today}\n"
              f"source: opencode\nstatus: draft\nquery: \"{query[:80].replace(chr(34), '')}\"\n"
              f"{extra}results: {len(results)}\ntags: [search]\n---\n\n"
              f"# Поиск: {query[:80]}\n\n" + body)
        (folder / fname).write_text(fm, encoding="utf-8")
        zone_rel = INBOX_PREFIX + "Search/" + fname
        out["artifact_path"] = zone_rel
        al = _links(zone_rel)
        out["artifact_obsidian_url"] = al["obsidian_url"]
        out["artifact_obsidian_web"] = al["obsidian_web"]
    except Exception as exc:  # noqa: BLE001
        out["artifact_error"] = f"{type(exc).__name__}: {str(exc)[:150]}"
    return out


@mcp.tool()
def semantic_search(query: str, folder: str = "", channel: str = "", limit: int = 100,
                    fresh: bool = False) -> dict[str, Any]:
    """Поиск по заметкам: СЫРЫЕ результаты для своего анализа (по папке/каналу и т.п.).
    Для ответа пользователю на «найди / что у меня есть / что я знаю про X» вызывай find_notes —
    он вернёт готовый ответ в формате пользователя.
    Гибридный поиск: по смыслу (синонимы, перефразировки) и по словам (BM25), по словам пользователя
    и по расширенной формулировке (её делает сервер — сам запрос не расширяй).
    Каждый вызов создаёт заметку поиска (Inbox/<зона>/Search/<дата>-<тема>.md) с полным списком.
    folder — точное значение свойства (например 'Marketing/'), channel — имя канала (например '.growth');
    fresh=true — «сначала свежее» (только если пользователь просит новое/актуальное).
    Возвращает score (1.0 = лучший), path, title, date, folder, snippet, telegram_url (если заметка из
    Telegram), obsidian_url, obsidian_web. Read-only: индекс пишут только индексаторы."""
    try:
        r = _search_v2(query, folder, channel, fresh)
        results = r["results"][:max(1, min(int(limit or 100), SEARCH_MAX_NOTES))]
        log_operation("semantic_search", query, "success",
                      {"notes": len(results), "expanded": r["expanded_query"][:120],
                       "lists": r["lists"], "errors": r["errors"][:2]})
        out: dict[str, Any] = {"query": query, "expanded_query": r["expanded_query"],
                               "total_notes": len(results), "results": results}
        out.update(_write_search_note(query, r["results"], r["expanded_query"], fresh))
        if r["errors"]:
            out["warnings"] = r["errors"]
        return out
    except Exception as exc:  # noqa: BLE001
        log_operation("semantic_search", query, "error", {"error": str(exc)[:120]})
        return {"query": query, "error": str(exc)[:200]}

# --- find_notes: готовый ответ в формате пользователя (2026-11-07, v2) ---
import re as _re_fn

_FN_MDLINK = _re_fn.compile(r"\[([^\]]*)\]\([^)]*\)")
_FN_SUMMARY = [_re_fn.compile(r"^- \*\*Резюме:\*\*\s*(.+)$", _re_fn.M),
               _re_fn.compile(r"^- \*\*Резюме документа:\*\*\s*(.+)$", _re_fn.M),
               _re_fn.compile(r"^- \*\*Описание:\*\*\s*(.+)$", _re_fn.M)]
_FN_SKIP = ("- **", "---", "📎", "🔗", "![", "|", "```", "> ")


_FN_EMOJI = _re_fn.compile("[\U0001F000-\U0001FFFF\u2600-\u27BF\u2190-\u21FF\u2B00-\u2BFF\uFE0F\u200D\u20E3]")


def _fn_clean(s: str) -> str:
    s = _FN_MDLINK.sub(r"\1", s)
    s = _re_fn.sub(r"https?://\S+", "", s)
    s = _re_fn.sub(r"#[\w-]+", "", s)
    s = _FN_EMOJI.sub("", s).replace("\u200b", "")
    s = _re_fn.sub(r"[*_`>#]+", "", s)
    s = _re_fn.sub(r"\s+", " ", s)
    return s.strip(" -—:·|•")


def _fn_cut(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    c = s[:n]
    i = c.rfind(" ")
    return (c[:i] if i > n * 0.6 else c).rstrip(" ,.;:—-") + "…"


def _fn_field(body: str, rx) -> str:
    m = rx.search(body)
    if not m:
        return ""
    v = _fn_clean(m.group(1))
    if not v or v.strip("_() ").lower() in ("нет", "") or _re_fn.fullmatch(r"\[.*\]", v):
        return ""
    return v


def _fn_digest(rel: str) -> dict:
    """Название и короткое резюме заметки — из её собственных полей: резюме видео/аудио и
    документов (их сделала LLM конвейера при импорте), текст поста, описание картинки.
    Ничего не генерирует."""
    try:
        text = (VAULT_PATH / rel).read_text(encoding="utf-8", errors="replace")[:30000]
    except Exception:  # noqa: BLE001
        text = ""
    body = text
    if body.startswith("---"):
        end = body.find("\n---", 3)
        body = body[end + 4:] if end > 0 else body
    media_sum = _fn_field(body, _FN_SUMMARY[0]) or _fn_field(body, _FN_SUMMARY[1])
    img_sum = _fn_field(body, _FN_SUMMARY[2])
    title = ""
    m = _re_fn.search(r"^## 📺 (.+)$", body, _re_fn.M)
    if m:
        title = _fn_clean(m.group(1))
    rest = []
    for line in body.splitlines():
        ls = line.strip().replace("\u200b", "")
        if ls == "---":
            break  # дальше — разделы конвейера (OCR/транскрипты), это не текст поста
        if ls.startswith("#"):
            h = ls.lstrip("#").strip()
            if _FN_EMOJI.search(h) or _re_fn.match(r"(Транскрипт|Описание|Текст документа|Резюме|OCR|Медиа)", h):
                break  # служебный раздел конвейера
            ls = h  # заголовок самого поста — это текст
        if not ls or ls.startswith(_FN_SKIP):
            continue
        c = _fn_clean(ls)
        if len(c) < 8 or c.lower().startswith(("медиа:", "автор", "источник")):
            continue
        if not title:
            parts = _re_fn.split(r"(?<=[.!?])\s+", c, maxsplit=1)
            title = parts[0]
            if len(parts) > 1 and len(parts[1]) >= 8:
                rest.append(parts[1])
        elif c != title:
            rest.append(c)
        if len(" ".join(rest)) > 400:
            break
    if not title:
        stem = rel.rsplit("/", 1)[-1]
        stem = stem[:-3] if stem.endswith(".md") else stem
        title = _re_fn.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem).replace("-", " ")
    title = title.rstrip(" .")
    post = " ".join(rest)
    summary = media_sum or (post if len(post) >= 40 else "") or img_sum or post
    if summary.lower().startswith(title.lower()[:40]):
        summary = summary[len(title):].strip(" .—-:")
    return {"title": _fn_cut(title, 140), "summary": _fn_cut(summary, 400)}


def _fn_desc(rel: str, n: int = 300) -> str:
    """Описание заметки в 2–4 строки — из её собственных полей (резюме конвейера / текст поста)."""
    d = _fn_digest(rel)
    stem = rel.rsplit("/", 1)[-1][:-3]
    title, summ = d["title"].rstrip("…"), d["summary"]
    stem_words = _re_fn.sub(r"^\d{4}-\d{2}-\d{2}-", "", stem).replace("-", " ").lower()
    parts = []
    if title and title.lower() != stem_words:
        parts.append(title.rstrip(" ."))
    if summ:
        parts.append(summ)
    if len(parts) == 2 and parts[0][-1:] in "?!…:":
        desc = parts[0] + " " + parts[1]
    else:
        desc = ". ".join(parts).strip()
    return _fn_cut(desc, n) if desc else "(в заметке нет текста — только медиа или ссылка)"


def _fn_line(i: int, x: dict, desc: str) -> str:
    name = x["path"].rsplit("/", 1)[-1][:-3]
    tg, ow = x.get("telegram_url"), x.get("obsidian_web")
    head = f"[{name}]({tg})" if tg else name
    return f"{i}. {head} · [открыть в Obsidian]({ow})\n   {desc}"


@mcp.tool()
def find_notes(query: str, top: int = 20, fresh: bool = False) -> dict[str, Any]:
    """ГЛАВНЫЙ инструмент для «найди в заметках / что у меня есть / что я знаю про X».
    query — тема запроса словами пользователя (без «найди», «в заметках»). Синонимы и перевод НЕ
    добавляй — сервер сам расширяет запрос одинаково для всех агентов.
    fresh=true — только если пользователь просит новое/свежее/актуальное («сначала свежее»).
    Возвращает ГОТОВЫЙ ОТВЕТ answer_markdown в формате пользователя: до 20 заметок, у каждой —
    имя заметки ссылкой на пост в Telegram · «открыть в Obsidian» и описание в 2–4 строки; в конце —
    ссылка «открыть в Obsidian» на заметку поиска с полным списком (до 100).
    ПЕРЕШЛИ answer_markdown ПОЛЬЗОВАТЕЛЮ ДОСЛОВНО: не пересказывай, не сокращай, не меняй ссылки и их
    текст, НЕ добавляй своё обобщение. Обобщение — только по просьбе пользователя («обобщи»): тогда
    вызови search_digest(artifact_path) и следуй его instruction."""
    try:
        top = max(1, min(int(top or 20), 20))
        r = _search_v2(query, fresh=fresh)
        results = r["results"]
        art = _write_search_note(query, results, r["expanded_query"], fresh)
        shown = results[:top]
        art_web = art.get("artifact_obsidian_web", "")
        if shown:
            lines = [f"По запросу «{query}» — самые подходящие заметки"
                     + (" (сначала свежие)" if fresh else "") + ":", ""]
            for i, x in enumerate(shown, 1):
                lines.append(_fn_line(i, x, _fn_desc(x["path"])))
            lines += ["", f"Полный список — [открыть в Obsidian]({art_web})",
                      "Обобщение по всем найденным заметкам — напишите «обобщи»."]
        else:
            lines = [f"По запросу «{query}» ничего не найдено.",
                     f"Заметка поиска — [открыть в Obsidian]({art_web})"]
        answer = "\n".join(lines)
        log_operation("find_notes", query, "success",
                      {"shown": len(shown), "found": len(results), "fresh": fresh,
                       "expanded": r["expanded_query"][:120], "errors": r["errors"][:2]})
        return {"query": query, "answer_markdown": answer,
                "artifact_path": art.get("artifact_path"), "artifact_obsidian_web": art_web,
                "instruction": ("Перешли answer_markdown пользователю ДОСЛОВНО, без своего обобщения. "
                                "Если пользователь попросит «обобщи» — search_digest(path=artifact_path)."),
                "results": [{"name": x["path"].rsplit("/", 1)[-1][:-3], "path": x["path"],
                             "date": x["date"], "folder": x["folder"],
                             "telegram_url": x.get("telegram_url"), "obsidian_web": x.get("obsidian_web")}
                            for x in shown]}
    except Exception as exc:  # noqa: BLE001
        log_operation("find_notes", query, "error", {"error": str(exc)[:120]})
        return {"query": query, "error": str(exc)[:200]}


SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "gpt-6-luna")
SUMMARY_EFFORT = os.getenv("SUMMARY_EFFORT", "max")
SUMMARY_PICK_TIMEOUT = float(os.getenv("SUMMARY_PICK_TIMEOUT", "70"))
SUMMARY_TIMEOUT = float(os.getenv("SUMMARY_TIMEOUT", "210"))
SUMMARY_MAX_NOTES = int(os.getenv("SUMMARY_MAX_NOTES", "40"))
SUMMARY_NOTE_CHARS = int(os.getenv("SUMMARY_NOTE_CHARS", "10000"))
SUMMARY_TOTAL_CHARS = int(os.getenv("SUMMARY_TOTAL_CHARS", "320000"))
_PICK_SYS = (
    "Тебе дан запрос пользователя и пронумерованный список найденных заметок с краткими описаниями. "
    "Выбери заметки, которые ДЕЙСТВИТЕЛЬНО посвящены теме запроса и полезны для обобщения "
    "(заметка, где тема лишь мельком упомянута — например, реклама или шутка со словом из запроса, — НЕ подходит) "
    f"(не больше {SUMMARY_MAX_NOTES}), от самых важных к менее важным. "
    "Ответь ТОЛЬКО JSON-массивом номеров, например [3, 1, 17]. Если по теме ничего нет — [].")
_SUMMARY_SYS = (
    "Ты пишешь обобщение по личной базе заметок пользователя (сохранённые посты из его Telegram-каналов "
    "и ручные записи). Тебе дан запрос и пронумерованные заметки ЦЕЛИКОМ (текст поста, резюме, "
    "транскрипты, распознанный текст).\n"
    "Правила:\n"
    "1. Используй ТОЛЬКО факты из заметок, ничего не выдумывай и не добавляй от себя.\n"
    "2. Если заметка оказалась не по теме — не используй её.\n"
    "3. Начни с 1–3 предложений главного вывода. Затем 3–7 смысловых разделов: строка заголовка вида "
    "«### Название раздела», под ней пункты «- …» с конкретикой: цифры, сроки, суммы, шаги, методы, "
    "инструменты, сервисы, имена, а также противоречия и расхождения между заметками.\n"
    "4. В конце каждого пункта — номера заметок-источников в квадратных скобках: [3] или [3][17]. "
    "Ссылки, названия заметок и список источников НЕ пиши — только номера в скобках.\n"
    "5. Пиши по-русски, без вступлений вроде «Вот обобщение».")


def _llm_chat(model: str, messages: list, timeout: float, effort: str = "",
              max_tokens: int = 16000) -> str:
    import json as _json
    import urllib.request as _ur
    try:
        keys = [k.strip() for k in Path(EXPAND_KEY_FILE).read_text(encoding="utf-8-sig").splitlines() if k.strip()]
    except OSError:
        keys = []
    last = "no LLM key"
    for key in keys:
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if effort:
            body["reasoning_effort"] = effort
        req = _ur.Request(EXPAND_URL, data=_json.dumps(body).encode(), method="POST", headers={
            "Authorization": "Bearer " + key, "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0", "x-opencode-session": "memory-system-search"})
        try:
            with _ur.urlopen(req, timeout=timeout) as r:
                d = _json.loads(r.read().decode())
            txt = (d["choices"][0]["message"].get("content") or "").strip()
            if txt:
                return txt
            last = "empty answer"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {str(exc)[:120]}"
    raise RuntimeError(last)


def _fn_fulltext(rel: str, n: int = 10000) -> str:
    """Тело заметки целиком (без frontmatter, ссылок и лишних пробелов), не длиннее n символов."""
    try:
        t = (VAULT_PATH / rel).read_text(encoding="utf-8", errors="replace")[:400000]
    except OSError:
        return ""
    if t.startswith("---"):
        e = t.find("\n---", 3)
        t = t[e + 4:] if e > 0 else t
    t = _re_fn.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = _re_fn.sub(r"https?://\S+", "", t)
    t = _re_fn.sub(r"[ \t]+", " ", t)
    t = _re_fn.sub(r"\n\s*\n+", "\n", t).strip()
    return t[:n]


@mcp.tool()
def search_digest(path: str) -> dict[str, Any]:
    """ОБОБЩЕНИЕ по ВСЕМ найденным заметкам из заметки поиска (не только по показанным 20). Пишет сервер.
    Вызывай, когда пользователь просит «обобщи» / «сделай резюме по всем найденным».
    path — artifact_path из последнего find_notes/semantic_search (Inbox/<зона>/Search/….md).
    Сервер (модель gpt-6-luna): 1) из всех найденных выбирает заметки по теме; 2) читает их ЦЕЛИКОМ
    (с транскриптами); 3) пишет обобщение с номерами источников и списком «Источники» в формате
    пользователя «N. [имя заметки](пост) · [открыть в Obsidian](…)»; 4) дописывает «## Обобщение» в
    заметку поиска. Занимает 1,5–4 минуты.
    Возвращает answer_markdown — ПЕРЕШЛИ ЕГО ПОЛЬЗОВАТЕЛЮ ДОСЛОВНО, без сокращений и своих добавок."""
    import json as _json
    import re as _re
    import time as _time
    t0 = _time.time()
    try:
        safe = validate_read_path(path)
        rel_note = safe.relative_to(VAULT_PATH).as_posix()
        text = safe.read_text(encoding="utf-8", errors="replace")
        paths = list(dict.fromkeys(_re.findall(r"\[\[([^\]|]+?\.md)\]\]", text)))[:SEARCH_MAX_NOTES]
        qm = _re.search(r'^query:\s*"?(.*?)"?\s*$', text, _re.M)
        query = qm.group(1) if qm else ""
        al = _links(rel_note)
        footer = f"Заметка поиска — [открыть в Obsidian]({al['obsidian_web']})"
        if not paths:
            return {"search_note": rel_note,
                    "answer_markdown": f"По запросу «{query}» нечего обобщать — ничего не найдено.\n\n{footer}"}
        # Шаг 1: отбор по теме из всех найденных (по описаниям)
        cat = "\n".join(f"[{i}] {p.rsplit('/', 1)[-1][:-3]} — {_fn_desc(p, 300)}"
                        for i, p in enumerate(paths, 1))
        picked: list[int] = []
        pick_note = ""
        try:
            raw = _llm_chat(SUMMARY_MODEL, [{"role": "system", "content": _PICK_SYS},
                                            {"role": "user", "content": f"Запрос: {query}\n\nЗаметки:\n{cat}"}],
                            SUMMARY_PICK_TIMEOUT, SUMMARY_EFFORT, 4000)
            m = _re.search(r"\[[\d,\s]*\]", raw)
            for x in (_json.loads(m.group(0)) if m else []):
                if isinstance(x, int) and 1 <= x <= len(paths) and x not in picked:
                    picked.append(x)
        except Exception as exc:  # noqa: BLE001
            pick_note = f"отбор не удался ({str(exc)[:60]}), взяты первые по релевантности"
        if not picked and not pick_note:
            return {"search_note": rel_note,
                    "answer_markdown": f"По запросу «{query}» среди найденных заметок нет подходящих по теме — обобщать нечего.\n\n{footer}"}
        if not picked:
            picked = list(range(1, min(len(paths), 25) + 1))
        picked = picked[:SUMMARY_MAX_NOTES]
        sel = [paths[i - 1] for i in picked]
        # Шаг 2: полные тексты отобранных
        per = max(2000, min(SUMMARY_NOTE_CHARS, SUMMARY_TOTAL_CHARS // max(len(sel), 1)))
        blocks = []
        for k, rel in enumerate(sel, 1):
            body = _fn_fulltext(rel, per) or _fn_desc(rel, 400)
            blocks.append(f"[{k}] {rel.rsplit('/', 1)[-1][:-3]}\n{body}")
        user = f"Запрос: {query}\n\nЗаметки ({len(sel)}):\n\n" + "\n\n=====\n\n".join(blocks)
        summary = _llm_chat(SUMMARY_MODEL, [{"role": "system", "content": _SUMMARY_SYS},
                                            {"role": "user", "content": user}],
                            SUMMARY_TIMEOUT, SUMMARY_EFFORT)
        # Шаг 3: номера источников по порядку появления + список «Источники»
        order: list[int] = []
        rx = r"\s*(?:\[\d+(?:\s*[,;]\s*\d+)*\])+"

        def _num(m):
            nums = []
            for x in _re.findall(r"\d+", m.group(0)):
                k = int(x)
                if 1 <= k <= len(sel):
                    if k not in order:
                        order.append(k)
                    n = order.index(k) + 1
                    if n not in nums:
                        nums.append(n)
            return (" (" + ", ".join(str(n) for n in nums) + ")") if nums else ""

        body = _re.sub(rx, _num, summary).strip()
        body_tg = _re.sub(r"^#{1,6}\s*(.+?)\s*$", r"**\1**", body, flags=_re.M)
        src, src_note = [], []
        for n, k in enumerate(order, 1):
            rel = sel[k - 1]
            name = rel.rsplit("/", 1)[-1][:-3]
            lk = _links(rel)
            tg, ow = lk.get("telegram_url"), lk.get("obsidian_web")
            head = f"[{name}]({tg})" if tg else name
            src.append(f"{n}. {head} · [открыть в Obsidian]({ow})")
            src_note.append(f"{n}. [[{rel}|{name}]]" + (f" · [пост]({tg})" if tg else ""))
        head_line = (f"Обобщение по запросу «{query}»: из {len(paths)} найденных заметок по теме отобрано "
                     f"{len(sel)}, прочитаны целиком.")
        if pick_note:
            head_line += f" ({pick_note})"
        answer = (head_line + "\n\n" + body_tg + "\n\n**Источники**\n" + "\n".join(src)
                  + "\n\n" + footer)
        saved = False
        try:
            wp = validate_write_path(rel_note)
            with open(wp, "a", encoding="utf-8") as f:
                f.write("\n## Обобщение\n\n" + head_line + "\n\n" + body + "\n\n### Источники\n"
                        + "\n".join(src_note) + "\n")
            saved = True
        except Exception:  # noqa: BLE001
            saved = False
        log_operation("search_digest", rel_note, "success",
                      {"notes": len(paths), "picked": len(sel), "sources": len(order), "per_note": per,
                       "chars": len(answer), "sec": round(_time.time() - t0, 1), "saved": saved,
                       "model": SUMMARY_MODEL})
        return {"search_note": rel_note, "answer_markdown": answer, "saved_to_search_note": saved,
                "instruction": "Перешли answer_markdown пользователю ДОСЛОВНО, без сокращений и своих добавок."}
    except PathSecurityError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log_operation("search_digest", path, "error", {"error": str(exc)[:120], "sec": round(_time.time() - t0, 1)})
        return {"error": f"Не удалось сделать обобщение: {str(exc)[:200]}"}


if __name__ == "__main__":
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    async def health_endpoint(request):  # noqa: ARG001
        return JSONResponse({"status": "ok", "service": "memory-mcp"})

    # Two transports on one port:
    #   GET  /sse  + POST /messages  -> SSE        (opencode client)
    #   POST /mcp (+ GET/DELETE)     -> Streamable HTTP (Hermes client)
    # shttp_app.routes == [Route("/mcp", StreamableHTTPASGIApp)]; include them
    # directly (nested Mount changes the remaining path and 404s). Its lifespan
    # runs the StreamableHTTP session manager.
    sse_app = mcp.sse_app()
    shttp_app = mcp.streamable_http_app()

    app = Starlette(
        routes=[
            Route("/health", health_endpoint, methods=["GET"]),
            *list(shttp_app.routes),
            Mount("/", app=sse_app),
        ],
        lifespan=shttp_app.router.lifespan_context,
    )

    port = int(os.getenv("MCP_PORT", "8765"))
    host = os.getenv("MCP_HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port, log_level="info")

