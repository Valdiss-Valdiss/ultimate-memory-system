"""Вотчер v3: чисто событийный (ботам нельзя историю — ограничение Telegram).

- events.NewMessage: посты каналов, где бот админ → буфер (альбомы склеиваются)
- sweeper каждые 20с: буферы старше 20с → заметки в staging (обогатитель донесёт в mirror)
- новые каналы регистрируются автоматически (channels.json)
- пропуски при простоях добирает ПК-скрипт pc_catchup.py (вариант «гибрид»)
"""
import asyncio
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from telethon import TelegramClient, events

from tg_common import load_folders, serialize_message, slugify
from tg_convert import (build_frontmatter, dup_key, merge_albums,
                        note_filename, render_markdown)

CODE = Path(__file__).resolve().parent
STAGE = Path(os.environ.get("TG_STAGE_DIR", "/staging"))
STATE_DIR = Path(os.environ.get("TG_STATE_DIR", "/app/state"))
MIRROR = Path(os.environ.get("MIRROR_DIR", "/mirror"))
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

CHANNELS = CODE / "channels.json"
pending = {}
lock = __import__("threading").Lock()


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_atomic(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_channels():
    return load_json(CHANNELS, {})


def save_channels(chans):
    save_atomic(CHANNELS, chans)


def register_channel(chat_id, chat):
    chans = load_channels()
    sid = str(chat_id)
    if sid in chans:
        return False
    chans[sid] = {"title": getattr(chat, "title", None) or sid,
                  "access_hash": str(getattr(chat, "access_hash", 0) or 0),
                  "username": getattr(chat, "username", None)}
    save_channels(chans)
    print(f"[auto] новый канал: {chans[sid]['title']} ({sid}) — добавлен", flush=True)
    return True


def stems(*dirs):
    s = set()
    for d in dirs:
        if d.exists():
            for p in d.glob("*.md"):
                s.add(p.stem.casefold())
    return s


def flush_buffer(key):
    b = pending[key]
    meta = b["meta"]
    all_recs = list(b["recs"])
    recs = [r for r in all_recs
            if (r.get("text") or "").strip() or r.get("media") or r.get("webpage")]
    if not recs:
        # Все части без контента — двигаем курсор, чтобы пост не залипал
        # «новым» навсегда (заметку на пустоту не создаём).
        max_id = max((r["msg_id"] for r in all_recs), default=0)
        if max_id:
            state = load_json(STATE_DIR / "state.json", {})
            cid = str(meta["channel_id"])
            if int(state.get(cid, 0) or 0) < max_id:
                state[cid] = max_id
                save_atomic(STATE_DIR / "state.json", state)
        del pending[key]
        return 0
    seen = load_json(STATE_DIR / "seen.json", {})
    fresh = [r for r in recs if f"{r['channel_id']}:{r['msg_id']}" not in seen]
    if not fresh:
        # Повторная доставка уже обработанных постов (ресинк сессии после
        # рестарта) — двигаем курсор, заметки не создаём.
        max_id = max((r["msg_id"] for r in recs), default=0)
        if max_id:
            state = load_json(STATE_DIR / "state.json", {})
            cid = str(meta["channel_id"])
            if int(state.get(cid, 0) or 0) < max_id:
                state[cid] = max_id
                save_atomic(STATE_DIR / "state.json", state)
        del pending[key]
        return 0
    recs = fresh
    folders = load_folders()
    state = load_json(STATE_DIR / "state.json", {})
    dups = load_json(STATE_DIR / "dups.json", {})
    recs = merge_albums(sorted(recs, key=lambda r: r["msg_id"]))
    slug = slugify(meta["title"])
    ch_dir = STAGE / slug
    ch_dir.mkdir(parents=True, exist_ok=True)
    mirror_dir = MIRROR / slug
    used = stems(ch_dir, mirror_dir)
    made = 0
    for rec in recs:
        key_d = dup_key(rec)
        dup_of = dups.get(key_d, {}).get("name") if key_d else None
        md = render_markdown(rec.get("text") or "", rec.get("entities") or [])
        media = rec.get("media") or []
        extras = []
        if media and set(media) - {"webpage"}:
            counts = Counter(media)
            extras.append("📎 Медиа: " + ", ".join(f"{k} ×{v}" if v > 1 else k for k, v in counts.items())
                          + " — смотрите по ссылке на оригинал")
        wp = rec.get("webpage")
        if wp and wp.get("url"):
            extras.append(f"🔗 [{wp.get('title') or wp['url']}]({wp['url']})")
        body = md
        if extras:
            body = (body + "\n\n" if body.strip() else "") + "\n".join(extras)
        if not body.strip():
            body = "_(медиа-пост без текста)_"
        name = note_filename(rec, used)
        fm, url = build_frontmatter(rec, (rec.get("date") or "")[:10], folders)
        if dup_of:
            fm = fm[:-4] + f'duplicate_of: "[[{dup_of}]]"\n---'
        (ch_dir / f"{name}.md").write_text(fm + "\n\n" + body + "\n", encoding="utf-8")
        made += 1
        if key_d:
            dups[key_d] = {"channel": rec["channel"], "name": name}
    state[str(meta["channel_id"])] = max(r["msg_id"] for r in recs)
    save_atomic(STATE_DIR / "state.json", state)
    save_atomic(STATE_DIR / "dups.json", dups)
    for r in recs:
        seen[f"{r['channel_id']}:{r['msg_id']}"] = name
    save_atomic(STATE_DIR / "seen.json", seen)
    del pending[key]
    print(f"[+] {meta['title']}: заметок {made}", flush=True)
    return made


async def sweeper():
    while True:
        await asyncio.sleep(20)
        now = time.time()
        with lock:
            stale = [k for k, b in pending.items() if now - b["last"] >= 20]
            for k in stale:
                try:
                    flush_buffer(k)
                except Exception as e:
                    print(f"[!] flush: {type(e).__name__}: {str(e)[:150]}", flush=True)


async def main():
    STAGE.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(CODE / "session" / "watcher"), API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    print(f"[OK] Вотчер v3 запущен: @{me.username} | каналов: {len(load_channels())} | режим: события", flush=True)

    @client.on(events.NewMessage())
    async def on_new(event):
        if not event.is_channel:
            return
        try:
            chat = await event.get_chat()
        except Exception:
            return
        if chat is None or not getattr(chat, "broadcast", False):
            return
        register_channel(event.chat_id, chat)
        meta = {"channel_id": event.chat_id,
                "title": getattr(chat, "title", None) or str(event.chat_id),
                "username": getattr(chat, "username", None)}
        rec = serialize_message(event.message, meta)
        gid = rec.get("grouped_id") or f"s-{rec['msg_id']}"
        key = (event.chat_id, gid)
        with lock:
            pending.setdefault(key, {"recs": [], "meta": meta, "last": 0})
            pending[key]["recs"].append(rec)
            pending[key]["last"] = time.time()

    sweeper_task = asyncio.create_task(sweeper())
    await asyncio.gather(client.run_until_disconnected(), sweeper_task)


if __name__ == "__main__":
    asyncio.run(main())
