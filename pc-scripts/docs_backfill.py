# -*- coding: utf-8 -*-
"""Бэкфилл документов: vault-скан → user-сессия (read-only) → конвертация на VPS
(convert_one.py: docling/mimo) → прямой патч заметок в vault (паттерн media_backfill).

ПК ничего не устанавливает: конвертация — в образе memory-tg-tools на VPS.
Правило: внешние записи в vault — только при ОТКРЫТОМ Obsidian (скрипт проверяет).
Идемпотентность: повторный прогон берёт заметки без has_doc_text/секции/пометок.
Лог: docs_backfill.log.
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
TMP = Path(os.environ.get("DOC_TMP_DIR", r"C:\Users\ADMINI~1\AppData\Local\Temp\opencode\docbf"))
BATCH = int(os.environ.get("DOC_BATCH", "10"))
LIMIT = int(os.environ.get("DOC_LIMIT", "0"))
MAX_DOC_DOWNLOAD = int(os.environ.get("MAX_DOC_DOWNLOAD_MB", "250")) * 1024 * 1024
HOST = "ai-agent@CHANGE-ME-server-ip"
KEY = r"C:\Users\<user>\.ssh\id_ed25519"
SKIP_SIGNALS = ("файл слишком большой", "не обработан")
DONE_SIGNAL = "has_doc_text: true"
PRIORITY = ("Маркетинг/", "Рост/", "Книга/")
LOG = SCRIPT_DIR / "docs_backfill.log"

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
        if len(fm) < 3 or "media: [document" not in fm[1]:
            continue
        if any(s in content for s in SKIP_SIGNALS):
            continue
        m = re.search(r"^channel_id: (-?\d+)$", content, re.M)
        g = re.search(r"^msg_id: (\d+)$", content, re.M)
        if not m or not g:
            continue
        if DONE_SIGNAL in content:
            # Добор альбома (2026-09-14): миграция записала в media: только вложение
            # подписного сообщения → старое окно 2n+2 обрезало альбомы >5.
            # album_size из frontmatter — настоящий размер; если секций+пометок меньше —
            # в альбоме есть необработанные документы.
            album = re.search(r"^album_size: (\d+)$", fm[1], re.M)
            if not album:
                continue
            known = (len(re.findall(r"^## 📄 Документ: ", content, re.M))
                     + len(re.findall(r"^## 🖼 Изображение", content, re.M))
                     + len(re.findall(r"- \*\*Документ:\*\* ⚠️", content, re.M)))
            if int(album.group(1)) <= known:
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
    """Каталог ПК/врем/docbf/<key>/ уже содержит файл+sidecar.json → scp на VPS → docker."""
    for attempt in range(2):
        r = ssh_run(["mkdir", "-p", f"/tmp/docbf/{workkey}"], timeout=30)
        base = TMP / workkey
        ok_all = True
        for name in os.listdir(base):
            r2 = subprocess.run(
                ["scp", "-q", "-i", KEY, "-o", "StrictHostKeyChecking=no",
                 str(base / name), f"{HOST}:/tmp/docbf/{workkey}/{name}"],
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
        ["docker", "run", "--rm", "--network", "localai_default", "--env-file", "/opt/memory-stack/tg-bot/.env",
         "-e", "PYTHONIOENCODING=utf-8",
         "-v", f"/tmp/docbf/{workkey}:/w",
         "-v", "/opt/memory-stack/tg-bot/state:/app/state",
         "memory-tg-tools:latest", "python", "/app/convert_one.py", "/w"],
        timeout=1200)
    out = (r.stdout or "").strip()
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return {"status": "error", "error": (r.stderr or "no json")[:200]}
    try:
        return json.loads(m.group(0))
    except Exception as e:
        return {"status": "error", "error": f"bad json: {e}"}


def vps_cleanup(workkey):
    ssh_run(["rm", "-rf", f"/tmp/docbf/{workkey}"], timeout=60, capture=False)


def fmt_size(n):
    n = int(n or 0)
    return f"{n // 1024 // 1024} МБ" if n >= 1024 * 1024 else f"{n // 1024} КБ"


def patch_note(path, title, size, summary, md):
    content = path.read_text(encoding="utf-8")
    section_hdr = f"## 📄 Документ: {title}"
    if section_hdr in content:
        return "dup"
    if "has_doc_text: true" not in content:
        content = content.replace("tags: [telegram]", "has_doc_text: true\ntags: [telegram]", 1)
    section = ("\n\n---\n\n"
               f"{section_hdr}\n\n"
               f"- **Файл:** {title} ({fmt_size(size)})\n"
               f"- **Резюме документа:** {summary or '_нет_'}\n"
               f"\n## Текст документа\n\n{md}\n")
    path.write_text(content.rstrip("\n") + section, encoding="utf-8")
    return "ok"


def mark_note(path, marker):
    content = path.read_text(encoding="utf-8")
    if marker not in content:
        content = content.rstrip("\n") + f"\n\n{marker}\n"
        path.write_text(content, encoding="utf-8")


async def process(targets, client):
    TMP.mkdir(parents=True, exist_ok=True)
    done = skipped = failed = 0
    for i, (cid, mid, note, prio) in enumerate(targets, 1):
        key = f"{cid}:{mid}"
        workkey = f"{abs(cid)}_{mid}"
        try:
            entity = await client.get_entity(cid)
            note_content = note.read_text(encoding="utf-8")
            n_docs = len(re.search(r"^media: \[([^\]]+)\]$", note_content, re.M).group(1).split(","))
            album = re.search(r"^album_size: (\d+)$", note_content, re.M)
            span = max(n_docs, int(album.group(1)) if album else 0)
            msgs = await client.get_messages(entity, ids=list(range(mid, mid + span)))
            docs = []
            for msg in msgs or []:
                if msg is None or getattr(msg, "document", None) is None:
                    continue
                doc = msg.document
                mime = (doc.mime_type or "")
                if mime.startswith(("audio/", "video/")):
                    continue
                docs.append((msg, doc))
            if not docs:
                skipped += 1
                log(f"[skip] {key}: документов нет в посте ({note.name})")
                continue
            wdir = TMP / workkey
            if wdir.exists():
                for old in wdir.iterdir():
                    old.unlink()
            wdir.mkdir(parents=True, exist_ok=True)
            results = []
            for msg, doc in docs:
                title = ""
                for attr in doc.attributes:
                    if hasattr(attr, "file_name") and getattr(attr, "file_name", None):
                        title = attr.file_name
                        break
                if f"## 📄 Документ: {title or f'document_{msg.id}'}" in note_content:
                    results.append("dup")
                    log(f"[dup] {key}: {(title or '')[:60]} уже в заметке — без перекачки")
                    continue
                local = wdir / ("input" + Path(title or f"document_{msg.id}").suffix)
                if local.exists():
                    local.unlink()
                try:
                    path = await asyncio.wait_for(client.download_media(msg, file=str(local)), timeout=900)
                except (asyncio.TimeoutError, Exception) as e:
                    log(f"[err] {key}: download: {type(e).__name__}: {str(e)[:100]}")
                    continue
                if not path:
                    continue
                size = os.path.getsize(path)
                if size > MAX_DOC_DOWNLOAD:
                    mark_note(note, f"- **Документ:** ⚠️ файл слишком большой ({size // 1024 // 1024} МБ) — обработка пропущена")
                    log(f"[skip] {key}: {size // 1024 // 1024} МБ > лимита")
                    continue
                (wdir / "sidecar.json").write_text(json.dumps(
                    {"channel_id": cid, "msg_id": msg.id, "kind": "document",
                     "title": title, "duration": 0, "mime": mime,
                     "size": size, "date": msg.date.isoformat()}, ensure_ascii=False),
                    encoding="utf-8")
                res = await asyncio.to_thread(convert_on_vps, workkey)
                if res.get("status") == "ok":
                    r = patch_note(note, title or f"document_{msg.id}",
                                   size, res.get("summary", ""), res.get("md", ""))
                    results.append(r)
                    if not res.get("summary") and res.get("summary_error"):
                        log(f"[warn] {key}: резюме не сгенерировано "
                            f"({res.get('summary_error')}) — текст есть, догонит re-summary")
                elif res.get("status") == "wait_job":
                    results.append("wait_job")
                    log(f"[wait-job] {key}: docling job ещё идёт на сервере — "
                        "результат заберём на следующем прогоне (task_id сохранён)")
                elif res.get("status") == "unsupported":
                    mark_note(note, f"- **Документ:** ⚠️ формат {res.get('ext')} не поддерживается — файл в посте")
                    results.append("unsupported")
                else:
                    _state[key] = _state.get(key, 0) + 1
                    if _state[key] >= 3:
                        mark_note(note, f"- **Документ:** ⚠️ не обработан ({str(res.get('error'))[:120]}) — файл в посте")
                        results.append("error-terminal")
                    else:
                        results.append("error")
                        log(f"[err] {key}: {str(res.get('error'))[:120]}")
            if results and "error" not in results and "wait_job" not in results:
                content = note.read_text(encoding="utf-8")
                if "has_doc_text: true" not in content:
                    content = content.replace("tags: [telegram]", "has_doc_text: true\ntags: [telegram]", 1)
                    note.write_text(content, encoding="utf-8")
            if any(r in ("ok", "dup", "unsupported", "error-terminal") for r in results):
                done += 1
                log(f"[+] {key} {results} [{i}/{len(targets)}] ({note.name})")
            vps_cleanup(workkey)
            for old in wdir.iterdir():
                old.unlink()
            if i < len(targets):
                await docs_pace(i)
        except Exception as e:
            failed += 1
            log(f"[err] {key}: {type(e).__name__}: {str(e)[:120]}")
    log(f"=== Итог: обработано {done}, скип {skipped}, ошибок {failed} ===")


RAM_MIN_MB = int(os.environ.get("RAM_MIN_MB", "2500"))
DOC_PAUSE_SEC = int(os.environ.get("DOC_PAUSE_SEC", "300"))
DOC_BATCH_PAUSE_SEC = int(os.environ.get("DOC_BATCH_PAUSE_SEC", "600"))


def vps_ram_avail_sync():
    r = ssh_run(["bash", "-lc", "free -m | awk '/Mem:/{print $7}'"], timeout=45)
    try:
        return int((r.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 9999


async def docs_pace(i):
    """Pace-control: пауза между конвертациями, батч-паузы, RAM-гейт."""
    await asyncio.sleep(DOC_PAUSE_SEC)
    if i % BATCH == 0:
        log(f"[pace] батч {i // BATCH} завершён — пауза {DOC_BATCH_PAUSE_SEC} с")
        await asyncio.sleep(DOC_BATCH_PAUSE_SEC)
    for _ in range(20):
        avail = await asyncio.to_thread(vps_ram_avail_sync)
        if avail >= RAM_MIN_MB:
            return
        log(f"[pace] RAM мало ({avail} МБ < {RAM_MIN_MB}) — пауза 120 с")
        await asyncio.sleep(120)


_state = {}


async def main():
    log("=== docs_backfill: старт ===")
    obsidian = [p for p in ("Obsidian.exe",) ]
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








