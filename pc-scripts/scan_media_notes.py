"""Скан заметок с медиа-файлами (voice/video/audio) в vault → длительности из Telegram → оценка стоимости Deepgram.

Создаёт media_targets.json: [{channel_id, msg_id, kind, duration, size, mime, note_path}].
Запуск: python scan_media_notes.py
"""
import os
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from telethon import TelegramClient

CODE = Path(__file__).resolve().parent
VAULT = Path(r"C:\Users\<user>\Documents\Obsidian Vault\Inbox\Telegram-Saved")
OUT = CODE / "media_targets.json"

TRANSCRIBABLE = {"voice", "video", "video_note", "audio"}


def load_env():
    env = {}
    for line in (CODE / ".env").read_text(encoding="utf-8-sig").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def fm_int(text, key):
    m = re.search(rf"^{key}: (-?\d+)$", text, re.M)
    return int(m.group(1)) if m else None


def main():
    notes = []
    for p in VAULT.rglob("*.md"):
        if p.name == "_index.md":
            continue
        head = p.read_text(encoding="utf-8", errors="replace")[:2500]
        if "## Транскрипт" in head:
            continue
        m = re.search(r"^media: \[([^\]]+)\]$", head, re.M)
        if not m:
            continue
        kinds = [x.strip() for x in m.group(1).split(",")]
        hit = kinds & TRANSCRIBABLE if False else set(kinds) & TRANSCRIBABLE
        if not hit:
            continue
        cid = fm_int(head, "channel_id")
        mid = fm_int(head, "msg_id")
        if cid is None or mid is None:
            continue
        notes.append({"channel_id": cid, "msg_id": mid, "kinds": sorted(hit),
                      "path": str(p), "has_doc": "document" in kinds})
    print(f"Заметок с медиа (voice/video/audio): {len(notes)}")
    if not notes:
        OUT.write_text("[]", encoding="utf-8")
        return

    env = load_env()
    session = CODE / "session" / env.get("SESSION_NAME", "vault_import")
    client = TelegramClient(str(session), int(env["API_ID"]), env["API_HASH"])

    async def run():
        await client.connect()
        if not await client.is_user_authorized():
            print("[!] Сессия не авторизована")
            return
        by_channel = {}
        for n in notes:
            by_channel.setdefault(n["channel_id"], []).append(n)
        results = []
        for cid, items in by_channel.items():
            ids = [n["msg_id"] for n in items]
            id2note = {n["msg_id"]: n for n in items}
            try:
                msgs = await client.get_messages(cid, ids=ids)
            except Exception as e:
                print(f"[!] канал {cid}: {type(e).__name__}: {str(e)[:80]}")
                continue
            for msg in msgs:
                if msg is None or msg.media is None:
                    continue
                n = id2note.get(msg.id)
                if n is None:
                    continue
                doc = msg.document
                if doc is None:
                    continue  # фото/sticker и пр. — не транскрибируем
                mime = doc.mime_type or ""
                size = doc.size or 0
                v_attr = a_attr = None
                skip = False
                dur = 0
                title = (msg.raw_text or "").splitlines()[0][:80] if msg.raw_text else ""
                for a in doc.attributes:
                    tname = type(a).__name__
                    if tname == "DocumentAttributeVideo":
                        v_attr = a
                    elif tname == "DocumentAttributeAudio":
                        a_attr = a
                    elif tname in ("DocumentAttributeSticker", "DocumentAttributeAnimated"):
                        skip = True
                if skip:
                    continue  # GIF/sticker — транскрибировать нечего
                if v_attr is None and a_attr is None:
                    if not mime.startswith(("audio/", "video/")):
                        continue
                    kind = "document"
                elif v_attr is not None:
                    kind = "video_note" if getattr(v_attr, "round_message", False) else "video"
                    dur = getattr(v_attr, "duration", 0) or 0
                else:
                    kind = "voice" if getattr(a_attr, "voice", False) else "audio"
                    dur = getattr(a_attr, "duration", 0) or 0
                    title = f"{getattr(a_attr, 'performer', '') or ''} {getattr(a_attr, 'title', '') or ''}".strip()
                results.append({**n, "kind": kind, "duration": dur, "size": size,
                                "mime": mime, "title": title})
        await client.disconnect()
        OUT.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        from collections import Counter
        cnt = Counter(r["kind"] for r in results)
        total_min = sum(r["duration"] or 0 for r in results) / 60
        est = total_min * 0.0052
        print(f"Транскрибируемых медиа: {len(results)} | по типам: {dict(cnt)}")
        print(f"Суммарная длительность: {total_min:.0f} мин | оценка Deepgram: ${est:.2f}")
        print(f"Сохранено: {OUT.name}")

    asyncio.run(run())


if __name__ == "__main__":
    main()
