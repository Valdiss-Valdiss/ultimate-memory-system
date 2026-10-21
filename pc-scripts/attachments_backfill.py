# -*- coding: utf-8 -*-
"""Бэкфилл вложений: документы (последовательно, docling) + фото (пул ×3, mimo)
в ОДНОМ процессе с ОДНИМ Telethon-клиентом.

Два параллельных клиента на одной сессии дают «database is locked» — поэтому
merge: docs и photos живут в одном event loop, блокирующие subprocess-вызовы —
через asyncio.to_thread. Реиспользует логику docs_backfill / photos_backfill.
Запуск: launch_backfill_attachments.vbs + hourly-guard.
"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import docs_backfill as dbf
import photos_backfill as pbf
import tg_floodguard as fg

LOG = SCRIPT_DIR / "attachments_backfill.log"


def log(msg):
    import time
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)
    try:
        print(line, flush=True)
    except Exception:
        pass  # stdout может быть сломан (Errno 22) — не убивать процесс
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


async def main():
    log("=== attachments_backfill: старт (docs seq + photos x3) ===")
    if fg.guard_or_exit(log):
        return
    running = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Obsidian.exe"],
                               capture_output=True, text=True,
                               encoding="cp1251", errors="replace").stdout.lower().find("obsidian.exe")
    if running == -1:
        log("[!] Obsidian не запущен — внешние записи в vault запрещены. Открой Obsidian.")
        return
    from telethon import TelegramClient
    env = dbf.env
    dtargets = dbf.scan_targets()
    ptargets = pbf.scan_targets()
    log(f"целей: docs={len(dtargets)}, photos={len(ptargets)}")
    if not dtargets and not ptargets:
        log("=== Итог: целей нет ===")
        return
    sess = SCRIPT_DIR / "session" / (env.get("SESSION_NAME") or "session")
    client = None
    for attempt in range(10):
        try:
            client = __import__("telethon").TelegramClient(
                str(sess), int(env["API_ID"]), env["API_HASH"],
                connection_retries=15, retry_delay=5, auto_reconnect=True,
                request_retries=5)
            await asyncio.wait_for(client.start(), timeout=120)
            break
        except Exception as e:
            log(f"[warn] telethon start попытка {attempt + 1}/10: {type(e).__name__}: {str(e)[:100]}")
            try:
                if client:
                    await client.disconnect()
            except Exception:
                pass
            client = None
            await asyncio.sleep(60)
    if client is None:
        log("[!] Telethon не стартовал за 10 попыток — выход")
        return
    log("[OK] сессия поднята (read-only)")
    try:
        dtargets = dbf.scan_targets()
        ptargets = pbf.scan_targets()
        log(f"целей: docs={len(dtargets)}, photos={len(ptargets)}")
        if dtargets:
            log("=== фаза 1: документы (последовательно) ===")
            await dbf.process(dtargets, client)
        if ptargets:
            log("=== фаза 2: фото (последовательно) ===")
            await pbf.process(ptargets, client)
        log("=== attachments_backfill: обе фазы завершены ===")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        if fg.is_flood_error(e):
            print(fg.set_cooldown(60, "( mid-run flood )"), flush=True)
            print("=== выход по flood-охлаждению (сессию не жжём) ===")
        else:
            raise
