# -*- coding: utf-8 -*-
"""Бэкфилл фото: vault-скан → user-сессия (read-only) → распознавание через VPS
(convert_one.py: mimo-vision) → прямой патч заметок в vault.

Осмысленные (скриншоты/инфографика/таблицы/схемы) получают «## 🖼 Изображение»;
мемы/фотографии — терминальную пометку «текстовой нагрузки нет».
ПК ничего не устанавливает. Правило: записи в vault — при ОТКРЫТОМ Obsidian.
Лог: photos_backfill.log.
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SCRIPT_DIR = Path(__file__).resolve().parent
VAULT = Path(os.environ.get("VAULT_DIR", r"C:\Users\<user>\Documents\Obsidian Vault"))
TMP = Path(os.environ.get("PHOTO_TMP_DIR", r"C:\Users\ADMINI~1\AppData\Local\Temp\opencode\photobf"))
BATCH = int(os.environ.get("PHOTO_BATCH", "25"))
LIMIT = int(os.environ.get("PHOTO_LIMIT", "0"))
HOST = "ai-agent@CHANGE-ME-server-ip"
KEY = r"C:\Users\<user>\.ssh\id_ed25519"
SKIP_SIGNALS = ("## 🖼 Изображение", "has_ocr_text: true",
                "текстовой нагрузки нет", "не обработано")
HARD_SKIP = "не обработано"
PRIORITY = ("Маркетинг/", "Рост/", "Книга/", "Дизайн/", "IT/")
LOG = SCRIPT_DIR / "photos_backfill.log"

env = {}
for line in (SCRIPT_DIR / ".env").read_text(encoding="utf-8-sig").splitlines():
    if "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()

sys.path.insert(0, str(SCRIPT_DIR))


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)
    try:
        print(line, flush=True)
    except Exception:
        pass  # stdout может быть сломан (Errno 22) — не убивать процесс
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def scan_targets():
    targets = []
    for p in VAULT.rglob("*.md"):
        rel = str(p.relative_to(VAULT)).replace("\\", "/")
        if "/.trash" in rel or rel.startswith(".trash/") or p.name.startswith("_index"):
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        fm = content.split("---", 2)
        if len(fm) < 3:
            continue
        mv = re.search(r"^media: \[([^\]]*)\]", fm[1], re.M)
        if not mv:
            continue
        types = {t.strip() for t in mv.group(1).split(",") if t.strip()}
        if not (types & {"photo", "image", "document"}):
            continue
        if HARD_SKIP in content:
            continue
        m = re.search(r"^channel_id: (-?\d+)$", content, re.M)
        g = re.search(r"^msg_id: (\d+)$", content, re.M)
        if not m or not g:
            continue
        album = re.search(r"^album_size: (\d+)$", fm[1], re.M)
        known = (len(re.findall(r"^## 🖼 Изображение", content, re.M))
                 + len(re.findall(r"^## 📄 Документ: ", content, re.M))
                 + content.count("текстовой нагрузки нет")
                 + len(re.findall(r"- \*\*Документ:\*\* ⚠️", content, re.M)))
        if album:
            # Добор альбома (2026-09-14): миграция писала в media: только вложение
            # подписного сообщения → окно 2n+2 обрезало альбомы. album_size — истина.
            # Частично обработанные заметки тоже цель: process_one скипает уже
            # обработанные фото (по числу секций+мем-строк).
            if int(album.group(1)) <= known:
                continue
        else:
            # Без album_size — старое поведение: только полностью необработанные
            # фото-заметки; док-заметки и частично обработанные не трогаем.
            if "document" in types or any(s in content for s in SKIP_SIGNALS):
                continue
        prio = 0 if any(rel.startswith(pr) for pr in PRIORITY) else 1
        targets.append((int(m.group(1)), int(g.group(1)), p, prio))
    targets.sort(key=lambda t: (t[3], t[2].name.lower()))
    return targets


def ssh_run(args, timeout=900, capture=True):
    cmd = ["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=no", HOST] + args
    return subprocess.run(cmd, capture_output=capture, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def convert_on_vps(workkey):
    for attempt in range(2):
        r = ssh_run(["mkdir", "-p", f"/tmp/photobf/{workkey}"], timeout=60)
        base = TMP / workkey
        ok_all = True
        for name in os.listdir(base):
            r2 = subprocess.run(
                ["scp", "-q", "-i", KEY, "-o", "StrictHostKeyChecking=no",
                 str(base / name), f"{HOST}:/tmp/photobf/{workkey}/{name}"],
                capture_output=True, timeout=300)
            if r2.returncode != 0:
                ok_all = False
                time.sleep(3)
                break
        if ok_all:
            break
        if attempt == 2:
            return {"status": "error", "error": "scp upload failed"}
    r = ssh_run(
        ["docker", "run", "--rm", "--network", "localai_default", "--env-file", "/opt/memory-stack/tg-bot/.env", "-e", "PYTHONIOENCODING=utf-8",
         "-v", f"/tmp/photobf/{workkey}:/w",
         "memory-tg-tools:latest", "python", "/app/convert_one.py", "/w"],
        timeout=600)
    out = (r.stdout or "").strip()
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return {"status": "error", "error": (r.stderr or "no json")[:200]}
    try:
        return json.loads(m.group(0))
    except Exception as e:
        return {"status": "error", "error": f"bad json: {e}"}


def vps_cleanup(workkey):
    ssh_run(["rm", "-rf", f"/tmp/photobf/{workkey}"], timeout=60, capture=False)


def patch_photo_note(path, results):
    content = path.read_text(encoding="utf-8")
    if any(s in content for s in SKIP_SIGNALS):
        return "skip"
    flags = []
    idx = len(re.findall(r"^## 🖼 Изображение", content, re.M))
    sections = []
    for res in results:
        idx += 1
        if res.get("meaningful"):
            flags.append("has_ocr_text: true")
            parts = [f"\n\n---\n\n## 🖼 Изображение ({idx})\n\n",
                     f"- **Тип:** {res.get('type', 'другое')}\n"]
            if res.get("description"):
                parts.append(f"- **Описание:** {res['description']}\n")
            if res.get("text"):
                parts.append(f"- **Текст на изображении:**\n\n{res['text']}\n")
            sections.append("".join(parts))
        else:
            sections.append("\n\n- **Изображение:** текстовой нагрузки нет (мем/фото) — пропуск\n")
    if not sections:
        return "skip"
    for fl in dict.fromkeys(flags):
        if fl not in content:
            content = content.replace("tags: [telegram]", fl + "\ntags: [telegram]", 1)
    path.write_text(content.rstrip("\n") + "".join(sections) + "\n", encoding="utf-8")
    return "ok"


async def process_one(target, client, i, total):
    cid, mid, note, prio = target
    key = f"{cid}:{mid}"
    workkey = f"{abs(cid)}_{mid}"
    try:
        entity = await client.get_entity(cid)
        note_content = note.read_text(encoding="utf-8")
        n_photos = len(re.search(r"^media: \[([^\]]+)\]$", note_content, re.M).group(1).split(","))
        album = re.search(r"^album_size: (\d+)$", note_content, re.M)
        span = max(n_photos, int(album.group(1)) if album else 0)
        msgs = await client.get_messages(entity, ids=list(range(mid, mid + span)))
        photos = []
        for msg in msgs or []:
            if msg is None or getattr(msg, "photo", None) is None:
                continue
            photos.append(msg)
        # Добор альбома: секции+мем-строки уже съели первые K фото (обработка
        # шла по возрастанию msg_id) — скипаем их, обрабатываем только недостающие.
        consumed = (len(re.findall(r"^## 🖼 Изображение", note_content, re.M))
                    + note_content.count("текстовой нагрузки нет"))
        if consumed:
            consumed = min(consumed, len(photos))
            if consumed:
                photos = photos[consumed:]
                log(f"[gap] {key}: {consumed} фото уже в заметке — добор {len(photos)}")
        if not photos:
            log(f"[skip] {key}: фото нет в посте ({note.name})")
            return "skip"
        wdir = TMP / workkey
        results = []
        errors = []
        if wdir.exists():
            for old in wdir.iterdir():
                old.unlink()
        wdir.mkdir(parents=True, exist_ok=True)
        for msg in photos:
            local = wdir / "input.jpg"
            if local.exists():
                local.unlink()
            try:
                path = await asyncio.wait_for(client.download_media(msg, file=str(local)), timeout=180)
            except (asyncio.TimeoutError, Exception) as e:
                log(f"[err] {key}: download: {type(e).__name__}: {str(e)[:100]}")
                continue
            if not path:
                continue
            size = os.path.getsize(path)
            (wdir / "sidecar.json").write_text(json.dumps(
                {"channel_id": cid, "msg_id": msg.id, "kind": "photo",
                 "title": "", "duration": 0, "mime": "image/jpeg",
                 "size": size, "date": msg.date.isoformat()}, ensure_ascii=False),
                encoding="utf-8")
            res = await asyncio.to_thread(convert_on_vps, workkey)
            if res.get("status") == "ok":
                results.append(res)
            else:
                errors.append(str(res.get("error"))[:120])
                log(f"[err] {key}: {str(res.get('error'))[:120]}")
        if results and not errors:
            r = patch_photo_note(note, results)
            if r == "ok":
                log(f"[+] {key} ({len(results)} фото) [{i}/{total}] ({note.name})")
                return "done"
            return "skip"
        if errors:
            log(f"[retry-later] {key}: есть ошибки — заметка не патчилась, повтор на следующем прогоне")
        return "error"
    except Exception as e:
        log(f"[err] {key}: {type(e).__name__}: {str(e)[:120]}")
        return "error"
    finally:
        try:
            wdir = TMP / workkey
            if wdir.exists():
                for old in wdir.iterdir():
                    old.unlink()
                wdir.rmdir()
        except Exception:
            pass
        vps_cleanup(workkey)


async def process(targets, client):
    TMP.mkdir(parents=True, exist_ok=True)
    workers = int(os.environ.get("PHOTO_WORKERS", "1"))
    sem = asyncio.Semaphore(workers)
    counts = {"done": 0, "skip": 0, "error": 0}
    total = len(targets)

    async def runner(i, target):
        async with sem:
            r = await process_one(target, client, i, total)
            counts[r if r in counts else "error"] += 1
            if r != "error" and i < total:
                await photos_pace(i)

    tasks = [runner(i, t) for i, t in enumerate(targets, 1)]
    await asyncio.gather(*tasks)
    log(f"=== Итог: обработано {counts['done']}, скип {counts['skip']}, ошибок {counts['error']} ===")


RAM_MIN_MB = int(os.environ.get("RAM_MIN_MB", "2500"))
PHOTO_PAUSE_SEC = int(os.environ.get("PHOTO_PAUSE_SEC", "30"))
PHOTO_BATCH_PAUSE_SEC = int(os.environ.get("PHOTO_BATCH_PAUSE_SEC", "90"))


def vps_ram_avail_sync():
    r = ssh_run(["bash", "-lc", "free -m | awk '/Mem:/{print $7}'"], timeout=45)
    try:
        return int((r.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 9999


async def photos_pace(i):
    """Pace-control: пауза между целями, батч-паузы, RAM-гейт."""
    await asyncio.sleep(PHOTO_PAUSE_SEC)
    if i % BATCH == 0:
        log(f"[pace] батч {i // BATCH} завершён — пауза {PHOTO_BATCH_PAUSE_SEC} с")
        await asyncio.sleep(PHOTO_BATCH_PAUSE_SEC)
    for _ in range(20):
        avail = await asyncio.to_thread(vps_ram_avail_sync)
        if avail >= RAM_MIN_MB:
            return
        log(f"[pace] RAM мало ({avail} МБ < {RAM_MIN_MB}) — пауза 120 с")
        await asyncio.sleep(120)


async def main():
    log("=== photos_backfill: старт ===")
    running = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Obsidian.exe"],
                             capture_output=True, text=True).stdout.lower().find("obsidian.exe")
    if running == -1:
        log("[!] Obsidian не запущен — внешние записи в vault запрещены. Открой Obsidian и перезапусти.")
        return
    targets = scan_targets()
    log(f"целей: {len(targets)} (приоритетных: {sum(1 for t in targets if t[3] == 0)})")
    if LIMIT:
        targets = targets[:LIMIT]
        log(f"лимит запуска: {LIMIT}")
    if not targets:
        log("=== Итог: целей нет ===")
        return
    from telethon import TelegramClient
    sess = SCRIPT_DIR / "session" / (env.get("SESSION_NAME") or "session")
    client = TelegramClient(str(sess), int(env["API_ID"]), env["API_HASH"])
    for attempt in range(10):
        try:
            await asyncio.wait_for(client.start(), timeout=120)
            break
        except Exception as e:
            log(f"[warn] telethon start попытка {attempt + 1}/10: {type(e).__name__}: {str(e)[:100]}")
            try:
                await client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(60)
    else:
        log("[!] Telethon не стартовал за 10 попыток — выход")
        return
    log("[OK] сессия поднята (read-only)")
    try:
        await process(targets, client)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())








