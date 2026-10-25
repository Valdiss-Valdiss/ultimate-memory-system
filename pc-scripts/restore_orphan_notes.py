# -*- coding: utf-8 -*-
"""Восстановление заметок для постов, чьи медиа остались в бэкапе, а заметок нет.

Случай 20.10: два видео (msg 859 «Маркетинг», msg 529 «ИИ») лежали в
staging-сиротах, заметки для них не были созданы (посты старше курсора бота).
Скрипт создаёт заметку по содержимому поста (текст + метаданные) — дальше
штатный медиа-конвейер (media_backfill) сам подхватит видео и расшифрует.

Запуск: python restore_orphan_notes.py [--dry]
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
VAULT = Path(os.environ.get("VAULT_DIR", r"C:\Users\<user>\Documents\Obsidian Vault"))

TARGETS = [
    {"cid": -1000000000000, "mid": 859, "channel": ".Маркетинг", "folder": "Marketing/"},
    {"cid": -1000000000000, "mid": 529, "channel": ".ии", "folder": "AI/"},
]

import vault_guard  # noqa: E402
from tg_common import load_folders, serialize_message, slugify  # noqa: E402
from tg_convert import build_frontmatter, note_filename, render_markdown  # noqa: E402


def env_map():
    env = {}
    for line in (SCRIPT_DIR / ".env").read_text(encoding="utf-8-sig").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    if not args.dry and not vault_guard.require_obsidian(print, "restore_orphan_notes"):
        return
    from telethon import TelegramClient
    env = env_map()
    folders = load_folders()
    sess = SCRIPT_DIR / "session" / env.get("SESSION_NAME", "vault_import")
    client = TelegramClient(str(sess), int(env["API_ID"]), env["API_HASH"])
    await client.start()
    if not await client.is_user_authorized():
        print("[!] сессия не авторизована")
        return
    for t in TARGETS:
        try:
            entity = await client.get_entity(t["cid"])
            msg = await client.get_messages(entity, ids=t["mid"])
            if msg is None:
                print(f"[!] {t['channel']}:{t['mid']} — сообщение недоступно")
                continue
            meta = {"channel_id": t["cid"], "title": t["channel"], "username": None}
            rec = serialize_message(msg, meta)
            text = rec.get("text") or ""
            media = rec.get("media") or []
            body = render_markdown(text, rec.get("entities") or [])
            fm, _ = build_frontmatter(rec, (rec.get("date") or "")[:10], folders)
            if not body.strip():
                body = "_(пост без текста)_"
            dest_dir = VAULT / t["folder"]
            dest_dir.mkdir(parents=True, exist_ok=True)
            fname = note_filename(rec, {p.stem.casefold() for p in dest_dir.glob("*.md")})
            dest = dest_dir / f"{fname}.md"
            n = 2
            while dest.exists():
                dest = dest_dir / f"{fname}-{n}.md"
                n += 1
            if args.dry:
                print(f"[dry] {t['channel']}:{t['mid']} → {dest.relative_to(VAULT)} "
                      f"| media={media} | текст {len(body)} симв.")
                continue
            dest.write_text(fm + "\n\n" + body + "\n", encoding="utf-8")
            print(f"[+] создано: {dest.relative_to(VAULT)} | media={media} | {len(body)} симв.")
        except Exception as e:
            print(f"[err] {t['channel']}:{t['mid']}: {type(e).__name__}: {str(e)[:120]}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
