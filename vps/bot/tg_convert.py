"""Конвертер v2: export/*.jsonl → заметки Obsidian (стандарт SKILL v2.2 + TG-поля).

v2: альбомы (grouped_id) склеиваются в одну заметку; слаг имён до 60 символов;
имя из названия веб-превью для постов без текста; свойство youtube: для YT-ссылок
(секции резюме/транскрипта добавляет yt_enrich.py).

python tg_convert.py                     # полный прогон → output/<канал>/*.md + report.md
python tg_convert.py --limit 20          # по 20 свежих постов на канал (проба)
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from collections import Counter
from datetime import date
from pathlib import Path

from tg_common import SCRIPT_DIR, load_folders, slugify, yt_video_ids


def uuid7():
    """UUIDv7 (Python 3.14+) с эмуляцией для 3.12 (VPS-образ)."""
    if hasattr(uuid, "uuid7"):
        return uuid.uuid7()
    ms = int(time.time() * 1000)
    rand = os.urandom(10)
    b = ms.to_bytes(6, "big") + b"\x70" + rand[0:2] + b"\x80" + rand[2:8]
    import uuid as _u
    return _u.UUID(bytes=b)

TODAY = date.today().isoformat()
WRAPPER = {
    "bold": ("**", "**"), "italic": ("*", "*"), "underline": ("__", "__"),
    "strike": ("~~", "~~"), "spoiler": ("||", "||"), "code": ("`", "`"),
}


def render_markdown(text, entities):
    if not text:
        return ""
    if not entities:
        return text
    ents = sorted(entities, key=lambda e: (e["offset"], -e["length"]))
    accepted = []
    last_end = -1
    for e in ents:
        start, end = e["offset"], e["offset"] + e["length"]
        if start >= last_end:
            accepted.append(e)
            last_end = end
    parts = []
    pos = 0
    for e in accepted:
        kind = e["type"]
        s = text[e["offset"]:e["offset"] + e["length"]]
        if e["offset"] > pos:
            parts.append(text[pos:e["offset"]])
        if kind in WRAPPER:
            o, c = WRAPPER[kind]
            parts.append(f"{o}{s}{c}")
        elif kind == "pre":
            lang = e.get("lang", "")
            parts.append(f"```{lang}\n{s}\n```" if "\n" in s else f"```{lang} {s}```")
        elif kind == "blockquote":
            parts.append("\n".join("> " + ln for ln in s.split("\n")))
        elif kind == "text_link":
            url = e.get("url", "")
            if url and re.match(r"^https?://", s.strip(), re.I):
                parts.append(url)
            else:
                parts.append(f"[{s}]({url})")
        elif kind == "mention_name":
            parts.append(f"[{s}](tg://user?id={e.get('user_id', '')})")
        else:
            parts.append(s)
        pos = e["offset"] + e["length"]
    if pos < len(text):
        parts.append(text[pos:])
    return "".join(parts)


def merge_albums(records):
    """Склейка альбомов: сообщения с одинаковым grouped_id → одна запись."""
    simple = []
    groups = {}
    for rec in records:
        gid = rec.get("grouped_id")
        if gid is None:
            simple.append(rec)
        else:
            groups.setdefault(gid, []).append(rec)
    merged_out = []
    for gid, members in groups.items():
        members = sorted(members, key=lambda r: r["msg_id"])
        text_members = [m for m in members if (m.get("text") or "").strip()]
        rep = text_members[0] if text_members else members[0]
        texts = [m["text"].strip() for m in text_members]
        media = []
        for m in members:
            for k in m.get("media") or []:
                media.append(k)
        webpage = next((m.get("webpage") for m in members if m.get("webpage")), None)
        views = max([m.get("views") or 0 for m in members] or [0]) or None
        reactions = max((m.get("reactions") or [] for m in members),
                        key=lambda rs: sum(r.get("count", 0) for r in rs), default=[])
        fwd = next((m.get("forward") for m in members if m.get("forward")), None)
        author = next((m.get("post_author") for m in members if m.get("post_author")), None)
        merged = {
            "channel_id": rep["channel_id"],
            "channel": rep["channel"],
            "msg_id": rep["msg_id"],
            "date": members[0].get("date"),
            "text": "\n\n".join(texts),
            "entities": rep.get("entities") or [],
            "forward": fwd,
            "media": media,
            "webpage": webpage,
            "views": views,
            "reactions": reactions,
            "post_author": author,
            "reply_to": rep.get("reply_to"),
            "grouped_id": gid,
            "album_size": len(members),
            "album_msg_ids": [m["msg_id"] for m in members],
        }
        merged_out.append(merged)
    return simple + merged_out


def build_url(rec):
    cid = rec["channel_id"]
    mid = rec["msg_id"]
    username = rec.get("channel_username") or None
    if username:
        return f"https://t.me/{username}/{mid}"
    if cid is not None and cid < 0:
        if cid <= -1000000000000:
            internal = -1000000000000 - cid
        else:
            internal = -cid
        return f"https://t.me/c/{internal}/{mid}"
    return None


def fm_list(values):
    return "[" + ", ".join(str(v) for v in values) + "]"


def fm_quote(value):
    return '"' + str(value).replace('"', "'") + '"'


def build_frontmatter(rec, note_date, folders=None):
    lines = [
        "---",
        "type: ссылка",
        f"id: {uuid7()}",
        f"created: {note_date}",
        f"updated: {note_date}",
        "source: opencode",
        "status: draft",
        "platform: telegram",
        f"channel: {fm_quote(rec['channel'])}",
        f"channel_id: {rec['channel_id']}",
        f"msg_id: {rec['msg_id']}",
    ]
    url = build_url(rec)
    if url:
        lines.append(f"url: {url}")
    if rec.get("post_author"):
        lines.append(f"author: {fm_quote(rec['post_author'])}")
    if rec.get("views"):
        lines.append(f"views: {rec['views']}")
    reactions = rec.get("reactions") or []
    total_reactions = sum(r.get("count", 0) for r in reactions)
    if total_reactions:
        lines.append(f"reactions: {total_reactions}")
    fwd = rec.get("forward")
    if fwd and (fwd.get("origin_name") or fwd.get("post_author")):
        lines.append(f"forward_from: {fm_quote(fwd.get('origin_name') or fwd.get('post_author'))}")
    media = rec.get("media") or []
    if media:
        lines.append(f"media: {fm_list(sorted(set(media)))}")
    if rec.get("album_size"):
        lines.append(f"album_size: {rec['album_size']}")
    yt = yt_video_ids(rec.get("text") or "", (rec.get("webpage") or {}).get("url") or "")
    if yt:
        lines.append(f"youtube: https://youtu.be/{yt[0]}")
    if folders:
        target = folders.get(rec["channel"])
        if target:
            lines.append(f"folder: {fm_quote(target)}")
    lines.append(f"imported: {TODAY}")
    lines.append("tags: [telegram]")
    lines.append("---")
    return "\n".join(lines), url


def dup_key(rec):
    fwd = rec.get("forward")
    if fwd and fwd.get("origin_id") and fwd.get("origin_msg_id"):
        return f"fw:{fwd['origin_id']}:{fwd['origin_msg_id']}"
    text = (rec.get("text") or "").strip()
    if len(text) >= 20:
        norm = re.sub(r"\s+", " ", text).strip().lower()
        return "tx:" + hashlib.sha256(norm.encode("utf-8")).hexdigest()
    return None


def note_filename(rec, used_names):
    name_source = ""
    text = (rec.get("text") or "").strip()
    if text:
        for ln in text.splitlines():
            ln = ln.strip().lstrip(">").lstrip("#").strip()
            if ln:
                name_source = ln
                break
    if not name_source:
        wp = rec.get("webpage") or {}
        name_source = wp.get("title") or ""
    if not name_source:
        counts = Counter(rec.get("media") or [])
        name_source = " ".join(f"{k}x{v}" if v > 1 else k for k, v in counts.items())
    base = slugify(name_source, 60) if name_source else "без-названия"
    day = (rec.get("date") or "")[:10] or "неизвестно"
    base_name = f"{day}-{base}"
    name = base_name
    if name.casefold() in used_names:
        name = f"{base_name}-{rec['msg_id']}"
    n = 2
    while name.casefold() in used_names:
        name = f"{base_name}-{rec['msg_id']}-{n}"
        n += 1
    used_names.add(name.casefold())
    return name


def convert_channel(jsonl_path, out_dir, limit, used_names, report, dup_map, folders=None):
    records = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    records = merge_albums(records)
    records.sort(key=lambda r: r["msg_id"], reverse=True)
    if limit:
        records = records[:limit]
    channel_slug = slugify(jsonl_path.stem)
    ch_dir = out_dir / channel_slug
    ch_dir.mkdir(parents=True, exist_ok=True)
    first = records[0] if records else {}
    index_links = []
    stats = {"notes": 0, "duplicates": 0, "empty": 0, "albums": 0, "youtube": 0}
    for rec in records:
        text = rec.get("text") or ""
        media = rec.get("media") or []
        webpage = rec.get("webpage")
        if not text.strip() and not media and not webpage:
            stats["empty"] += 1
            continue
        if rec.get("album_size"):
            stats["albums"] += 1
        yt = yt_video_ids(text, (webpage or {}).get("url") or "")
        if yt:
            stats["youtube"] += 1
        key = dup_key(rec)
        if key and key in dup_map:
            stats["duplicates"] += 1
            orig_name, orig_channel = dup_map[key]
            name = note_filename(rec, used_names)
            fm, url = build_frontmatter(rec, (rec.get("date") or "")[:10], folders)
            fm = fm[:-4] + f"duplicate_of: \"[[{orig_name}]]\"\n---"
            body = f"Дубликат поста из «{orig_channel}» — оригинал: [[{orig_name}]]."
            if url:
                body += f"\n\n[Этот пост в Telegram]({url})"
            (ch_dir / f"{name}.md").write_text(fm + "\n\n" + body + "\n", encoding="utf-8")
            stats["notes"] += 1
            index_links.append(((rec.get("date") or "")[:10] or TODAY, name))
            continue
        md = render_markdown(text, rec.get("entities") or [])
        extras = []
        if media and set(media) - {"webpage"}:
            counts = Counter(media)
            media_str = ", ".join(f"{k} ×{v}" if v > 1 else k for k, v in counts.items())
            extras.append(f"📎 Медиа: {media_str} — смотрите по ссылке на оригинал")
        if webpage and webpage.get("url"):
            title = webpage.get("title") or webpage.get("url")
            extras.append(f"🔗 [{title}]({webpage['url']})")
        body = md
        if extras:
            body = (body + "\n\n" if body.strip() else "") + "\n".join(extras)
        if not body.strip():
            body = "_(медиа-пост без текста)_"
        note_date = (rec.get("date") or "")[:10] or TODAY
        fm, url = build_frontmatter(rec, note_date, folders)
        name = note_filename(rec, used_names)
        (ch_dir / f"{name}.md").write_text(fm + "\n\n" + body + "\n", encoding="utf-8")
        stats["notes"] += 1
        if key:
            dup_map[key] = (name, rec["channel"])
        index_links.append((note_date, name))
    write_index(ch_dir, first.get("channel") or jsonl_path.stem, index_links)
    report.append((channel_slug, first.get("channel") or jsonl_path.stem, dict(stats), len(records)))
    return stats


def write_index(ch_dir, channel_title, links):
    links = sorted(links, key=lambda x: x[0], reverse=True)
    cap = 500
    shown = links[:cap]
    lines = [
        "---",
        "type: ссылка",
        f"created: {TODAY}",
        f"updated: {TODAY}",
        "source: opencode",
        "status: draft",
        "platform: telegram",
        f"channel: {fm_quote(channel_title)}",
        f"imported: {TODAY}",
        "tags: [telegram]",
        "---",
        "",
        f"# {channel_title} — импорт из Telegram",
        "",
        f"Заметок: {len(links)}" + (f" (показаны последние {cap})" if len(links) > cap else ""),
        "",
    ]
    for day, name in shown:
        lines.append(f"- {day} — [[{name}]]")
    if len(links) > cap:
        lines.append(f"- …и ещё {len(links) - cap}: поиск по `channel: \"{channel_title}\"` или тегу #telegram")
    (ch_dir / "_index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--export-dir", default=str(SCRIPT_DIR / "export"))
    parser.add_argument("--out", default=str(SCRIPT_DIR / "output"))
    parser.add_argument("--limit", type=int, default=0, help="постов на канал (проба)")
    args = parser.parse_args()
    export_dir = Path(args.export_dir)
    out_dir = Path(args.out)
    manifest_path = export_dir / "manifest.json"
    if not manifest_path.exists():
        sys.exit("[!] Нет export/manifest.json — сначала tg_export.py --export")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    used_names = set()
    dup_map = {}
    folders = load_folders()
    report = []
    for ch in manifest["channels"]:
        jsonl = export_dir / ch["jsonl"]
        if not jsonl.exists():
            print(f"[!] нет {jsonl.name} — пропуск")
            continue
        stats = convert_channel(jsonl, out_dir, args.limit, used_names, report, dup_map, folders)
        extras = []
        if stats["albums"]:
            extras.append(f"альбомов {stats['albums']}")
        if stats["youtube"]:
            extras.append(f"youtube {stats['youtube']}")
        print(f"{ch['title']}: заметок {stats['notes']}, дубликатов {stats['duplicates']}, "
              f"пустых {stats['empty']}" + (f", {', '.join(extras)}" if extras else ""))
    rep = ["# Отчёт конвертации v2 (альбомы склеены)", "",
           f"Дата: {TODAY}, лимит: {args.limit or 'без лимита'}", "",
           "| Канал | Сообщений | Заметок | Дубликатов | Пустых | Альбомов | YouTube |",
           "|---|---|---|---|---|---|---|"]
    for slug, title, stats, total in report:
        rep.append(f"| {title} | {total} | {stats['notes']} | {stats['duplicates']} | "
                   f"{stats['empty']} | {stats['albums']} | {stats['youtube']} |")
    total_notes = sum(s["notes"] for _, _, s, _ in report)
    total_dupes = sum(s["duplicates"] for _, _, s, _ in report)
    total_albums = sum(s["albums"] for _, _, s, _ in report)
    total_yt = sum(s["youtube"] for _, _, s, _ in report)
    rep += ["", f"**ИТОГО заметок: {total_notes}** (дубликатов-ссылок: {total_dupes}, "
                f"склеено альбомов: {total_albums}, с YouTube: {total_yt})"]
    (out_dir / "report.md").write_text("\n".join(rep) + "\n", encoding="utf-8")
    print(f"\nГотово. Заметки: {out_dir}\\<канал>\\, отчёт: {out_dir}\\report.md")


if __name__ == "__main__":
    main()
