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
MAX_DOC_DOWNLOAD = int(os.environ.get("MAX_DOC_DOWNLOAD_MB", "700")) * 1024 * 1024
HOST = "memory@CHANGE-ME-server-ip"
KEY = r"C:\Users\<user>\.ssh\id_ed25519"
SKIP_SIGNALS = ("файл слишком большой", "не обработан")
DONE_SIGNAL = "has_doc_text: true"
PRIORITY = ("Marketing/", "Growth/", "Books/")
LOG = SCRIPT_DIR / "docs_backfill.log"

env = {}
for line in (SCRIPT_DIR / ".env").read_text(encoding="utf-8-sig").splitlines():
    if "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()

sys.path.insert(0, str(SCRIPT_DIR))

import vps_ssh


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
                     + len(re.findall(r"- \*\*Документ:\*\* ⚠️", content, re.M))
                     + content.count("не документ (пропуск)")
                     + content.count("текстовой нагрузки нет"))
            if int(album.group(1)) <= known:
                continue
        prio = 0 if any(rel.startswith(pr) for pr in PRIORITY) else 1
        targets.append((int(m.group(1)), int(g.group(1)), p, prio))
    targets.sort(key=lambda t: (t[3], t[2].name.lower()))
    return targets


SSH_OPTS = ["-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3"]


def ssh_run(args, timeout=900, capture=True):
    cmd = ["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=no"] + SSH_OPTS + [HOST] + args
    return subprocess.run(cmd, capture_output=capture, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def convert_on_vps(workkey):
    """Каталог ПК/врем/docbf/<key>/ уже содержит файл+sidecar.json → sftp на VPS → docker.
    Транспорт — vps_ssh (одно постоянное соединение на прогон, keepalive, ретрай)."""
    remote = f"/tmp/docbf/{workkey}"
    try:
        rc, _, err = vps_ssh.exec_(f"mkdir -p {remote}", timeout=30)
        if rc != 0:
            return {"status": "error", "error": f"mkdir rc={rc} {(err or '')[:120]}"}
        for name in os.listdir(TMP / workkey):
            vps_ssh.put(TMP / workkey / name, remote)
        rc, out, err = vps_ssh.exec_(
            f"docker run --rm --network localai_default --env-file /opt/memory-stack/tg-bot/.env "
            f"-e PYTHONIOENCODING=utf-8 -v {remote}:/w -v /opt/memory-stack/tg-bot/state:/app/state "
            f"memory-tg-tools:latest python /app/convert_one.py /w", timeout=1200)
        out = (out or "").strip()
        m = re.search(r"\{.*\}", out, re.S)
        if not m:
            return {"status": "error",
                    "error": f"no json rc={rc} out={out[-120:]!r} err={(err or '')[-120:]!r}"}
        try:
            return json.loads(m.group(0))
        except Exception as e:
            return {"status": "error", "error": f"bad json: {e}"}
    except Exception as e:
        vps_ssh.close()
        return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:120]}"}


def vps_cleanup(workkey):
    try:
        vps_ssh.exec_(f"rm -rf /tmp/docbf/{workkey}", timeout=60)
    except Exception:
        vps_ssh.close()


def fmt_size(n):
    n = int(n or 0)
    return f"{n // 1024 // 1024} МБ" if n >= 1024 * 1024 else f"{n // 1024} КБ"


def _atomic_write(path, content):
    tmp = path.with_name(path.name + ".patchtmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


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
    _atomic_write(path, content.rstrip("\n") + section)
    return "ok"


def mark_note(path, marker):
    content = path.read_text(encoding="utf-8")
    if marker not in content:
        content = content.rstrip("\n") + f"\n\n{marker}\n"
        _atomic_write(path, content)


async def process(targets, client):
    TMP.mkdir(parents=True, exist_ok=True)
    done = skipped = failed = 0
    for i, (cid, mid, note, prio) in enumerate(targets, 1):
        key = f"{cid}:{mid}"
        workkey = f"{abs(cid)}_{mid}"
        try:
            entity = await asyncio.wait_for(client.get_entity(cid), timeout=120)
            note_content = note.read_text(encoding="utf-8")
            n_docs = len(re.search(r"^media: \[([^\]]+)\]$", note_content, re.M).group(1).split(","))
            album = re.search(r"^album_size: (\d+)$", note_content, re.M)
            span = max(n_docs, int(album.group(1)) if album else 0)
            msgs = await asyncio.wait_for(
                client.get_messages(entity, ids=list(range(mid, mid + span))), timeout=120)
            docs = []
            for msg in msgs or []:
                if msg is None or getattr(msg, "document", None) is None:
                    continue
                doc = msg.document
                mime = (doc.mime_type or "")
                if mime.startswith(("audio/", "video/")):
                    # 2026-10-20: аудио/видео в док-альбоме — не документ; помечаем,
                    # иначе known никогда не сойдётся и заметка циклится вечно.
                    mark_note(note, "- **Вложение:** аудио/видео в альбоме — не документ (пропуск)")
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
                    path = await asyncio.wait_for(client.download_media(msg, file=str(local)), timeout=3600)
                except (asyncio.TimeoutError, Exception) as e:
                    log(f"[err] {key}: download: {type(e).__name__}: {str(e)[:100]}")
                    continue
                if not path:
                    continue
                size = os.path.getsize(path)
                with open(path, "rb") as fh:
                    magic = fh.read(8).hex()
                log(f"[dl] {key}: {(title or 'noname')[:40]!r} {size} bytes magic={magic}")
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
                    results.append("error")
                    err_txt = str(res.get("error"))[:120]
                    if not err_txt:
                        err_txt = f"empty error, res={ {k: v for k, v in res.items() if k != 'md'} }"
                    log(f"[err] {key}: {err_txt}")
            if results and "error" not in results and "wait_job" not in results:
                content = note.read_text(encoding="utf-8")
                if "has_doc_text: true" not in content:
                    content = content.replace("tags: [telegram]", "has_doc_text: true\ntags: [telegram]", 1)
                    _atomic_write(note, content)
            if any(r in ("ok", "dup", "unsupported") for r in results):
                done += 1
                log(f"[+] {key} {results} [{i}/{len(targets)}] ({note.name})")
            else:
                log(f"[retry-later] {key}: {results} — заметка не финализирована, повтор на следующем прогоне")
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
    try:
        rc, out, _ = vps_ssh.exec_("free -m | awk '/Mem:/{print $7}'", timeout=45)
        return int((out or "").strip().splitlines()[-1])
    except Exception as e:
        vps_ssh.close()
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








